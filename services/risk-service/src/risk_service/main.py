from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Depends, FastAPI, Request, Response, status
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from prometheus_client import Counter
from prometheus_fastapi_instrumentator import Instrumentator
from pydantic import BaseModel

from risk_service.config import get_settings
from risk_service.errors import Problem, install_exception_handlers
from risk_service.features import TransferFeatures
from risk_service.model import Decision, RiskModel
from risk_service.telemetry import configure_logging, configure_tracing

DECISIONS = Counter("settle_risk_decisions_total", "Risk decisions by outcome.", ["decision"])


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    configure_logging("risk-service")
    configure_tracing(
        "risk-service",
        enabled=settings.otel_enabled,
        otlp_endpoint=settings.otel_exporter_otlp_endpoint,
    )
    # Load once at startup, not per request: unpickling takes far longer than a prediction.
    app.state.model = RiskModel.load(settings.model_path)
    yield
    app.state.model = None


app = FastAPI(title="Settle Risk Service", version="0.1.0", lifespan=lifespan)
install_exception_handlers(app)
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


class ScoreResponse(BaseModel):
    score: float
    decision: Decision
    model_version: str


def get_model(request: Request) -> RiskModel:
    model = getattr(request.app.state, "model", None)
    if model is None:
        raise Problem(503, "Model not loaded", "The risk model is not available.")
    return model


@app.post("/v1/score", response_model=ScoreResponse)
def score(features: TransferFeatures, model: Annotated[RiskModel, Depends(get_model)]):
    # Plain `def`: prediction is CPU work, so FastAPI runs it in the thread pool.
    risk = model.score(features)
    decision = model.decide(risk)
    DECISIONS.labels(decision).inc()
    return ScoreResponse(score=risk, decision=decision, model_version=model.model_version)


@app.get("/health/live")
def live() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/health/ready")
def ready(request: Request, response: Response) -> dict[str, str]:
    if getattr(request.app.state, "model", None) is None:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {"status": "unavailable"}
    return {"status": "ok"}
