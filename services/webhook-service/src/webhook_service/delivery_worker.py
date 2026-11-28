"""Delivery worker: sends pending webhooks, retries failures, and dead-letters the hopeless.

Run as its own process: `python -m webhook_service.delivery_worker`.
"""

import json
import logging
import random
import signal
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import httpx
from opentelemetry import trace
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.trace import Status, StatusCode
from prometheus_client import start_http_server
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from webhook_service.backoff import retry_delay_seconds
from webhook_service.config import Settings, get_settings
from webhook_service.db import get_sessionmaker
from webhook_service.kafka import EventPublisher, KafkaPublisher, OutgoingMessage, ensure_topic
from webhook_service.metrics import (
    DELIVERY_BACKLOG,
    DLQ_SIZE,
    WEBHOOK_DELIVERIES,
    WEBHOOK_DELIVERY_DURATION,
)
from webhook_service.models import DEAD, PENDING, SUCCEEDED, WebhookDelivery, WebhookSubscription
from webhook_service.signing import signed_headers
from webhook_service.telemetry import (
    configure_logging,
    configure_tracing,
    extract_context,
    shutdown_tracing,
)

logger = logging.getLogger(__name__)
tracer = trace.get_tracer(__name__)


@dataclass(frozen=True, slots=True)
class ClaimedDelivery:
    id: uuid.UUID
    url: str
    payload: dict[str, object]
    secret: str
    attempt: int
    trace_context: dict[str, str] | None = None


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    max_attempts: int = 8
    base_seconds: float = 1.0
    cap_seconds: float = 3600.0
    lease_seconds: float = 30.0


class DeliveryWorker:
    def __init__(
        self,
        session_factory: Callable[[], Session],
        http: httpx.Client,
        dlq_publisher: EventPublisher,
        *,
        dlq_topic: str,
        policy: RetryPolicy | None = None,
        batch_size: int = 20,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        rng: random.Random | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.http = http
        self.dlq_publisher = dlq_publisher
        self.dlq_topic = dlq_topic
        self.policy = policy or RetryPolicy()
        self.batch_size = batch_size
        self.clock = clock
        self.rng = rng

    def run_once(self) -> int:
        """Attempt every delivery that is due. Returns how many were attempted."""
        claimed = self.claim_due()
        for delivery in claimed:
            error = self.send(delivery)
            self.record(delivery, error)
        self.publish_dead_letters()
        self.update_gauges()
        return len(claimed)

    def update_gauges(self) -> None:
        with self.session_factory() as session:
            DLQ_SIZE.set(
                session.scalar(
                    select(func.count())
                    .select_from(WebhookDelivery)
                    .where(WebhookDelivery.status == DEAD)
                )
                or 0
            )
            DELIVERY_BACKLOG.set(
                session.scalar(
                    select(func.count())
                    .select_from(WebhookDelivery)
                    .where(
                        WebhookDelivery.status == PENDING,
                        WebhookDelivery.next_attempt_at <= self.clock(),
                    )
                )
                or 0
            )

    def claim_due(self) -> list[ClaimedDelivery]:
        """Lease due deliveries in a short transaction, so no lock is held during HTTP calls.

        The lease pushes `next_attempt_at` forward: if this worker dies mid-send, the row
        becomes due again once the lease runs out and another worker retries it.
        """
        now = self.clock()
        with self.session_factory() as session, session.begin():
            rows = session.execute(
                select(WebhookDelivery, WebhookSubscription.secret)
                .join(
                    WebhookSubscription, WebhookSubscription.id == WebhookDelivery.subscription_id
                )
                .where(WebhookDelivery.status == PENDING, WebhookDelivery.next_attempt_at <= now)
                .order_by(WebhookDelivery.next_attempt_at)
                .limit(self.batch_size)
                .with_for_update(skip_locked=True, of=WebhookDelivery)
            ).all()
            claimed = []
            for delivery, secret in rows:
                delivery.attempts += 1
                delivery.next_attempt_at = now + timedelta(seconds=self.policy.lease_seconds)
                claimed.append(
                    ClaimedDelivery(
                        id=delivery.id,
                        url=delivery.url,
                        payload=delivery.payload,
                        secret=secret,
                        attempt=delivery.attempts,
                        trace_context=delivery.trace_context,
                    )
                )
            return claimed

    def send(self, delivery: ClaimedDelivery) -> str | None:
        """POST the signed webhook. Returns None on success, or a short error description."""
        body = json.dumps(delivery.payload, separators=(",", ":")).encode()
        timestamp = int(self.clock().timestamp())
        # Joins the trace of the transfer that caused this webhook. Retries hours later still
        # land in the same trace, one span per attempt.
        with tracer.start_as_current_span(
            "deliver webhook",
            context=extract_context(delivery.trace_context),
            attributes={"settle.delivery_id": str(delivery.id), "settle.attempt": delivery.attempt},
        ) as span:
            started = time.perf_counter()
            try:
                response = self.http.post(
                    delivery.url,
                    content=body,
                    headers=signed_headers(delivery.secret, timestamp, body),
                )
            except httpx.HTTPError as exc:
                error = f"{type(exc).__name__}: {exc}"[:500]
            else:
                error = None if response.is_success else f"HTTP {response.status_code}"
            finally:
                WEBHOOK_DELIVERY_DURATION.observe(time.perf_counter() - started)
            if error is not None:
                span.set_status(Status(StatusCode.ERROR, error))
            return error

    def record(self, claimed: ClaimedDelivery, error: str | None) -> None:
        now = self.clock()
        with self.session_factory() as session, session.begin():
            delivery = session.get(WebhookDelivery, claimed.id, with_for_update=True)
            # Skip if our lease ran out and another worker has claimed the row since.
            if delivery is None or delivery.status != PENDING:
                return
            if delivery.attempts != claimed.attempt:
                return

            if error is None:
                WEBHOOK_DELIVERIES.labels("succeeded").inc()
                delivery.status = SUCCEEDED
                delivery.delivered_at = now
                delivery.last_error = None
                logger.info("Delivered %s on attempt %d", delivery.id, claimed.attempt)
                return

            delivery.last_error = error
            if claimed.attempt >= self.policy.max_attempts:
                WEBHOOK_DELIVERIES.labels("dead").inc()
                delivery.status = DEAD
                logger.warning(
                    "Delivery %s is DEAD after %d attempts", delivery.id, claimed.attempt
                )
                return
            WEBHOOK_DELIVERIES.labels("retry").inc()
            delay = retry_delay_seconds(
                claimed.attempt,
                base_seconds=self.policy.base_seconds,
                cap_seconds=self.policy.cap_seconds,
                rng=self.rng,
            )
            delivery.next_attempt_at = now + timedelta(seconds=delay)
            logger.info(
                "Delivery %s attempt %d failed (%s); retrying in %.1fs",
                delivery.id,
                claimed.attempt,
                error,
                delay,
            )

    def publish_dead_letters(self) -> int:
        """Publish DEAD deliveries to the dead-letter topic, at least once.

        Marking a row DEAD and publishing it are two systems, so the row records
        `dlq_published_at` only after Kafka acknowledges. A crash in between means the row is
        published again on the next pass, never lost.
        """
        with self.session_factory() as session, session.begin():
            dead = session.scalars(
                select(WebhookDelivery)
                .where(WebhookDelivery.status == DEAD, WebhookDelivery.dlq_published_at.is_(None))
                .limit(self.batch_size)
                .with_for_update(skip_locked=True)
            ).all()
            if not dead:
                return 0
            messages = [
                OutgoingMessage(
                    key=str(delivery.event_id),
                    value=json.dumps(dead_letter(delivery), separators=(",", ":")).encode(),
                    headers={"delivery_id": str(delivery.id)},
                )
                for delivery in dead
            ]
            self.dlq_publisher.publish(self.dlq_topic, messages)
            now = self.clock()
            for delivery in dead:
                delivery.dlq_published_at = now
            return len(dead)


def dead_letter(delivery: WebhookDelivery) -> dict[str, object]:
    return {
        "delivery_id": str(delivery.id),
        "event_id": str(delivery.event_id),
        "subscription_id": str(delivery.subscription_id),
        "url": delivery.url,
        "attempts": delivery.attempts,
        "last_error": delivery.last_error,
        "payload": delivery.payload,
    }


def build_worker(settings: Settings) -> DeliveryWorker:
    http = httpx.Client(timeout=settings.delivery_timeout_seconds)
    HTTPXClientInstrumentor.instrument_client(http)  # adds traceparent to each webhook request
    return DeliveryWorker(
        get_sessionmaker(),
        http,
        KafkaPublisher(settings.kafka_bootstrap_servers),
        dlq_topic=settings.dead_letter_topic,
        policy=RetryPolicy(
            max_attempts=settings.max_attempts,
            base_seconds=settings.retry_base_seconds,
            cap_seconds=settings.retry_cap_seconds,
            lease_seconds=settings.delivery_lease_seconds,
        ),
        batch_size=settings.delivery_batch_size,
    )


def main() -> None:
    configure_logging("webhook-delivery-worker")
    settings = get_settings()
    configure_tracing(
        "webhook-delivery-worker",
        enabled=settings.otel_enabled,
        otlp_endpoint=settings.otel_exporter_otlp_endpoint,
    )
    start_http_server(settings.worker_metrics_port)
    ensure_topic(settings.kafka_bootstrap_servers, settings.dead_letter_topic)
    worker = build_worker(settings)
    stop = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    logger.info("Delivery worker started")
    while not stop.is_set():
        try:
            attempted = worker.run_once()
        except Exception:
            logger.exception("Delivery pass failed; retrying")
            attempted = 0
        if not attempted:
            stop.wait(settings.delivery_poll_interval_seconds)
    shutdown_tracing()


if __name__ == "__main__":
    main()
