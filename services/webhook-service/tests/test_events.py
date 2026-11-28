import uuid

import pytest
from helpers import subscribe, transfer_event
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from webhook_service.db import get_sessionmaker
from webhook_service.events import InvalidEvent, handle_event
from webhook_service.models import ProcessedEvent, WebhookDelivery


def count(session: Session, model) -> int:
    return session.scalar(select(func.count()).select_from(model))


def test_event_creates_a_delivery_for_each_subscribed_account(db_session: Session):
    alice, bob, carol = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    subscribe(db_session, alice)
    subscribe(db_session, bob)
    subscribe(db_session, carol)  # not part of the transfer
    event = transfer_event(alice, bob)

    with get_sessionmaker()() as session:
        assert handle_event(session, event) == 2

    deliveries = db_session.scalars(select(WebhookDelivery)).all()
    assert len(deliveries) == 2
    assert {d.status for d in deliveries} == {"PENDING"}
    assert all(d.payload == event for d in deliveries)


def test_redelivered_event_is_ignored(db_session: Session):
    alice, bob = uuid.uuid4(), uuid.uuid4()
    subscribe(db_session, alice)
    event = transfer_event(alice, bob)

    with get_sessionmaker()() as session:
        assert handle_event(session, event) == 1
    with get_sessionmaker()() as session:
        assert handle_event(session, event) is None

    assert count(db_session, WebhookDelivery) == 1
    assert count(db_session, ProcessedEvent) == 1


def test_event_without_subscribers_is_still_marked_processed(db_session: Session):
    with get_sessionmaker()() as session:
        assert handle_event(session, transfer_event(uuid.uuid4(), uuid.uuid4())) == 0

    assert count(db_session, ProcessedEvent) == 1
    assert count(db_session, WebhookDelivery) == 0


@pytest.mark.parametrize(
    "event",
    [
        {"event_type": "TransferCompleted", "data": {}},
        {"event_id": "not-a-uuid", "event_type": "TransferCompleted", "data": {}},
        {"event_id": str(uuid.uuid4()), "event_type": "TransferCompleted", "data": {}},
        {"event_id": str(uuid.uuid4()), "event_type": "TransferCompleted"},
    ],
)
def test_malformed_events_are_rejected(db_session: Session, event: dict):
    with pytest.raises(InvalidEvent):
        handle_event(db_session, event)
