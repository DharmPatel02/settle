import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from payments_api.errors import Problem
from payments_api.models import Account, IdempotencyKey, LedgerEntry, Transfer
from payments_api.outbox import (
    TRANSFER_COMPLETED,
    TRANSFER_DECLINED,
    TRANSFER_PENDING_REVIEW,
    TRANSFER_REJECTED,
    transfer_event,
)
from payments_api.repositories import TransferRepository
from payments_api.risk import RiskAssessment, RiskDecision, RiskScorer
from payments_api.schemas import TransferCreate, TransferResponse


@dataclass(frozen=True, slots=True)
class OperationResult:
    status_code: int
    body: dict[str, object]


def request_hash(request: BaseModel) -> str:
    canonical = json.dumps(
        request.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
    ).encode()
    return hashlib.sha256(canonical).hexdigest()


def _score_value(assessment: RiskAssessment | None) -> Decimal | None:
    if assessment is None or assessment.score is None:
        return None
    return Decimal(f"{assessment.score:.6f}")


class TransferService:
    def __init__(self, session: Session, risk: RiskScorer | None = None) -> None:
        """`risk` is None for deposits, which come from outside and are not risk-scored."""
        self.session = session
        self.transfers = TransferRepository(session)
        self.risk = risk

    def create(
        self, request: TransferCreate, *, client_id: str, idempotency_key: str
    ) -> OperationResult:
        hashed_request = request_hash(request)
        # Rule: no network calls while holding locks. The risk call finishes before the
        # locking transaction below begins, so a slow risk-service cannot block other transfers.
        assessment = self._assess(request, client_id, idempotency_key)

        with self.session.begin():
            replay = self._claim_key(client_id, idempotency_key, hashed_request)
            if replay is not None:
                return replay

            if request.from_account_id == request.to_account_id:
                return self._complete_problem(
                    client_id,
                    idempotency_key,
                    Problem(400, "Invalid transfer", "Source and destination must be different."),
                )

            accounts = self.transfers.lock_accounts(
                {request.from_account_id, request.to_account_id}
            )
            missing = [
                str(account_id)
                for account_id in (request.from_account_id, request.to_account_id)
                if account_id not in accounts
            ]
            if missing:
                return self._complete_problem(
                    client_id,
                    idempotency_key,
                    Problem(404, "Account not found", f"Account {missing[0]} does not exist."),
                )

            source = accounts[request.from_account_id]
            destination = accounts[request.to_account_id]
            if source.currency != destination.currency or source.currency != request.currency:
                return self._reject(
                    request,
                    client_id,
                    idempotency_key,
                    "CURRENCY_MISMATCH",
                    Problem(422, "Currency mismatch", "All transfer currencies must match."),
                    assessment,
                )
            if not self._has_funds(source, request.amount_minor):
                return self._reject(
                    request,
                    client_id,
                    idempotency_key,
                    "INSUFFICIENT_FUNDS",
                    Problem(
                        422, "Insufficient funds", "The source account has insufficient funds."
                    ),
                    assessment,
                )

            if assessment is not None and assessment.decision == RiskDecision.DECLINE:
                return self._reject(
                    request,
                    client_id,
                    idempotency_key,
                    "RISK_DECLINED",
                    Problem(422, "Transfer declined", "The transfer was declined by risk checks."),
                    assessment,
                    status="DECLINED",
                    event_type=TRANSFER_DECLINED,
                )
            if assessment is not None and assessment.decision == RiskDecision.REVIEW:
                # Held for a person to approve. No money moves and no funds are reserved;
                # the balance is checked again at approval time.
                transfer = self._new_transfer(request, "PENDING_REVIEW", assessment)
                self.session.add(transfer_event(transfer, TRANSFER_PENDING_REVIEW))
                body = TransferResponse.model_validate(transfer).model_dump(mode="json")
                self._complete_key(client_id, idempotency_key, 202, body)
                return OperationResult(202, body)

            transfer = self._new_transfer(request, "COMPLETED", assessment)
            self._move_money(transfer, source, destination)
            body = TransferResponse.model_validate(transfer).model_dump(mode="json")
            self._complete_key(client_id, idempotency_key, 201, body)
            return OperationResult(201, body)

    def approve(self, transfer_id: uuid.UUID) -> OperationResult:
        """Execute a held transfer through the same locked path as a normal one."""
        with self.session.begin():
            transfer = self._lock_pending(transfer_id)
            # Transfer row first, then accounts in UUID order. Transfer creation never locks
            # transfer rows, so this order cannot form a deadlock cycle with it.
            accounts = self.transfers.lock_accounts(
                {transfer.from_account_id, transfer.to_account_id}
            )
            source = accounts[transfer.from_account_id]
            destination = accounts[transfer.to_account_id]
            if not self._has_funds(source, transfer.amount_minor):
                transfer.status = "REJECTED"
                transfer.failure_reason = "INSUFFICIENT_FUNDS"
                self.session.flush()
                self.session.add(transfer_event(transfer, TRANSFER_REJECTED))
                body = Problem(
                    422, "Insufficient funds", "The source account has insufficient funds."
                ).body()
                body["transfer_id"] = str(transfer.id)
                return OperationResult(422, body)

            transfer.status = "COMPLETED"
            self._move_money(transfer, source, destination)
            return OperationResult(
                200, TransferResponse.model_validate(transfer).model_dump(mode="json")
            )

    def decline(self, transfer_id: uuid.UUID) -> OperationResult:
        with self.session.begin():
            transfer = self._lock_pending(transfer_id)
            transfer.status = "DECLINED"
            transfer.failure_reason = "REVIEW_DECLINED"
            self.session.flush()
            self.session.add(transfer_event(transfer, TRANSFER_DECLINED))
            return OperationResult(
                200, TransferResponse.model_validate(transfer).model_dump(mode="json")
            )

    def pending_review(self, limit: int) -> list[Transfer]:
        return self.transfers.list_by_status("PENDING_REVIEW", limit)

    def get(self, transfer_id: uuid.UUID) -> Transfer:
        transfer = self.transfers.get(transfer_id)
        if transfer is None:
            raise Problem(404, "Transfer not found", f"Transfer {transfer_id} does not exist.")
        return transfer

    def _assess(
        self, request: TransferCreate, client_id: str, idempotency_key: str
    ) -> RiskAssessment | None:
        if self.risk is None or request.from_account_id == request.to_account_id:
            return None
        with self.session.begin():
            # A replay returns the stored answer, so it does not need a new score.
            if self.session.get(IdempotencyKey, (client_id, idempotency_key)) is not None:
                return None
            features = self.transfers.risk_features(
                request.from_account_id,
                request.to_account_id,
                request.amount_minor,
                datetime.now(UTC),
            )
        # The short read-only transaction has ended, so no connection or lock is held here.
        if features is None:
            return None  # unknown account: the main transaction answers 404
        return self.risk.assess(features)

    def _lock_pending(self, transfer_id: uuid.UUID) -> Transfer:
        transfer = self.transfers.lock(transfer_id)
        if transfer is None:
            raise Problem(404, "Transfer not found", f"Transfer {transfer_id} does not exist.")
        if transfer.status != "PENDING_REVIEW":
            raise Problem(
                409,
                "Transfer not pending review",
                f"Transfer {transfer_id} is {transfer.status}, not PENDING_REVIEW.",
            )
        return transfer

    @staticmethod
    def _has_funds(source: Account, amount_minor: int) -> bool:
        return source.allow_negative or source.balance_minor >= amount_minor

    def _new_transfer(
        self, request: TransferCreate, status: str, assessment: RiskAssessment | None
    ) -> Transfer:
        transfer = Transfer(
            from_account_id=request.from_account_id,
            to_account_id=request.to_account_id,
            amount_minor=request.amount_minor,
            currency=request.currency,
            status=status,
            risk_score=_score_value(assessment),
            risk_model_version=assessment.model_version if assessment else None,
        )
        self.session.add(transfer)
        self.session.flush()
        return transfer

    def _move_money(self, transfer: Transfer, source: Account, destination: Account) -> None:
        """Write both ledger entries, update cached balances, and queue the completed event."""
        self.session.add_all(
            [
                LedgerEntry(
                    transfer_id=transfer.id,
                    account_id=source.id,
                    amount_minor=-transfer.amount_minor,
                ),
                LedgerEntry(
                    transfer_id=transfer.id,
                    account_id=destination.id,
                    amount_minor=transfer.amount_minor,
                ),
            ]
        )
        source.balance_minor -= transfer.amount_minor
        destination.balance_minor += transfer.amount_minor
        now = datetime.now(UTC)
        source.updated_at = now
        destination.updated_at = now
        self.session.flush()
        # Same transaction as the money movement: the event exists if and only if
        # the transfer committed. The outbox relay publishes it to Kafka later.
        self.session.add(transfer_event(transfer, TRANSFER_COMPLETED))
        self.session.flush()

    def _claim_key(
        self, client_id: str, idempotency_key: str, hashed_request: str
    ) -> OperationResult | None:
        statement = (
            insert(IdempotencyKey)
            .values(
                client_id=client_id,
                idem_key=idempotency_key,
                request_hash=hashed_request,
                status="IN_PROGRESS",
                expires_at=datetime.now(UTC) + timedelta(hours=24),
            )
            .on_conflict_do_nothing()
            .returning(IdempotencyKey.client_id)
        )
        inserted = self.session.scalar(statement)
        if inserted is not None:
            return None

        existing = self.session.scalar(
            select(IdempotencyKey).where(
                IdempotencyKey.client_id == client_id,
                IdempotencyKey.idem_key == idempotency_key,
            )
        )
        if existing is None:
            raise RuntimeError("Idempotency key disappeared after a conflict")
        if existing.request_hash != hashed_request:
            problem = Problem(
                422,
                "Idempotency key reused",
                "This key was already used with a different request body.",
            )
            return OperationResult(problem.status, problem.body())
        if existing.status == "COMPLETED":
            if existing.response_code is None or existing.response_body is None:
                raise RuntimeError("Completed idempotency key has no stored response")
            return OperationResult(existing.response_code, existing.response_body)
        problem = Problem(409, "Request in progress", "A request with this key is still running.")
        return OperationResult(problem.status, problem.body())

    def _reject(
        self,
        request: TransferCreate,
        client_id: str,
        idempotency_key: str,
        reason: str,
        problem: Problem,
        assessment: RiskAssessment | None,
        *,
        status: str = "REJECTED",
        event_type: str = TRANSFER_REJECTED,
    ) -> OperationResult:
        """Record a failed transfer (REJECTED or DECLINED) with no ledger entries."""
        transfer = self._new_transfer(request, status, assessment)
        transfer.failure_reason = reason
        self.session.flush()
        self.session.add(transfer_event(transfer, event_type))
        body = problem.body()
        body["transfer_id"] = str(transfer.id)
        self._complete_key(client_id, idempotency_key, problem.status, body)
        return OperationResult(problem.status, body)

    def _complete_problem(
        self, client_id: str, idempotency_key: str, problem: Problem
    ) -> OperationResult:
        body = problem.body()
        self._complete_key(client_id, idempotency_key, problem.status, body)
        return OperationResult(problem.status, body)

    def _complete_key(
        self,
        client_id: str,
        idempotency_key: str,
        response_code: int,
        response_body: dict[str, object],
    ) -> None:
        key = self.session.get(IdempotencyKey, (client_id, idempotency_key))
        if key is None:
            raise RuntimeError("Claimed idempotency key could not be loaded")
        key.status = "COMPLETED"
        key.response_code = response_code
        key.response_body = response_body
