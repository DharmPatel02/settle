from payments_api.models import OutboxEvent, Transfer
from payments_api.schemas import TransferResponse
from payments_api.telemetry import inject_context

TRANSFER_COMPLETED = "TransferCompleted"
TRANSFER_REJECTED = "TransferRejected"
TRANSFER_DECLINED = "TransferDeclined"
TRANSFER_PENDING_REVIEW = "TransferPendingReview"


def transfer_event(transfer: Transfer, event_type: str) -> OutboxEvent:
    """Build the outbox row for a transfer. The caller adds it in the same transaction."""
    return OutboxEvent(
        aggregate_type="transfer",
        aggregate_id=transfer.id,
        event_type=event_type,
        partition_key=str(transfer.from_account_id),
        payload=TransferResponse.model_validate(transfer).model_dump(mode="json"),
        trace_context=inject_context() or None,
    )


def event_envelope(event: OutboxEvent) -> dict[str, object]:
    """The message published to Kafka. `event_id` lets consumers deduplicate redeliveries."""
    return {
        "event_id": str(event.id),
        "event_type": event.event_type,
        "aggregate_type": event.aggregate_type,
        "aggregate_id": str(event.aggregate_id),
        "occurred_at": event.created_at.isoformat(),
        "data": event.payload,
    }
