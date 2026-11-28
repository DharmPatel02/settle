"""Prometheus metrics for payments-api and its worker processes.

HTTP rate, errors and duration per route come from prometheus-fastapi-instrumentator. These are
the business and dependency metrics on top of that.
"""

from collections.abc import Iterator

from prometheus_client import Counter, Gauge, Histogram
from prometheus_client.core import GaugeMetricFamily
from prometheus_client.registry import REGISTRY, Collector
from sqlalchemy import Engine
from sqlalchemy.pool import QueuePool

TRANSFERS = Counter(
    "settle_transfers_total",
    "New transfers by final status (idempotent replays are not counted).",
    ["status"],
)
TRANSFER_DURATION = Histogram(
    "settle_transfer_duration_seconds",
    "Time to create a transfer in the service layer, including the risk call.",
    buckets=(0.005, 0.01, 0.025, 0.05, 0.075, 0.1, 0.15, 0.25, 0.5, 1, 2.5, 5),
)
RISK_LATENCY = Histogram(
    "settle_risk_latency_seconds",
    "Time to get a risk decision, by whether the model or the fallback answered.",
    ["source"],
    buckets=(0.001, 0.0025, 0.005, 0.01, 0.025, 0.05, 0.1, 0.15, 0.25, 0.5),
)
RISK_FALLBACKS = Counter(
    "settle_risk_fallbacks_total", "Risk decisions made by the fallback policy.", ["reason"]
)
RISK_CIRCUIT_STATE = Gauge(
    "settle_risk_circuit_state", "Risk-service circuit breaker: 0 closed, 1 half-open, 2 open."
)
OUTBOX_LAG = Gauge(
    "settle_outbox_lag_seconds", "Age of the oldest unpublished outbox event; 0 when empty."
)
OUTBOX_PUBLISHED = Counter("settle_outbox_published_total", "Outbox events published to Kafka.")
RECONCILIATION_MISMATCHES = Gauge(
    "settle_reconciliation_mismatches",
    "Ledger invariant violations found by the last reconciliation run. Must always be 0.",
)
RECONCILIATION_LAST_SUCCESS = Gauge(
    "settle_reconciliation_last_success_timestamp_seconds",
    "Unix time of the last completed reconciliation run.",
)


class PoolCollector(Collector):
    """Reads the SQLAlchemy connection pool at scrape time."""

    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    def collect(self) -> Iterator[GaugeMetricFamily]:
        pool = self.engine.pool
        if not isinstance(pool, QueuePool):
            return
        yield GaugeMetricFamily("settle_db_pool_size", "Configured pool size.", pool.size())
        yield GaugeMetricFamily(
            "settle_db_pool_checked_out", "Connections in use by requests.", pool.checkedout()
        )
        yield GaugeMetricFamily(
            "settle_db_pool_overflow", "Connections opened beyond the pool size.", pool.overflow()
        )


_pool_collector: PoolCollector | None = None


def register_pool_metrics(engine: Engine) -> None:
    global _pool_collector
    if _pool_collector is not None:
        REGISTRY.unregister(_pool_collector)
    _pool_collector = PoolCollector(engine)
    REGISTRY.register(_pool_collector)
