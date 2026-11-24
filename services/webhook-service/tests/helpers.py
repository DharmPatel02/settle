import uuid

from sqlalchemy.orm import Session

from webhook_service.models import WebhookSubscription


def transfer_event(from_account: uuid.UUID, to_account: uuid.UUID) -> dict[str, object]:
    transfer_id = uuid.uuid4()
    return {
        "event_id": str(uuid.uuid4()),
        "event_type": "TransferCompleted",
        "aggregate_type": "transfer",
        "aggregate_id": str(transfer_id),
        "occurred_at": "2026-11-24T12:00:00+00:00",
        "data": {
            "id": str(transfer_id),
            "from_account_id": str(from_account),
            "to_account_id": str(to_account),
            "amount_minor": 125,
            "currency": "USD",
            "status": "COMPLETED",
        },
    }


def subscribe(
    session: Session, account_id: uuid.UUID, url: str = "https://merchant.test/webhooks"
) -> WebhookSubscription:
    subscription = WebhookSubscription(
        account_id=account_id, url=url, secret="merchant-secret-123456"
    )
    session.add(subscription)
    session.commit()
    return subscription
