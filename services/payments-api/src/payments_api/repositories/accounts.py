import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from payments_api.models import Account, LedgerEntry


class AccountRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def add(self, account: Account) -> None:
        self.session.add(account)

    def get(self, account_id: uuid.UUID) -> Account | None:
        return self.session.get(Account, account_id)

    def list_entries(
        self, account_id: uuid.UUID, cursor: int | None, limit: int
    ) -> list[LedgerEntry]:
        statement = select(LedgerEntry).where(LedgerEntry.account_id == account_id)
        if cursor is not None:
            statement = statement.where(LedgerEntry.id > cursor)
        statement = statement.order_by(LedgerEntry.id).limit(limit + 1)
        return list(self.session.scalars(statement))
