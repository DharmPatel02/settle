from typing import Annotated

from fastapi import Depends, FastAPI, Response, status
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from payments_api.db import get_session
from payments_api.errors import install_exception_handlers
from payments_api.routers.accounts import router as accounts_router
from payments_api.routers.admin import router as admin_router
from payments_api.routers.transfers import router as transfers_router

app = FastAPI(title="Settle Payments API", version="0.1.0")
install_exception_handlers(app)
app.include_router(accounts_router)
app.include_router(transfers_router)
app.include_router(admin_router)


@app.get("/health/live")
def live() -> dict[str, str]:
    # Deliberately checks nothing external: a failing liveness probe makes
    # Kubernetes restart the process, which would not fix a database outage.
    return {"status": "ok"}


@app.get("/health/ready")
def ready(response: Response, session: Annotated[Session, Depends(get_session)]) -> dict[str, str]:
    try:
        session.execute(text("SELECT 1"))
    except SQLAlchemyError:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {"status": "unavailable"}
    return {"status": "ok"}
