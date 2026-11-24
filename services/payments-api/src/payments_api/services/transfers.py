import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from payments_api.errors import Problem
from payments_api.models import IdempotencyKey, LedgerEntry, Transfer
from payments_api.outbox import TRANSFER_COMPLETED, TRANSFER_REJECTED, transfer_event
from payments_api.repositories import TransferRepository
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


class TransferService:
    def __init__(self, session: Session) -> None:
        self.session = session
        self.transfers = TransferRepository(session)

    def create(
        self, request: TransferCreate, *, client_id: str, idempotency_key: str
    ) -> OperationResult:
        hashed_request = request_hash(request)

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
                )
            if not source.allow_negative and source.balance_minor < request.amount_minor:
                return self._reject(
                    request,
                    client_id,
                    idempotency_key,
                    "INSUFFICIENT_FUNDS",
                    Problem(
                        422, "Insufficient funds", "The source account has insufficient funds."
                    ),
                )

            transfer = Transfer(
                from_account_id=source.id,
                to_account_id=destination.id,
                amount_minor=request.amount_minor,
                currency=request.currency,
                status="COMPLETED",
            )
            self.session.add(transfer)
            self.session.flush()
            self.session.add_all(
                [
                    LedgerEntry(
                        transfer_id=transfer.id,
                        account_id=source.id,
                        amount_minor=-request.amount_minor,
                    ),
                    LedgerEntry(
                        transfer_id=transfer.id,
                        account_id=destination.id,
                        amount_minor=request.amount_minor,
                    ),
                ]
            )
            source.balance_minor -= request.amount_minor
            destination.balance_minor += request.amount_minor
            now = datetime.now(UTC)
            source.updated_at = now
            destination.updated_at = now
            # Same transaction as the money movement: the event exists if and only if
            # the transfer committed. The outbox relay publishes it to Kafka later.
            self.session.add(transfer_event(transfer, TRANSFER_COMPLETED))
            self.session.flush()

            body = TransferResponse.model_validate(transfer).model_dump(mode="json")
            self._complete_key(client_id, idempotency_key, 201, body)
            return OperationResult(201, body)

    def get(self, transfer_id: uuid.UUID) -> Transfer:
        transfer = self.transfers.get(transfer_id)
        if transfer is None:
            raise Problem(404, "Transfer not found", f"Transfer {transfer_id} does not exist.")
        return transfer

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
    ) -> OperationResult:
        """Record a business rejection as a REJECTED transfer with no ledger entries."""
        transfer = Transfer(
            from_account_id=request.from_account_id,
            to_account_id=request.to_account_id,
            amount_minor=request.amount_minor,
            currency=request.currency,
            status="REJECTED",
            failure_reason=reason,
        )
        self.session.add(transfer)
        self.session.flush()
        self.session.add(transfer_event(transfer, TRANSFER_REJECTED))
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
