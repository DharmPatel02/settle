# Settle

[![Payments API CI](https://github.com/DharmPatel02/settle/actions/workflows/payments-api.yml/badge.svg)](https://github.com/DharmPatel02/settle/actions/workflows/payments-api.yml)

Settle is a payments and wallet API built with FastAPI and PostgreSQL. It uses an
append-only, double-entry ledger and idempotent transfer requests to keep money movement safe.

## Run locally

```bash
docker compose -f infra/docker-compose.yml up -d postgres redis
cd services/payments-api
cp .env.example .env
uv sync
uv run alembic upgrade head
uv run python -m payments_api.create_api_client --name local
uv run uvicorn payments_api.main:app --reload
```

Copy the API key printed by the client command and use Swagger's **Authorize** button to set
`X-API-Key`. Open `http://127.0.0.1:8000/docs` for the Swagger UI. Run checks from the service
directory:

```bash
uv run ruff check .
uv run ruff format --check .
uv run pytest --cov=payments_api
uv run alembic check
```
