import secrets
import uuid
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status
from fastapi.security import APIKeyHeader
from sqlalchemy import select
from sqlalchemy.orm import Session

from webhook_service.config import get_settings
from webhook_service.db import get_session
from webhook_service.errors import Problem
from webhook_service.models import DEAD, PENDING, WebhookDelivery, WebhookSubscription
from webhook_service.schemas import (
    DeliveryResponse,
    SubscriptionCreate,
    SubscriptionCreated,
    SubscriptionResponse,
)

admin_token_header = APIKeyHeader(name="X-Admin-Token", auto_error=False)


def require_admin(token: Annotated[str | None, Depends(admin_token_header)]) -> None:
    expected = get_settings().admin_token
    # compare_digest takes the same time whether the first or last character differs.
    if token is None or not secrets.compare_digest(token.encode(), expected.encode()):
        raise Problem(401, "Admin token required", "Provide a valid X-Admin-Token header.")


router = APIRouter(prefix="/admin", tags=["admin"], dependencies=[Depends(require_admin)])
SessionDep = Annotated[Session, Depends(get_session)]


@router.post(
    "/subscriptions", response_model=SubscriptionCreated, status_code=status.HTTP_201_CREATED
)
def create_subscription(request: SubscriptionCreate, session: SessionDep):
    with session.begin():
        subscription = WebhookSubscription(
            account_id=request.account_id,
            url=str(request.url),
            secret=request.secret or secrets.token_urlsafe(32),
        )
        session.add(subscription)
        session.flush()
    return subscription


@router.get("/subscriptions", response_model=list[SubscriptionResponse])
def list_subscriptions(session: SessionDep, account_id: uuid.UUID | None = None):
    statement = select(WebhookSubscription).order_by(WebhookSubscription.created_at)
    if account_id is not None:
        statement = statement.where(WebhookSubscription.account_id == account_id)
    return session.scalars(statement).all()


@router.get("/deliveries", response_model=list[DeliveryResponse])
def list_deliveries(
    session: SessionDep,
    delivery_status: Annotated[str | None, Query(alias="status")] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
):
    statement = select(WebhookDelivery).order_by(WebhookDelivery.created_at.desc()).limit(limit)
    if delivery_status is not None:
        statement = statement.where(WebhookDelivery.status == delivery_status.upper())
    return session.scalars(statement).all()


@router.get("/dlq", response_model=list[DeliveryResponse])
def list_dead_letters(session: SessionDep, limit: Annotated[int, Query(ge=1, le=100)] = 50):
    return session.scalars(
        select(WebhookDelivery)
        .where(WebhookDelivery.status == DEAD)
        .order_by(WebhookDelivery.updated_at.desc())
        .limit(limit)
    ).all()


@router.post("/dlq/{delivery_id}/replay", response_model=DeliveryResponse)
def replay_dead_letter(delivery_id: uuid.UUID, session: SessionDep):
    """Give a DEAD delivery a fresh set of attempts, for example after the merchant is fixed."""
    with session.begin():
        delivery = session.get(WebhookDelivery, delivery_id, with_for_update=True)
        if delivery is None:
            raise Problem(404, "Delivery not found", f"Delivery {delivery_id} does not exist.")
        if delivery.status != DEAD:
            raise Problem(
                409,
                "Delivery is not dead",
                f"Only DEAD deliveries can be replayed, not {delivery.status}.",
            )
        delivery.status = PENDING
        delivery.attempts = 0
        delivery.next_attempt_at = datetime.now(UTC)
        delivery.last_error = None
        # Cleared so that if it dies again, the new failure reaches the DLQ topic too.
        delivery.dlq_published_at = None
    return delivery
