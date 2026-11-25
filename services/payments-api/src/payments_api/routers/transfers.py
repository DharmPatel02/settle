import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Header, status
from sqlalchemy.orm import Session

from payments_api.db import get_session
from payments_api.rate_limit import require_transfer_capacity
from payments_api.risk import RiskScorer, get_risk_scorer
from payments_api.routers.accounts import operation_response
from payments_api.schemas import TransferCreate, TransferResponse
from payments_api.security import ApiClientPrincipal, get_api_client
from payments_api.services import TransferService

router = APIRouter(prefix="/v1/transfers", tags=["transfers"])


@router.post(
    "",
    response_model=TransferResponse,
    status_code=status.HTTP_201_CREATED,
    responses={
        202: {"description": "Held for manual review (status PENDING_REVIEW)."},
        **{code: {} for code in (400, 401, 404, 409, 422, 429, 503)},
    },
)
def create_transfer(
    request: TransferCreate,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=200)],
    session: Annotated[Session, Depends(get_session)],
    client: Annotated[ApiClientPrincipal, Depends(require_transfer_capacity)],
    risk: Annotated[RiskScorer, Depends(get_risk_scorer)],
):
    result = TransferService(session, risk).create(
        request,
        client_id=str(client.id),
        idempotency_key=idempotency_key,
    )
    return operation_response(result)


@router.get("/{transfer_id}", response_model=TransferResponse)
def get_transfer(
    transfer_id: uuid.UUID,
    session: Annotated[Session, Depends(get_session)],
    _client: Annotated[ApiClientPrincipal, Depends(get_api_client)],
):
    return TransferService(session).get(transfer_id)
