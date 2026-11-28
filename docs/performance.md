# Performance

Every number here comes from a k6 summary saved in [`perf-results/`](perf-results/). The
file name says the scenario, configuration, rate and repetition (`steady-before-100-r2.json`).

## Setup

| | |
|---|---|
| Machine | Apple M4 (4 performance + 6 efficiency cores), 16 GB, macOS 26.6.2 |
| Load generator | k6 v2.4.0, on the same laptop |
| Services | One process each, run with `uv run` on the host, Python 3.14.7; payments-api is one uvicorn worker with SQLAlchemy's default pool (5 + 10 overflow) |
| Infrastructure | Docker 29.8.2 (10 CPUs, 8 GB): PostgreSQL 16.15, Redis 7, Kafka 3.9.1 |
| Also running | Outbox relay, webhook consumer and delivery worker, reconciliation, Prometheus, Grafana, Jaeger |
| Tracing | Off (`OTEL_ENABLED=false`) |
| Rate limit | Raised for the load-test API key so it does not shape the results |
| Data | 10,000 accounts with $10,000 each, created through the API by `loadtest/seed.py`. Fresh accounts before every run, so the risk model sees the same history each time |

Each request is `POST /v1/transfers` between two seeded accounts for $1–$49. A 201 (completed),
202 (held for review) or 422 (declined) is a correct answer; anything else counts as an error.
Thresholds: p95 below 250 ms and errors below 1%. Everything shares one laptop, so these
numbers compare configurations with each other; they are not production capacity.

## Bottleneck: OpenMP oversubscription in risk-service

**Symptom.** Throughput stopped at about 110 transfers/s whatever the offered load, and latency
then grew to seconds as requests queued. With a 200/s run going:

- The database connection pool was not the limit: `settle_db_pool_checked_out` read 0 at every
  scrape and the pool never grew past its first connection. (In a later 100/s run, Postgres
  used 14–32% CPU.)
- payments-api measured its risk calls at a p95 of 490 ms.
- `ps` showed risk-service at **445% CPU**, payments-api at 86%.

**Cause.** scikit-learn's `HistGradientBoostingClassifier` predicts with OpenMP, which by
default starts one thread per core (10 here) for every call. Scoring one transfer gains nothing
from 10 threads, but paying to start and synchronise them on every call is expensive. With up to
40 requests predicting at once in risk-service's thread pool, hundreds of OpenMP threads fought
over 10 cores. That starved risk-service itself and the payments-api process next to it.

**Change (the only one).** `OMP_NUM_THREADS=1` for risk-service, set in
`risk_service/__init__.py` before scikit-learn loads OpenMP; a test checks it. "Before" runs
start risk-service with `OMP_NUM_THREADS=10`, which is what it used before the change.

**Method.** Three runs per configuration, alternating before and after, each on freshly seeded
accounts. The tables show the median of the three; every run is listed below.

### Results

Capacity: 200 transfers/s offered for 1 minute, more than either configuration can handle.

| | Achieved throughput | Median latency | p95 |
|---|---|---|---|
| Before | 107.6/s | 4.28 s | 5.95 s |
| After | **143.1/s (+33%)** | 3.09 s | 4.09 s |

Latency at 100 transfers/s for 2 minutes, which both configurations can sustain:

| | Median | p95 | p99 |
|---|---|---|---|
| Before | 22.9 ms | 300.6 ms | 485.7 ms |
| After | **11.4 ms** | 339.8 ms | 779.0 ms |

One client sending transfers back to back (smoke, 30 s, one run each): 51.2/s and median
16.7 ms before; 82.8/s and median 10.7 ms after.

**Reading.** The change raised capacity by a third and halved the typical request. It did not
improve the slowest 5% at 100/s, which still fails the 250 ms target: 100/s is close to what a
single payments-api process can do, and its tail comes from queueing in that process (see
"Next bottleneck").

### Every run

| Run | Throughput | Median | p95 | p99 |
|---|---|---|---|---|
| steady-before-100-r1 | 99.9/s | 22.9 ms | 300.6 ms | 432.6 ms |
| steady-before-100-r2 | 99.9/s | 29.7 ms | 282.8 ms | 485.7 ms |
| steady-before-100-r3 | 98.7/s | 21.1 ms | 425.8 ms | 1.18 s |
| steady-after-100-r1 | 73.6/s | 3.55 s | 13.69 s | 16.66 s |
| steady-after-100-r2 | 99.7/s | 11.1 ms | 339.8 ms | 643.3 ms |
| steady-after-100-r3 | 99.5/s | 11.4 ms | 310.1 ms | 779.0 ms |
| steady-before-200-r1 | 102.2/s | 4.57 s | 5.95 s | 6.14 s |
| steady-before-200-r2 | 107.6/s | 4.28 s | 6.22 s | 6.42 s |
| steady-before-200-r3 | 112.1/s | 4.20 s | 5.38 s | 5.56 s |
| steady-after-200-r1 | 140.3/s | 3.44 s | 4.09 s | 4.20 s |
| steady-after-200-r2 | 149.1/s | 3.06 s | 3.81 s | 4.00 s |
| steady-after-200-r3 | 143.1/s | 3.09 s | 4.81 s | 5.91 s |

`steady-after-100-r1` stalled for about 30 seconds mid-run: throughput in Prometheus fell to
around 30/s and k6 dropped 3,172 iterations. The two repeats did not stall, and no cause was
found. It is kept here rather than discarded; the median is unaffected by it.

## Other scenarios (after the change)

| Scenario | Load | Throughput | Median | p95 | Notes |
|---|---|---|---|---|---|
| steady-after-50 | 50/s, 1 min | 50.0/s | 13.0 ms | 18.9 ms | Passes both thresholds |
| spike-after | 25/s → 200/s for 40 s → 25/s | 80.0/s | 2.11 s | 5.56 s | Above ~143/s requests queue; 943 dropped; no errors; recovers when the spike ends |
| hot-after-100 | 100/s from 5 accounts | 100.0/s | 10.5 ms | 66.5 ms | Risk model held 3,486 and declined 2,500 of 6,001 |

No run returned an error (`http_req_failed` 0% everywhere). Reconciliation reported 0
mismatches after all runs.

## Hot accounts vs random accounts

In `hot-after-100` the risk model, not the database, decided the outcome: five accounts each
sending 20 transfers a second looks exactly like the velocity fraud the model was trained on, so
it held or declined almost everything. Held and declined transfers still lock both accounts
but write no ledger entries, so the locks are released quickly.

To measure lock contention on its own, the next runs stopped risk-service. The breaker opens,
the fallback approves every transfer (all are under $100), and each one locks the hot account,
writes two ledger entries and updates both balances.

| Run | Load | Throughput | Median | p95 |
|---|---|---|---|---|
| steady-no-risk-100 | 100/s, random | 100.0/s | 8.8 ms | 18.4 ms |
| hot-no-risk-100 | 100/s, 5 hot accounts | 100.0/s | 9.0 ms | 20.9 ms |
| steady-no-risk-250 | 250/s, random | 185.5/s | 2.53 s | 3.14 s |
| hot-no-risk-250 | 250/s, 5 hot accounts | 184.7/s | 2.58 s | 2.98 s |

Hot and random behave the same here. Each transfer holds its account locks only for the few
statements of one transaction, so 20 to 50 transfers a second per account barely queue. Both
saturate at about 185/s because the single payments-api process runs out of CPU first. Lock
contention would show up with fewer hot accounts or more API processes: one account can never
go faster than one transfer per lock-hold time, however many workers there are, because the
row lock serialises them. That serialisation is what prevents double spending.

## Next bottleneck

With risk-service stopped, p95 at 100/s is 18–21 ms; with it running, 310–340 ms. The risk
model itself answers in 5–25 ms (p95 measured by risk-service), so most of the difference is
time inside the single payments-api process around the call, not prediction time. The next
experiments, one at a time, would be more uvicorn workers for payments-api (Prometheus then
needs multiprocess mode) and a second risk-service worker.
