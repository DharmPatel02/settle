"""Reconciliation: checks the ledger invariant against the live database.

Run as its own process: `python -m payments_api.reconciliation`. Every interval it checks that
all ledger entries sum to zero and that every account's cached balance equals the sum of its
entries, and exports the number of violations as `settle_reconciliation_mismatches`. The value
must always be 0; anything else means money was created, lost, or mis-cached.
"""

import logging
import signal
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

from prometheus_client import start_http_server
from sqlalchemy import text
from sqlalchemy.orm import Session

from payments_api.config import get_settings
from payments_api.db import get_sessionmaker
from payments_api.metrics import RECONCILIATION_LAST_SUCCESS, RECONCILIATION_MISMATCHES
from payments_api.telemetry import configure_logging

logger = logging.getLogger(__name__)

LEDGER_SUM = text("SELECT COALESCE(SUM(amount_minor), 0) FROM ledger_entries")
BALANCE_MISMATCHES = text(
    """
    SELECT COUNT(*) FROM accounts a
    LEFT JOIN (
        SELECT account_id, SUM(amount_minor) AS total FROM ledger_entries GROUP BY account_id
    ) e ON e.account_id = a.id
    WHERE a.balance_minor <> COALESCE(e.total, 0)
    """
)


@dataclass(frozen=True, slots=True)
class ReconciliationResult:
    ledger_sum: int
    balance_mismatches: int

    @property
    def mismatches(self) -> int:
        return self.balance_mismatches + (1 if self.ledger_sum != 0 else 0)


def reconcile(session: Session) -> ReconciliationResult:
    # Both queries must see the same snapshot. Under READ COMMITTED a transfer committing
    # between them would show up in one query but not the other: a false alarm.
    with session.begin():
        session.connection(execution_options={"isolation_level": "REPEATABLE READ"})
        ledger_sum = int(session.scalar(LEDGER_SUM) or 0)
        mismatches = int(session.scalar(BALANCE_MISMATCHES) or 0)
    return ReconciliationResult(ledger_sum, mismatches)


def run_once(session_factory: Callable[[], Session]) -> ReconciliationResult:
    with session_factory() as session:
        result = reconcile(session)
    RECONCILIATION_MISMATCHES.set(result.mismatches)
    RECONCILIATION_LAST_SUCCESS.set(time.time())
    if result.mismatches:
        logger.error(
            "Ledger invariant violated: ledger sum %d, %d accounts mismatched",
            result.ledger_sum,
            result.balance_mismatches,
        )
    else:
        logger.info("Reconciliation passed")
    return result


def main() -> None:
    configure_logging("reconciliation")
    settings = get_settings()
    start_http_server(settings.reconciliation_metrics_port)
    stop = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    session_factory = get_sessionmaker()
    while not stop.is_set():
        try:
            run_once(session_factory)
        except Exception:
            logger.exception("Reconciliation run failed")
        stop.wait(settings.reconciliation_interval_seconds)


if __name__ == "__main__":
    main()
