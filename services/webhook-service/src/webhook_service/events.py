import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from webhook_service.models import PENDING, ProcessedEvent, WebhookDelivery, WebhookSubscription


class InvalidEvent(ValueError):
    """The message is not a transfer event this service understands."""


def event_accounts(event: dict[str, object]) -> set[uuid.UUID]:
    data = event.get("data")
    if not isinstance(data, dict):
        raise InvalidEvent("event has no data object")
    try:
        return {uuid.UUID(str(data["from_account_id"])), uuid.UUID(str(data["to_account_id"]))}
    except (KeyError, ValueError) as exc:
        raise InvalidEvent("event data is missing account ids") from exc


def handle_event(
    session: Session, event: dict[str, object], trace_context: dict[str, str] | None = None
) -> int | None:
    """Record the event and queue one delivery per matching subscription.

    Returns the number of deliveries created, or None for an event already processed. The dedupe
    row and the deliveries commit together, so a crash leaves either both or neither.
    """
    try:
        event_id = uuid.UUID(str(event["event_id"]))
        event_type = str(event["event_type"])
    except (KeyError, ValueError) as exc:
        raise InvalidEvent("event has no valid event_id or event_type") from exc
    accounts = event_accounts(event)

    with session.begin():
        first_time = session.scalar(
            insert(ProcessedEvent)
            .values(event_id=event_id, event_type=event_type)
            .on_conflict_do_nothing()
            .returning(ProcessedEvent.event_id)
        )
        if first_time is None:
            return None

        subscriptions = session.scalars(
            select(WebhookSubscription).where(WebhookSubscription.account_id.in_(accounts))
        ).all()
        now = datetime.now(UTC)
        session.add_all(
            WebhookDelivery(
                event_id=event_id,
                subscription_id=subscription.id,
                url=subscription.url,
                payload=event,
                trace_context=trace_context or None,
                status=PENDING,
                attempts=0,
                next_attempt_at=now,
            )
            for subscription in subscriptions
        )
        return len(subscriptions)
