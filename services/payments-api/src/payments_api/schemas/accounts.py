import uuid
from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

Currency = Annotated[str, StringConstraints(pattern=r"^[A-Z]{3}$")]


class AccountCreate(BaseModel):
    owner_name: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)
    ]
    currency: Currency


class AccountResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    owner_name: str
    currency: str
    balance_minor: int
    created_at: datetime
    updated_at: datetime


class LedgerEntryResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    transfer_id: uuid.UUID
    account_id: uuid.UUID
    amount_minor: int
    created_at: datetime


class LedgerEntriesPage(BaseModel):
    items: list[LedgerEntryResponse]
    next_cursor: int | None


class DepositCreate(BaseModel):
    account_id: uuid.UUID
    amount_minor: Annotated[int, Field(gt=0)]
    currency: Currency
