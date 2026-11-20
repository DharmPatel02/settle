# Settle Payments Design

## Problem

Settle lets a client create wallets, deposit funds, transfer funds, and inspect an account's
append-only ledger. A request retry must never move money twice, and concurrent transfers must
never spend the same balance twice.

## Goals and non-goals

The initial API provides accounts, deposits, transfers, ledger history, PostgreSQL persistence,
and RFC 7807 errors. Authentication, fraud scoring, events, webhooks, and multi-region operation
are intentionally outside this first boundary.

## API sketch

- `POST /v1/accounts`; `GET /v1/accounts/{id}`
- `GET /v1/accounts/{id}/entries?cursor=&limit=`
- `POST /v1/deposits` with `Idempotency-Key`
- `POST /v1/transfers` with `Idempotency-Key`; `GET /v1/transfers/{id}`

## Data model

`accounts` caches the current balance. `transfers` records the business operation.
`ledger_entries` records one debit and one credit per completed transfer. `idempotency_keys`
binds a client key to a request hash and stored response. The external funding account is allowed
to go negative so deposits remain balanced ledger movements.

## Failure scenarios

- A duplicate request returns the stored response; a changed body with the same key is rejected.
- Account rows are locked in a stable order to prevent double-spending and avoid deadlocks.
- Any unexpected database failure rolls back the key, transfer, entries, and balance updates.
- Database constraints reject zero entries, non-positive transfers, and invalid account balances.

## Open questions

Authentication will define the real client identity used to scope idempotency keys. Additional
currencies need one external funding account per currency. Retention and archival rules remain to
be decided.
