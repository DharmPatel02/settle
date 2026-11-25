import uuid
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from payments_api.models import Account, Transfer
from payments_api.risk import RiskFeatures


class TransferRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def get(self, transfer_id: uuid.UUID) -> Transfer | None:
        return self.session.get(Transfer, transfer_id)

    def lock(self, transfer_id: uuid.UUID) -> Transfer | None:
        # Serialises admin decisions on one transfer: a second approve waits, then sees
        # the new status instead of moving the money twice.
        return self.session.scalar(
            select(Transfer).where(Transfer.id == transfer_id).with_for_update()
        )

    def list_by_status(self, status: str, limit: int) -> list[Transfer]:
        statement = (
            select(Transfer)
            .where(Transfer.status == status)
            .order_by(Transfer.created_at, Transfer.id)
            .limit(limit)
        )
        return list(self.session.scalars(statement))

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

    def risk_features(
        self,
        from_account_id: uuid.UUID,
        to_account_id: uuid.UUID,
        amount_minor: int,
        now: datetime,
    ) -> RiskFeatures | None:
        """Features for the risk model, read without locks. None if an account is missing."""
        source = self.session.get(Account, from_account_id)
        if source is None or self.session.get(Account, to_account_id) is None:
            return None

        # Only money that actually moved counts as history.
        completed = (Transfer.from_account_id == from_account_id, Transfer.status == "COMPLETED")
        transfers_last_hour = self.session.scalar(
            select(func.count())
            .select_from(Transfer)
            .where(*completed, Transfer.created_at >= now - timedelta(hours=1))
        )
        average = self.session.scalar(
            select(func.avg(Transfer.amount_minor)).where(
                *completed, Transfer.created_at >= now - timedelta(days=30)
            )
        )
        paid_before = self.session.scalar(
            select(Transfer.id).where(*completed, Transfer.to_account_id == to_account_id).limit(1)
        )
        return RiskFeatures(
            amount_minor=amount_minor,
            hour_of_day=now.hour,
            account_age_days=max((now - source.created_at).total_seconds(), 0) / 86_400,
            transfers_last_hour=transfers_last_hour or 0,
            avg_amount_30d_minor=round(average) if average is not None else 0,
            is_new_recipient=paid_before is None,
        )
