import hashlib
import uuid
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends
from fastapi.security import APIKeyHeader
from sqlalchemy import select

from payments_api.db import get_sessionmaker
from payments_api.errors import Problem
from payments_api.models import ApiClient

api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


@dataclass(frozen=True, slots=True)
class ApiClientPrincipal:
    id: uuid.UUID
    name: str
    is_admin: bool


def hash_api_key(api_key: str) -> str:
    return hashlib.sha256(api_key.encode()).hexdigest()


def get_api_client(
    api_key: Annotated[str | None, Depends(api_key_header)],
) -> ApiClientPrincipal:
    if api_key is None:
        raise Problem(401, "Authentication required", "Provide an X-API-Key header.")

    with get_sessionmaker()() as session:
        client = session.scalar(
            select(ApiClient).where(ApiClient.key_hash == hash_api_key(api_key))
        )
    if client is None:
        raise Problem(401, "Invalid API key", "The supplied API key is not valid.")
    return ApiClientPrincipal(id=client.id, name=client.name, is_admin=client.is_admin)


def require_admin_client(
    client: Annotated[ApiClientPrincipal, Depends(get_api_client)],
) -> ApiClientPrincipal:
    if not client.is_admin:
        raise Problem(403, "Admin access required", "This operation requires an admin API key.")
    return client
