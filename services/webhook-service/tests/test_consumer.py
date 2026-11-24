import json
import uuid
from collections.abc import Iterator

import pytest
from confluent_kafka import Producer, TopicPartition
from helpers import subscribe, transfer_event
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from testcontainers.community.kafka import KafkaContainer

from webhook_service.consumer import EventConsumer, build_consumer
from webhook_service.db import get_sessionmaker
from webhook_service.kafka import ensure_topic
from webhook_service.models import WebhookDelivery

TOPIC = "transfers.events"


@pytest.fixture(scope="module")
def kafka_bootstrap() -> Iterator[str]:
    with KafkaContainer("confluentinc/cp-kafka:7.6.0").with_kraft() as kafka:
        bootstrap = kafka.get_bootstrap_server()
        ensure_topic(bootstrap, TOPIC)
        yield bootstrap


def produce(bootstrap: str, values: list[bytes], key: str, topic: str = TOPIC) -> None:
    producer = Producer({"bootstrap.servers": bootstrap})
    for value in values:
        producer.produce(topic, key=key.encode(), value=value)
    assert producer.flush(10) == 0


def process(consumer: EventConsumer, messages: int, attempts: int = 30) -> None:
    handled = 0
    for _ in range(attempts):
        if consumer.process_one(timeout_seconds=1.0):
            handled += 1
        if handled == messages:
            return
    raise AssertionError(f"only {handled} of {messages} messages arrived")


def test_consumer_dedupes_redelivery_and_commits_offsets(db_session: Session, kafka_bootstrap: str):
    account = uuid.uuid4()
    subscribe(db_session, account)
    event = json.dumps(transfer_event(account, uuid.uuid4())).encode()
    # The same event twice (as after a relay retry), plus a malformed message.
    produce(kafka_bootstrap, [event, event, b"not json"], key=str(account))

    group = f"webhook-test-{uuid.uuid4()}"
    kafka_consumer = build_consumer(kafka_bootstrap, group, TOPIC)
    try:
        process(EventConsumer(kafka_consumer, get_sessionmaker()), messages=3)
        partition = kafka_consumer.position(kafka_consumer.assignment())
        committed = kafka_consumer.committed(kafka_consumer.assignment(), timeout=10)
    finally:
        kafka_consumer.close()

    deliveries = db_session.scalar(select(func.count()).select_from(WebhookDelivery))
    assert deliveries == 1
    # Every message, including the malformed one, has its offset committed.
    committed_total = sum(tp.offset for tp in committed if tp.offset >= 0)
    assert committed_total == sum(tp.offset for tp in partition if tp.offset >= 0) == 3
    assert all(isinstance(tp, TopicPartition) for tp in committed)


def test_database_failure_rewinds_so_the_message_is_not_skipped(
    db_session: Session, kafka_bootstrap: str
):
    topic = f"rewind-{uuid.uuid4()}"
    ensure_topic(kafka_bootstrap, topic, partitions=1)
    account = uuid.uuid4()
    subscribe(db_session, account)
    event = json.dumps(transfer_event(account, uuid.uuid4())).encode()
    produce(kafka_bootstrap, [event], key=str(account), topic=topic)
    calls = {"count": 0}

    def flaky_sessions():
        calls["count"] += 1
        if calls["count"] == 1:
            raise RuntimeError("database unavailable")
        return get_sessionmaker()()

    kafka_consumer = build_consumer(kafka_bootstrap, f"webhook-test-{uuid.uuid4()}", topic)
    consumer = EventConsumer(kafka_consumer, flaky_sessions)
    try:
        with pytest.raises(RuntimeError):
            for _ in range(30):
                consumer.process_one(timeout_seconds=1.0)
        # The failed message is read again, not skipped.
        process(consumer, messages=1)
    finally:
        kafka_consumer.close()

    assert calls["count"] == 2
    assert db_session.scalar(select(func.count()).select_from(WebhookDelivery)) == 1
