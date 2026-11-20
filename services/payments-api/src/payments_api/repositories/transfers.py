import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from payments_api.models import Account, Transfer


class TransferRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def get(self, transfer_id: uuid.UUID) -> Transfer | None:
        return self.session.get(Transfer, transfer_id)

    def lock_accounts(self, account_ids: set[uuid.UUID]) -> dict[uuid.UUID, Account]:
        # Every transaction asks Postgres for locks in UUID order. A consistent
        # ordering prevents A→B and B→A transfers from deadlocking each other.
        statement = (
            select(Account)
            .where(Account.id.in_(account_ids))
            .order_by(Account.id)
            .with_for_update()
        )
        return {account.id: account for account in self.session.scalars(statement)}
