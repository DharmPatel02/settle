import random
import uuid
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from payments_api.constants import EXTERNAL_FUNDING_ID
from payments_api.db import get_sessionmaker
from payments_api.models import Account, Transfer
from payments_api.schemas import TransferCreate
from payments_api.services import OperationResult, TransferService


def create_accounts(db_session: Session, count: int) -> list[uuid.UUID]:
    accounts = [Account(owner_name=f"Account {index}", currency="USD") for index in range(count)]
    db_session.add_all(accounts)
    db_session.commit()
    return [account.id for account in accounts]


def fund_account(account_id: uuid.UUID, amount_minor: int, key: str) -> None:
    request = TransferCreate(
        from_account_id=EXTERNAL_FUNDING_ID,
        to_account_id=account_id,
        amount_minor=amount_minor,
        currency="USD",
    )
    with get_sessionmaker()() as session:
        result = TransferService(session).create(
            request, client_id="concurrency-setup", idempotency_key=key
        )
    assert result.status_code == 201


def run_concurrent_transfer(
    barrier: Barrier,
    request: TransferCreate,
    *,
    client_id: str,
    idempotency_key: str,
) -> OperationResult:
    barrier.wait(timeout=30)
    with get_sessionmaker()() as session:
        return TransferService(session).create(
            request,
            client_id=client_id,
            idempotency_key=idempotency_key,
        )


def test_double_spend_allows_only_available_balance(db_session: Session):
    source_id, destination_id = create_accounts(db_session, 2)
    fund_account(source_id, 100, "fund-double-spend")
    request = TransferCreate(
        from_account_id=source_id,
        to_account_id=destination_id,
        amount_minor=10,
        currency="USD",
    )
    barrier = Barrier(50)

    with ThreadPoolExecutor(max_workers=50) as executor:
        futures = [
            executor.submit(
                run_concurrent_transfer,
                barrier,
                request,
                client_id="double-spend",
                idempotency_key=f"attempt-{index}",
            )
            for index in range(50)
        ]
        results = [future.result(timeout=60) for future in futures]

    assert sum(result.status_code == 201 for result in results) == 10
    assert sum(result.status_code == 422 for result in results) == 40
    db_session.expire_all()
    assert db_session.get(Account, source_id).balance_minor == 0
    assert db_session.get(Account, destination_id).balance_minor == 100


def test_consistent_lock_order_avoids_deadlocks(db_session: Session):
    account_ids = create_accounts(db_session, 4)
    for index, account_id in enumerate(account_ids):
        fund_account(account_id, 100, f"fund-deadlock-{index}")

    generator = random.Random(20261121)
    pairs = [generator.sample(account_ids, 2) for _ in range(40)]
    barrier = Barrier(len(pairs))

    with ThreadPoolExecutor(max_workers=len(pairs)) as executor:
        futures = [
            executor.submit(
                run_concurrent_transfer,
                barrier,
                TransferCreate(
                    from_account_id=source_id,
                    to_account_id=destination_id,
                    amount_minor=1,
                    currency="USD",
                ),
                client_id="deadlock-test",
                idempotency_key=f"transfer-{index}",
            )
            for index, (source_id, destination_id) in enumerate(pairs)
        ]
        results = [future.result(timeout=60) for future in futures]

    assert all(result.status_code == 201 for result in results)
    db_session.expire_all()
    assert (
        sum(db_session.get(Account, account_id).balance_minor for account_id in account_ids) == 400
    )


def test_duplicate_key_race_creates_one_transfer(db_session: Session):
    source_id, destination_id = create_accounts(db_session, 2)
    fund_account(source_id, 100, "fund-duplicate-key")
    request = TransferCreate(
        from_account_id=source_id,
        to_account_id=destination_id,
        amount_minor=10,
        currency="USD",
    )
    barrier = Barrier(20)

    with ThreadPoolExecutor(max_workers=20) as executor:
        futures = [
            executor.submit(
                run_concurrent_transfer,
                barrier,
                request,
                client_id="duplicate-key-test",
                idempotency_key="same-key",
            )
            for _ in range(20)
        ]
        results = [future.result(timeout=60) for future in futures]

    assert all(result.status_code == 201 for result in results)
    assert len({result.body["id"] for result in results}) == 1
    transfer_count = db_session.scalar(
        select(func.count())
        .select_from(Transfer)
        .where(
            Transfer.from_account_id == source_id,
            Transfer.to_account_id == destination_id,
        )
    )
    assert transfer_count == 1
