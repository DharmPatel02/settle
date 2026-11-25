"""Risk checks for transfers: a call to risk-service guarded by a timeout and a circuit breaker.

The call happens before the transfer's locking transaction opens, so a slow risk-service can
never hold account locks. When risk-service is slow, failing, or the breaker is open, the
fallback policy decides instead (ADR 0010).
"""

import logging
from dataclasses import asdict, dataclass
from enum import StrEnum
from functools import lru_cache
from typing import Protocol

import httpx
import pybreaker
from pydantic import BaseModel

from payments_api.config import get_settings

logger = logging.getLogger(__name__)

FALLBACK_MODEL_VERSION = "fallback"


class RiskDecision(StrEnum):
    APPROVE = "APPROVE"
    REVIEW = "REVIEW"
    DECLINE = "DECLINE"


@dataclass(frozen=True, slots=True)
class RiskFeatures:
    amount_minor: int
    hour_of_day: int
    account_age_days: float
    transfers_last_hour: int
    avg_amount_30d_minor: int
    is_new_recipient: bool


@dataclass(frozen=True, slots=True)
class RiskAssessment:
    decision: RiskDecision
    score: float | None
    model_version: str
    fallback_reason: str | None = None


class RiskScorer(Protocol):
    def assess(self, features: RiskFeatures) -> RiskAssessment: ...


class _ScoreResponse(BaseModel):
    score: float
    decision: RiskDecision
    model_version: str


@dataclass(frozen=True, slots=True)
class FallbackPolicy:
    """Used when the model cannot answer: small amounts go through, larger ones wait for a person.

    It never declines: without a score there is no evidence against the customer.
    """

    approve_up_to_minor: int

    def decide(self, features: RiskFeatures, reason: str) -> RiskAssessment:
        decision = (
            RiskDecision.APPROVE
            if features.amount_minor <= self.approve_up_to_minor
            else RiskDecision.REVIEW
        )
        return RiskAssessment(decision, None, FALLBACK_MODEL_VERSION, reason)


class RiskClient:
    def __init__(
        self, http: httpx.Client, breaker: pybreaker.CircuitBreaker, fallback: FallbackPolicy
    ) -> None:
        self.http = http
        self.breaker = breaker
        self.fallback = fallback

    def assess(self, features: RiskFeatures) -> RiskAssessment:
        try:
            # Every failure inside `call` counts towards opening the breaker. Once open, calls
            # fail immediately for `reset_timeout` seconds instead of waiting for a timeout.
            return self.breaker.call(self._score, features)
        except pybreaker.CircuitBreakerError:
            reason = "circuit_open"
        except httpx.TimeoutException:
            reason = "timeout"
        except httpx.HTTPError, ValueError:
            reason = "error"
        logger.warning("Risk service unavailable (%s); using fallback policy", reason)
        return self.fallback.decide(features, reason)

    def _score(self, features: RiskFeatures) -> RiskAssessment:
        response = self.http.post("/v1/score", json=asdict(features))
        response.raise_for_status()
        body = _ScoreResponse.model_validate(response.json())
        return RiskAssessment(body.decision, body.score, body.model_version)


@lru_cache
def get_risk_scorer() -> RiskScorer:
    # One client per process, so every request shares the same breaker state and connections.
    settings = get_settings()
    return RiskClient(
        httpx.Client(
            base_url=str(settings.risk_service_url),
            timeout=settings.risk_timeout_seconds,
        ),
        pybreaker.CircuitBreaker(
            fail_max=settings.risk_breaker_fail_max,
            reset_timeout=settings.risk_breaker_reset_seconds,
            name="risk-service",
        ),
        FallbackPolicy(approve_up_to_minor=settings.risk_fallback_approve_max_minor),
    )
