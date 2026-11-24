import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Header, Query, status
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from payments_api.constants import EXTERNAL_FUNDING_ID
from payments_api.db import get_session
from payments_api.schemas import (
    AccountCreate,
    AccountResponse,
    DepositCreate,
    LedgerEntriesPage,
    TransferCreate,
    TransferResponse,
)
from payments_api.security import ApiClientPrincipal, get_api_client
from payments_api.services import AccountService, OperationResult, TransferService

router = APIRouter(prefix="/v1", tags=["accounts"])
IdempotencyHeader = Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=200)]


def operation_response(result: OperationResult) -> JSONResponse:
    media_type = "application/json" if result.status_code < 400 else "application/problem+json"
    return JSONResponse(status_code=result.status_code, content=result.body, media_type=media_type)


@router.post("/accounts", response_model=AccountResponse, status_code=status.HTTP_201_CREATED)
def create_account(
    request: AccountCreate,
    session: Annotated[Session, Depends(get_session)],
    _client: Annotated[ApiClientPrincipal, Depends(get_api_client)],
):
    return AccountService(session).create(request)


@router.get("/accounts/{account_id}", response_model=AccountResponse)
def get_account(
    account_id: uuid.UUID,
    session: Annotated[Session, Depends(get_session)],
    _client: Annotated[ApiClientPrincipal, Depends(get_api_client)],
):
    return AccountService(session).get(account_id)


@router.get("/accounts/{account_id}/entries", response_model=LedgerEntriesPage)
def list_entries(
    account_id: uuid.UUID,
    session: Annotated[Session, Depends(get_session)],
    _client: Annotated[ApiClientPrincipal, Depends(get_api_client)],
    cursor: Annotated[int | None, Query(gt=0)] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
):
    return AccountService(session).entries(account_id, cursor, limit)


@router.post(
    "/deposits",
    response_model=TransferResponse,
    status_code=status.HTTP_201_CREATED,
    responses={400: {}, 404: {}, 409: {}, 422: {}},
)
def create_deposit(
    request: DepositCreate,
    idempotency_key: IdempotencyHeader,
    session: Annotated[Session, Depends(get_session)],
    client: Annotated[ApiClientPrincipal, Depends(get_api_client)],
):
    transfer_request = TransferCreate(
        from_account_id=EXTERNAL_FUNDING_ID,
        to_account_id=request.account_id,
        amount_minor=request.amount_minor,
        currency=request.currency,
    )
    result = TransferService(session).create(
        transfer_request,
        client_id=str(client.id),
        idempotency_key=idempotency_key,
    )
    return operation_response(result)
