import uuid

from sqlalchemy.orm import Session

from payments_api.errors import Problem
from payments_api.models import Account
from payments_api.repositories import AccountRepository
from payments_api.schemas import AccountCreate, LedgerEntriesPage, LedgerEntryResponse


class AccountService:
    def __init__(self, session: Session) -> None:
        self.session = session
        self.accounts = AccountRepository(session)

    def create(self, request: AccountCreate) -> Account:
        with self.session.begin():
            account = Account(owner_name=request.owner_name, currency=request.currency)
            self.accounts.add(account)
            self.session.flush()
        return account

    def get(self, account_id: uuid.UUID) -> Account:
        account = self.accounts.get(account_id)
        if account is None:
            raise Problem(404, "Account not found", f"Account {account_id} does not exist.")
        return account

    def entries(self, account_id: uuid.UUID, cursor: int | None, limit: int) -> LedgerEntriesPage:
        if self.accounts.get(account_id) is None:
            raise Problem(404, "Account not found", f"Account {account_id} does not exist.")
        rows = self.accounts.list_entries(account_id, cursor, limit)
        has_more = len(rows) > limit
        page_rows = rows[:limit]
        return LedgerEntriesPage(
            items=[LedgerEntryResponse.model_validate(row) for row in page_rows],
            next_cursor=page_rows[-1].id if has_more else None,
        )
