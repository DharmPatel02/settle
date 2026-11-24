# Settle Payments Design

## Problem

Settle lets a client create wallets, deposit funds, transfer funds, and inspect an account's
append-only ledger. A request retry must never move money twice, and concurrent transfers must
never spend the same balance twice.

## Goals and non-goals

The API provides accounts, deposits, transfers, ledger history, API-key authentication, per-client
transfer rate limiting, PostgreSQL persistence, and RFC 7807 errors. Fraud scoring, events,
webhooks, and multi-region operation are outside the current boundary.

## API sketch

- All `/v1` routes require `X-API-Key`; health endpoints remain public.
- `POST /v1/accounts`; `GET /v1/accounts/{id}`
- `GET /v1/accounts/{id}/entries?cursor=&limit=`
- `POST /v1/deposits` with `Idempotency-Key`
- `POST /v1/transfers` with `Idempotency-Key`; `GET /v1/transfers/{id}`

## Data model

`accounts` caches the current balance. `transfers` records the business operation.
`ledger_entries` records one debit and one credit per completed transfer. `idempotency_keys`
binds an API client and key to a request hash and stored response. `api_clients` stores only a
SHA-256 key hash. The external funding account is allowed to go negative so deposits remain
balanced ledger movements.

## Failure scenarios

- A duplicate request returns the stored response; a changed body with the same key is rejected.
- Account rows are locked in a stable order to prevent double-spending and avoid deadlocks.
- Any unexpected database failure rolls back the key, transfer, entries, and balance updates.
- Database constraints reject zero entries, non-positive transfers, and invalid account balances.
- Redis atomically enforces a token bucket per API client; transfers fail closed if it is down.

## Open questions

Key rotation and revocation workflows are not defined yet. Additional currencies need one external
funding account per currency. Retention and archival rules remain to be decided.
