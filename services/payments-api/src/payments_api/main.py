from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Depends, FastAPI, Response, status
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from prometheus_fastapi_instrumentator import Instrumentator
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from payments_api.config import get_settings
from payments_api.db import get_session
from payments_api.errors import install_exception_handlers
from payments_api.routers.accounts import router as accounts_router
from payments_api.routers.admin import router as admin_router
from payments_api.routers.transfers import router as transfers_router
from payments_api.telemetry import configure_logging, configure_tracing


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    configure_logging("payments-api")
    configure_tracing(
        "payments-api",
        enabled=settings.otel_enabled,
        otlp_endpoint=settings.otel_exporter_otlp_endpoint,
    )
    yield


app = FastAPI(title="Settle Payments API", version="0.1.0", lifespan=lifespan)
install_exception_handlers(app)
app.include_router(accounts_router)
app.include_router(transfers_router)
app.include_router(admin_router)
# GET /metrics: request rate, errors and duration per route, plus the settle_* metrics.
Instrumentator(
    excluded_handlers=["/metrics", "/health/.*"],
    should_instrument_requests_inprogress=True,
    inprogress_labels=True,
).instrument(
    app,
    # Millisecond-scale buckets; the library default (0.1, 0.5, 1 s) cannot show a p95 of 20 ms.
    latency_lowr_buckets=(0.005, 0.01, 0.025, 0.05, 0.075, 0.1, 0.15, 0.25, 0.5, 1, 2.5, 5),
).expose(app, include_in_schema=False)
FastAPIInstrumentor.instrument_app(app, excluded_urls="/metrics,/health/live,/health/ready")


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
