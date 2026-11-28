# Settle runbook

One section per alert in `infra/prometheus/alerts.yml`. Each says what the alert means, how to
confirm it, and how to fix it. Local URLs: Grafana `localhost:3000` (dashboards "Settle /
Services (RED)" and "Settle / Payments flow"), Prometheus `localhost:9090/alerts`, Jaeger
`localhost:16686`. Logs are JSON with a `trace_id` field; paste it into Jaeger to see the whole
request.

Severity `page` means act now; `ticket` means fix during working hours.

## HighErrorRate

**Means:** more than 2% of a service's requests returned 5xx for 5 minutes. 4xx responses
(validation, insufficient funds, declines) do not count; they are correct answers.

**Check:**
1. RED dashboard, pick the `job` from the alert: which route's error share rose, and when.
2. Search the service's logs for `"level": "error"` around that time and open a `trace_id` in
   Jaeger. The red span shows which dependency failed.
3. `GET /health/ready` on the service: 503 means it cannot reach Postgres.
4. payments-api returns 503 on transfers when Redis is down (the rate limiter fails closed, ADR
   0006). Check `docker compose -f infra/docker-compose.yml ps redis`.

**Fix:** restore the failing dependency (Postgres, Redis). If a deploy caused it, roll back.
Transfers are idempotent, so clients can safely retry once the service is healthy.

## TransferLatencyHigh

**Means:** p95 of `POST /v1/transfers` stayed above 250 ms for 5 minutes.

**Check:**
1. Payments flow dashboard, "Risk decision latency p95": a slow risk-service adds up to 150 ms
   per transfer before the timeout.
2. RED dashboard, "DB connection pool": if checked-out connections sit at pool size plus
   overflow, requests are queueing for a connection.
3. "Requests in flight" rising while throughput is flat means a queue is forming.
4. Open a slow trace in Jaeger (search `payments-api`, `POST /v1/transfers`, min duration
   250ms) and look for the longest child span: risk call, a lock wait on `SELECT ... FOR UPDATE`,
   or a slow `INSERT`.
5. Many transfers from one account serialise on its row lock; that is expected (see the
   hot-account results in `docs/performance.md`).

**Fix:** depends on the long span. Risk-service slow: scale it or lower the timeout so the
fallback kicks in sooner. Pool exhausted: raise `DB_POOL_SIZE` / `DB_MAX_OVERFLOW` within
Postgres's `max_connections`, or add API workers. Lock contention on one account: nothing is
broken; the lock is what prevents double spending.

## OutboxLagHigh

**Means:** the oldest unpublished outbox event is more than 30 seconds old. Transfers are safe
(the event is committed in Postgres), but webhooks are delayed.

**Check:**
1. Is the relay running? `up{job="outbox-relay"}` in Prometheus, or the TargetDown alert.
2. Relay logs: repeated `Outbox publish failed; retrying` means Kafka is unreachable.
3. `docker compose -f infra/docker-compose.yml ps kafka`.
4. Payments flow dashboard: "published per second" near zero while transfers continue.

**Fix:** start the relay (`uv run python -m payments_api.outbox_relay`) or restore Kafka. The
relay catches up by itself; several relays can run at once (`SKIP LOCKED`) to drain a backlog
faster. Consumers deduplicate, so republished events are harmless.

## DeadLetterQueueNotEmpty

**Means:** at least one webhook delivery failed 8 times and is now `DEAD`. The merchant did not
get that event.

**Check:**
1. `GET localhost:8001/admin/dlq` (header `X-Admin-Token`) lists dead deliveries with
   `last_error`: `HTTP 500`, `HTTP 401` (signature rejected), or a connection error.
2. Group by URL: one merchant failing points at the merchant; all failing points at us.
3. For `HTTP 401`, check that the subscription's secret matches what the merchant verifies with.

**Fix:** once the merchant is healthy, replay each delivery with
`POST /admin/dlq/{id}/replay`. Replays are safe: merchants must dedupe by `event_id`.

## ReconciliationMismatch

**Means:** the ledger invariant is broken. Either all entries do not sum to zero, or an account's
cached balance differs from the sum of its entries. Money was created, lost, or mis-cached. This
should never happen.

**Check:**
1. Reconciliation logs show the ledger sum and the number of mismatched accounts.
2. Find the accounts:
   ```sql
   SELECT a.id, a.balance_minor, COALESCE(SUM(e.amount_minor), 0) AS ledger
   FROM accounts a LEFT JOIN ledger_entries e ON e.account_id = a.id
   GROUP BY a.id HAVING a.balance_minor <> COALESCE(SUM(e.amount_minor), 0);
   ```
3. Look at those accounts' recent transfers and any manual database changes or new deploys.

**Fix:** stop the cause first (roll back the deploy, stop manual edits). Ledger entries are the
source of truth and are never edited; a wrong cached balance is corrected to the ledger sum. A
non-zero ledger sum needs a compensating, reviewed entry, never a delete. Write an incident
report.

## ReconciliationStale

**Means:** no reconciliation has completed for 5 minutes, so a broken ledger would go unnoticed.

**Check:** is `python -m payments_api.reconciliation` running? Its logs show
`Reconciliation run failed` with the database error if Postgres is unreachable.

**Fix:** restart it, or restore Postgres. The first successful run clears the alert.

## RiskCircuitOpen

**Means:** payments-api's circuit breaker for risk-service is open: 5 calls in a row failed or
timed out. Transfers still work: up to `RISK_FALLBACK_APPROVE_MAX_MINOR` ($100 by default) is
approved, larger amounts wait in the review queue (ADR 0010).

**Check:**
1. `up{job="risk-service"}` and `GET localhost:8003/health/ready`. 503 means the model did not
   load (for example a scikit-learn version mismatch; see its logs).
2. Payments flow dashboard, "Risk fallbacks": the `reason` label shows `timeout`, `error`, or
   `circuit_open`.
3. Watch the review queue grow: `GET /admin/transfers/review` with an admin API key.

**Fix:** restore risk-service. The breaker tries one call after 30 seconds and closes on
success. It only tries when a transfer arrives, so with no traffic the alert stays on until the
next transfer. Afterwards, work through the review queue.

## TargetDown

**Means:** Prometheus could not scrape a process for 2 minutes. Either the process is down, or
its metrics endpoint is unreachable, which also hides its other alerts.

**Check:** the `job` label names the process. Ports: payments-api 8000, webhook-service 8001,
mock-merchant 8002, risk-service 8003, outbox-relay 9101, reconciliation 9102,
webhook-consumer 9103, webhook-delivery-worker 9104.

**Fix:** restart the process. Each one is safe to restart at any time: the API is stateless, the
relay and consumer redeliver at least once and deduplicate, and the delivery worker's leases
expire so another pass retries in-flight deliveries.
