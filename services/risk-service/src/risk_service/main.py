from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Depends, FastAPI, Request, Response, status
from pydantic import BaseModel

from risk_service.config import get_settings
from risk_service.errors import Problem, install_exception_handlers
from risk_service.features import TransferFeatures
from risk_service.model import Decision, RiskModel


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    # Load once at startup, not per request: unpickling takes far longer than a prediction.
    app.state.model = RiskModel.load(get_settings().model_path)
    yield
    app.state.model = None


app = FastAPI(title="Settle Risk Service", version="0.1.0", lifespan=lifespan)
install_exception_handlers(app)


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
    return ScoreResponse(score=risk, decision=model.decide(risk), model_version=model.model_version)


@app.get("/health/live")
def live() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/health/ready")
def ready(request: Request, response: Response) -> dict[str, str]:
    if getattr(request.app.state, "model", None) is None:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {"status": "unavailable"}
    return {"status": "ok"}
