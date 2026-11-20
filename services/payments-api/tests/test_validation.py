import uuid

import pytest
from pydantic import ValidationError

from payments_api.schemas import AccountCreate, TransferCreate
from payments_api.services import request_hash


def test_transfer_rejects_nonpositive_amount():
    with pytest.raises(ValidationError):
        TransferCreate(
            from_account_id=uuid.uuid4(),
            to_account_id=uuid.uuid4(),
            amount_minor=0,
            currency="USD",
        )


def test_account_rejects_invalid_currency():
    with pytest.raises(ValidationError):
        AccountCreate(owner_name="Alice", currency="usd")


def test_request_hash_is_stable_and_body_sensitive():
    base = TransferCreate(
        from_account_id=uuid.uuid4(),
        to_account_id=uuid.uuid4(),
        amount_minor=100,
        currency="USD",
    )
    same = TransferCreate(**base.model_dump())
    changed = base.model_copy(update={"amount_minor": 101})

    assert request_hash(base) == request_hash(same)
    assert request_hash(base) != request_hash(changed)
