"""Outbox relay: publishes committed outbox rows to Kafka.

Run as its own process: `python -m payments_api.outbox_relay`.
"""

import json
import logging
import signal
import threading
from collections.abc import Callable
from datetime import UTC, datetime

from opentelemetry import trace
from opentelemetry.trace import SpanKind, Status, StatusCode
from prometheus_client import start_http_server
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from payments_api.config import get_settings
from payments_api.db import get_sessionmaker
from payments_api.kafka import EventPublisher, KafkaPublisher, OutgoingMessage, ensure_topic
from payments_api.metrics import OUTBOX_LAG, OUTBOX_PUBLISHED
from payments_api.models import OutboxEvent
from payments_api.outbox import event_envelope
from payments_api.telemetry import (
    configure_logging,
    configure_tracing,
    extract_context,
    inject_context,
    shutdown_tracing,
)

logger = logging.getLogger(__name__)
tracer = trace.get_tracer(__name__)


class OutboxRelay:
    def __init__(
        self,
        session_factory: Callable[[], Session],
        publisher: EventPublisher,
        *,
        topic: str,
        batch_size: int = 100,
    ) -> None:
        self.session_factory = session_factory
        self.publisher = publisher
        self.topic = topic
        self.batch_size = batch_size

    def publish_batch(self) -> int:
        """Publish one batch of unpublished events. Returns how many were published.

        Delivery is at-least-once: if the process dies after Kafka acknowledges the batch but
        before the commit below, the same rows are published again on restart. Consumers
        deduplicate by `event_id`.
        """
        with self.session_factory() as session, session.begin():
            # SKIP LOCKED lets several relays run side by side: each one takes rows the
            # others have not locked, instead of waiting for them or publishing them twice.
            # The locks are only on outbox rows, never on accounts, so holding them during
            # the Kafka call does not block transfers.
            events = session.scalars(
                select(OutboxEvent)
                .where(OutboxEvent.published_at.is_(None))
                .order_by(OutboxEvent.created_at)
                .limit(self.batch_size)
                .with_for_update(skip_locked=True)
            ).all()
            if not events:
                return 0

            # One span per event, child of the request that wrote it. Its context travels in
            # the Kafka headers, so the consumer's work joins the same trace.
            spans = [
                tracer.start_span(
                    f"publish {self.topic}",
                    context=extract_context(event.trace_context),
                    kind=SpanKind.PRODUCER,
                    attributes={
                        "messaging.system": "kafka",
                        "messaging.destination.name": self.topic,
                        "settle.event_id": str(event.id),
                        "settle.event_type": event.event_type,
                    },
                )
                for event in events
            ]
            messages = []
            for event, span in zip(events, spans, strict=True):
                with trace.use_span(span, end_on_exit=False):
                    headers = {"event_type": event.event_type, "event_id": str(event.id)}
                    headers.update(inject_context())
                messages.append(
                    OutgoingMessage(
                        key=event.partition_key,
                        value=json.dumps(event_envelope(event), separators=(",", ":")).encode(),
                        headers=headers,
                    )
                )
            try:
                # Raises if Kafka does not acknowledge every message; the transaction then
                # rolls back and the rows stay unpublished for the next attempt.
                self.publisher.publish(self.topic, messages)
            except Exception as exc:
                for span in spans:
                    span.set_status(Status(StatusCode.ERROR, str(exc)))
                    span.end()
                raise
            for span in spans:
                span.end()

            now = datetime.now(UTC)
            for event in events:
                event.published_at = now
            OUTBOX_PUBLISHED.inc(len(events))
            return len(events)

    def measure_lag(self) -> float:
        """Seconds since the oldest unpublished event was written; 0 when all are published."""
        with self.session_factory() as session:
            oldest = session.scalar(
                select(func.min(OutboxEvent.created_at)).where(OutboxEvent.published_at.is_(None))
            )
        lag = 0.0 if oldest is None else max((datetime.now(UTC) - oldest).total_seconds(), 0.0)
        OUTBOX_LAG.set(lag)
        return lag

    def run(self, stop: threading.Event, poll_interval_seconds: float) -> None:
        while not stop.is_set():
            try:
                published = self.publish_batch()
            except Exception:
                logger.exception("Outbox publish failed; retrying")
                published = 0
            try:
                self.measure_lag()
            except Exception:
                logger.exception("Could not measure outbox lag")
            if published:
                logger.info("Published %d outbox events", published)
            elif stop.wait(poll_interval_seconds):
                break


def main() -> None:
    configure_logging("outbox-relay")
    settings = get_settings()
    configure_tracing(
        "outbox-relay",
        enabled=settings.otel_enabled,
        otlp_endpoint=settings.otel_exporter_otlp_endpoint,
    )
    start_http_server(settings.relay_metrics_port)
    ensure_topic(settings.kafka_bootstrap_servers, settings.transfer_events_topic)
    relay = OutboxRelay(
        get_sessionmaker(),
        KafkaPublisher(settings.kafka_bootstrap_servers),
        topic=settings.transfer_events_topic,
        batch_size=settings.outbox_batch_size,
    )
    stop = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    logger.info(
        "Outbox relay started, publishing to %s, metrics on :%d",
        settings.transfer_events_topic,
        settings.relay_metrics_port,
    )
    relay.run(stop, settings.outbox_poll_interval_seconds)
    shutdown_tracing()


if __name__ == "__main__":
    main()
