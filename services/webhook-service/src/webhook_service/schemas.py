import uuid
from datetime import datetime
from typing import Annotated

from pydantic import AnyHttpUrl, BaseModel, ConfigDict, Field


class SubscriptionCreate(BaseModel):
    account_id: uuid.UUID
    url: AnyHttpUrl
    # Optional so a merchant can bring its own secret; otherwise one is generated.
    secret: Annotated[str | None, Field(min_length=16, max_length=200)] = None


class SubscriptionResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    account_id: uuid.UUID
    url: str
    created_at: datetime


class SubscriptionCreated(SubscriptionResponse):
    # Returned once, at creation. The merchant needs it to verify signatures.
    secret: str


class DeliveryResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    event_id: uuid.UUID
    subscription_id: uuid.UUID
    url: str
    status: str
    attempts: int
    next_attempt_at: datetime
    last_error: str | None
    delivered_at: datetime | None
    dlq_published_at: datetime | None
    created_at: datetime
