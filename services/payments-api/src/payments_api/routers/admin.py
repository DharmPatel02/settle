import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from payments_api.db import get_session
from payments_api.routers.accounts import operation_response
from payments_api.schemas import TransferResponse
from payments_api.security import ApiClientPrincipal, require_admin_client
from payments_api.services import TransferService

router = APIRouter(prefix="/admin/transfers", tags=["admin"])
AdminClient = Annotated[ApiClientPrincipal, Depends(require_admin_client)]


@router.get("/review", response_model=list[TransferResponse])
def review_queue(
    session: Annotated[Session, Depends(get_session)],
    _admin: AdminClient,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
):
    """Transfers held by risk checks, oldest first."""
    return TransferService(session).pending_review(limit)


@router.post(
    "/{transfer_id}/approve",
    response_model=TransferResponse,
    responses={401: {}, 403: {}, 404: {}, 409: {}, 422: {}},
)
def approve_transfer(
    transfer_id: uuid.UUID,
    session: Annotated[Session, Depends(get_session)],
    _admin: AdminClient,
):
    return operation_response(TransferService(session).approve(transfer_id))


@router.post(
    "/{transfer_id}/decline",
    response_model=TransferResponse,
    responses={401: {}, 403: {}, 404: {}, 409: {}},
)
def decline_transfer(
    transfer_id: uuid.UUID,
    session: Annotated[Session, Depends(get_session)],
    _admin: AdminClient,
):
    return operation_response(TransferService(session).decline(transfer_id))
