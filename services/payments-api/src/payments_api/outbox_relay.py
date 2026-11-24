"""Outbox relay: publishes committed outbox rows to Kafka.

Run as its own process: `python -m payments_api.outbox_relay`.
"""

import json
import logging
import signal
import threading
from collections.abc import Callable
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from payments_api.config import get_settings
from payments_api.db import get_sessionmaker
from payments_api.kafka import EventPublisher, KafkaPublisher, OutgoingMessage, ensure_topic
from payments_api.models import OutboxEvent
from payments_api.outbox import event_envelope

logger = logging.getLogger(__name__)


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

            messages = [
                OutgoingMessage(
                    key=event.partition_key,
                    value=json.dumps(event_envelope(event), separators=(",", ":")).encode(),
                    headers={"event_type": event.event_type, "event_id": str(event.id)},
                )
                for event in events
            ]
            # Raises if Kafka does not acknowledge every message; the transaction then rolls
            # back and the rows stay unpublished for the next attempt.
            self.publisher.publish(self.topic, messages)

            now = datetime.now(UTC)
            for event in events:
                event.published_at = now
            return len(events)

    def run(self, stop: threading.Event, poll_interval_seconds: float) -> None:
        while not stop.is_set():
            try:
                published = self.publish_batch()
            except Exception:
                logger.exception("Outbox publish failed; retrying")
                published = 0
            if published:
                logger.info("Published %d outbox events", published)
            elif stop.wait(poll_interval_seconds):
                break


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    settings = get_settings()
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
    logger.info("Outbox relay started, publishing to %s", settings.transfer_events_topic)
    relay.run(stop, settings.outbox_poll_interval_seconds)


if __name__ == "__main__":
    main()
