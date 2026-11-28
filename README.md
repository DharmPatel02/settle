# Settle

[![CI](https://github.com/DharmPatel02/settle/actions/workflows/ci.yml/badge.svg)](https://github.com/DharmPatel02/settle/actions/workflows/ci.yml)

Settle is a payments and wallet API built with FastAPI and PostgreSQL. It uses an
append-only, double-entry ledger and idempotent transfer requests to keep money movement safe.
Each transfer is scored by a fraud-risk model before money moves, with a timeout, circuit breaker
and fallback policy around the call. Transfer events go through a transactional outbox to Kafka,
and a webhook service delivers them to merchants as signed webhooks with retries and a
dead-letter queue. Every service exports Prometheus metrics, and one OpenTelemetry trace
follows a transfer from the API request to the merchant's webhook.

| Service | What it does |
|---|---|
| `services/payments-api` | Accounts, transfers, ledger, idempotency, rate limiting, outbox relay |
| `services/webhook-service` | Kafka consumer, signed webhook delivery, retries, DLQ admin API |
| `services/mock-merchant` | Fake merchant that verifies signatures and fails on purpose |
| `services/risk-service` | Fraud scoring model (scikit-learn) served by FastAPI; see its [model card](services/risk-service/MODEL_CARD.md) |

## Run locally

```bash
docker compose -f infra/docker-compose.yml up -d postgres redis kafka
cd services/payments-api
cp .env.example .env
uv sync
uv run alembic upgrade head
uv run python -m payments_api.create_api_client --name local
uv run uvicorn payments_api.main:app --reload
```

Copy the API key printed by the client command and use Swagger's **Authorize** button to set
`X-API-Key`. Open `http://127.0.0.1:8000/docs` for the Swagger UI. Run checks from any service
directory:

```bash
uv run ruff check .
uv run ruff format --check .
uv run pytest --cov=payments_api   # or webhook_service, mock_merchant, risk_service
uv run alembic check                # payments-api and webhook-service
```

## Run the webhook pipeline

Each command runs in its own terminal, from the service directory, after `cp .env.example .env`
and `uv sync`. The `settle_webhooks` database is created when the Postgres volume is first made;
on an existing volume, run
`docker compose -f infra/docker-compose.yml exec postgres createdb -U settle settle_webhooks`.

```bash
# services/payments-api
uv run python -m payments_api.outbox_relay
uv run python -m payments_api.reconciliation   # re-checks the ledger invariant every minute

# services/webhook-service
uv run alembic upgrade head
uv run uvicorn webhook_service.main:app --port 8001
uv run python -m webhook_service.consumer
uv run python -m webhook_service.delivery_worker

# services/mock-merchant (fails 30% of requests by default)
uv run uvicorn mock_merchant.main:app --port 8002
```

Subscribe an account to the mock merchant, using the secret the merchant verifies with:

```bash
curl -X POST localhost:8001/admin/subscriptions \
  -H "X-Admin-Token: local-admin-token-change-me" -H "Content-Type: application/json" \
  -d '{"account_id": "<account id>", "url": "http://localhost:8002/webhooks",
       "secret": "mock-merchant-secret-change-me"}'
```

Then make a transfer from that account. `GET localhost:8001/admin/deliveries` (same admin
header) shows each delivery's attempts. To see the dead-letter queue, set the merchant to always fail with
`curl -X PUT localhost:8002/config/failure-rate -H "Content-Type: application/json" -d '{"failure_rate": 1}'`,
make another transfer, and watch it reach `GET /admin/dlq` after 8 attempts. Replay it with
`POST /admin/dlq/{id}/replay`.

## Run the risk service

payments-api calls the risk service at `RISK_SERVICE_URL` (default `http://localhost:8003`).
Without it, transfers still work through the fallback policy: up to $100 is approved and larger
amounts wait in the review queue.

```bash
# services/risk-service
uv run uvicorn risk_service.main:app --port 8003
uv run python -m risk_service.train            # optional: retrain the committed model
uv run python scripts/measure_latency.py       # p50/p95/p99 against the running service
```

Held transfers are listed at `GET /admin/transfers/review` and approved or declined with
`POST /admin/transfers/{id}/approve` or `/decline`. These need an admin API key:
`uv run python -m payments_api.create_api_client --name admin --admin`.

## Observability

```bash
docker compose -f infra/docker-compose.yml up -d --build prometheus grafana jaeger
```

| Tool | URL | What is there |
|---|---|---|
| Grafana | http://localhost:3000 | "Settle / Services (RED)" and "Settle / Payments flow" dashboards |
| Prometheus | http://localhost:9090/alerts | Alert rules; each has a section in [docs/runbook.md](docs/runbook.md) |
| Jaeger | http://localhost:16686 | Traces; set `OTEL_ENABLED=true` in each service's `.env` |

Every FastAPI service serves `GET /metrics`. The worker processes serve metrics on their own
ports: outbox relay 9101, reconciliation 9102, webhook consumer 9103, delivery worker 9104.
Logs are JSON lines with a `trace_id`. Prometheus and Grafana configs are baked into their
images, so rebuild them (`--build`) after editing files in `infra/`.

## Load tests

The scenarios are in [`loadtest/`](loadtest/): smoke, steady, spike, and hot-account contention.
Run payments-api with a high rate limit for the load-test key
(`TRANSFER_RATE_CAPACITY=1000000 TRANSFER_RATE_REFILL_PER_SECOND=1000000`), then:

```bash
cd loadtest
uv run --project ../services/payments-api python seed.py --api-key <key> --accounts 10000
k6 run -e API_KEY=<key> -e SCENARIO=steady -e RATE=100 -e LABEL=mine transfers.js
```

Raw k6 summaries are saved in `docs/perf-results/`; results and the bottleneck fix are written
up in [docs/performance.md](docs/performance.md).
