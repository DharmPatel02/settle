import uuid
from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

from payments_api.schemas.accounts import Currency


class TransferCreate(BaseModel):
    from_account_id: uuid.UUID
    to_account_id: uuid.UUID
    amount_minor: Annotated[int, Field(gt=0)]
    currency: Currency


class TransferResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    from_account_id: uuid.UUID
    to_account_id: uuid.UUID
    amount_minor: int
    currency: str
    status: str
    failure_reason: str | None
    created_at: datetime
    updated_at: datetime
