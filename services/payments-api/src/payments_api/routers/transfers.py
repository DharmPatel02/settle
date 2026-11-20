import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Header, status
from sqlalchemy.orm import Session

from payments_api.constants import DEFAULT_CLIENT_ID
from payments_api.db import get_session
from payments_api.routers.accounts import operation_response
from payments_api.schemas import TransferCreate, TransferResponse
from payments_api.services import TransferService

router = APIRouter(prefix="/v1/transfers", tags=["transfers"])


@router.post(
    "",
    response_model=TransferResponse,
    status_code=status.HTTP_201_CREATED,
    responses={400: {}, 404: {}, 409: {}, 422: {}},
)
def create_transfer(
    request: TransferCreate,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=200)],
    session: Annotated[Session, Depends(get_session)],
):
    result = TransferService(session).create(
        request,
        client_id=DEFAULT_CLIENT_ID,
        idempotency_key=idempotency_key,
    )
    return operation_response(result)


@router.get("/{transfer_id}", response_model=TransferResponse)
def get_transfer(transfer_id: uuid.UUID, session: Annotated[Session, Depends(get_session)]):
    return TransferService(session).get(transfer_id)
