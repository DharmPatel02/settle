import logging
from dataclasses import dataclass
from typing import Protocol

from confluent_kafka import KafkaError, KafkaException, Message, Producer
from confluent_kafka.admin import AdminClient, NewTopic

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class OutgoingMessage:
    key: str
    value: bytes
    headers: dict[str, str]


class PublishError(Exception):
    """At least one message was not acknowledged by Kafka."""


class EventPublisher(Protocol):
    def publish(self, topic: str, messages: list[OutgoingMessage]) -> None: ...


class KafkaPublisher:
    """Publishes a batch and only returns once Kafka has acknowledged every message."""

    def __init__(self, bootstrap_servers: str, *, flush_timeout_seconds: float = 10.0) -> None:
        self.flush_timeout_seconds = flush_timeout_seconds
        self.producer = Producer(
            {
                "bootstrap.servers": bootstrap_servers,
                # Wait for all in-sync replicas, and have the broker deduplicate the
                # producer's internal retries so one produce call is written once.
                "acks": "all",
                "enable.idempotence": True,
            }
        )

    def publish(self, topic: str, messages: list[OutgoingMessage]) -> None:
        errors: list[KafkaError] = []

        def on_delivery(error: KafkaError | None, _message: Message) -> None:
            if error is not None:
                errors.append(error)

        for message in messages:
            self.producer.produce(
                topic,
                key=message.key.encode(),
                value=message.value,
                headers=list(message.headers.items()),
                on_delivery=on_delivery,
            )
        remaining = self.producer.flush(self.flush_timeout_seconds)
        if remaining or errors:
            raise PublishError(
                f"{remaining} messages unacknowledged, {len(errors)} failed: {errors[:1]}"
            )


def ensure_topic(bootstrap_servers: str, topic: str, *, partitions: int = 3) -> None:
    """Create the topic if it does not exist yet. Safe to call on every start."""
    admin = AdminClient({"bootstrap.servers": bootstrap_servers})
    futures = admin.create_topics(
        [NewTopic(topic, num_partitions=partitions, replication_factor=1)]
    )
    try:
        futures[topic].result(timeout=10)
        logger.info("Created topic %s with %d partitions", topic, partitions)
    except KafkaException as exc:
        if exc.args[0].code() != KafkaError.TOPIC_ALREADY_EXISTS:
            raise
