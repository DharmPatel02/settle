"""Kafka consumer: turns transfer events into pending webhook deliveries.

Run as its own process: `python -m webhook_service.consumer`.
"""

import json
import logging
import signal
import threading
from collections.abc import Callable

from confluent_kafka import Consumer, Message, TopicPartition
from sqlalchemy.orm import Session

from webhook_service.config import get_settings
from webhook_service.db import get_sessionmaker
from webhook_service.events import InvalidEvent, handle_event

logger = logging.getLogger(__name__)


class EventConsumer:
    def __init__(self, consumer: Consumer, session_factory: Callable[[], Session]) -> None:
        self.consumer = consumer
        self.session_factory = session_factory

    def process_one(self, timeout_seconds: float = 1.0) -> bool:
        """Handle at most one message. Returns True if a message was read."""
        message: Message | None = self.consumer.poll(timeout_seconds)
        if message is None:
            return False
        if message.error() is not None:
            logger.warning("Kafka error: %s", message.error())
            return False

        try:
            event = json.loads(message.value() or b"null")
            if not isinstance(event, dict):
                raise InvalidEvent("message is not a JSON object")
            with self.session_factory() as session:
                created = handle_event(session, event)
            logger.info("Event %s queued %d deliveries", event["event_id"], created)
        except ValueError:  # InvalidEvent, bad JSON, or bytes that are not UTF-8
            # A malformed message will never succeed; log it and move past it rather than
            # blocking the partition forever.
            logger.exception("Skipping malformed message at offset %s", message.offset())
        except Exception:
            # Rewind so the next poll returns this same message instead of the one after it.
            self.consumer.seek(
                TopicPartition(message.topic(), message.partition(), message.offset())
            )
            raise

        # Commit the offset only after the database commit. If we crash in between, Kafka
        # redelivers the message and processed_events turns the second copy into a no-op.
        self.consumer.commit(message=message, asynchronous=False)
        return True

    def run(self, stop: threading.Event) -> None:
        while not stop.is_set():
            try:
                self.process_one()
            except Exception:
                # Database errors land here; the message was rewound, so it is read again
                # after a short pause.
                logger.exception("Failed to process message; retrying")
                stop.wait(1.0)


def build_consumer(bootstrap_servers: str, group_id: str, topic: str) -> Consumer:
    consumer = Consumer(
        {
            "bootstrap.servers": bootstrap_servers,
            "group.id": group_id,
            # We commit offsets ourselves, after the database write.
            "enable.auto.commit": False,
            "auto.offset.reset": "earliest",
        }
    )
    consumer.subscribe([topic])
    return consumer


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    settings = get_settings()
    consumer = build_consumer(
        settings.kafka_bootstrap_servers,
        settings.consumer_group_id,
        settings.transfer_events_topic,
    )
    stop = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    logger.info("Consuming %s", settings.transfer_events_topic)
    try:
        EventConsumer(consumer, get_sessionmaker()).run(stop)
    finally:
        consumer.close()


if __name__ == "__main__":
    main()
