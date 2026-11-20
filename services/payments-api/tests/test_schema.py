import uuid

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from payments_api.models import Account, LedgerEntry, Transfer


def test_database_rejects_negative_balance(db_session: Session):
    account = Account(owner_name="Alice", currency="USD", balance_minor=-1)
    db_session.add(account)

    with pytest.raises(IntegrityError):
        db_session.flush()
    db_session.rollback()


def test_database_rejects_zero_ledger_entry(db_session: Session):
    source = Account(owner_name="Source", currency="USD")
    destination = Account(owner_name="Destination", currency="USD")
    db_session.add_all([source, destination])
    db_session.flush()
    transfer = Transfer(
        id=uuid.uuid4(),
        from_account_id=source.id,
        to_account_id=destination.id,
        amount_minor=1,
        currency="USD",
        status="COMPLETED",
    )
    db_session.add(transfer)
    db_session.flush()
    db_session.add(
        LedgerEntry(
            transfer_id=transfer.id,
            account_id=destination.id,
            amount_minor=0,
        )
    )

    with pytest.raises(IntegrityError):
        db_session.flush()
    db_session.rollback()
