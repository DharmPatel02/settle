# Settle Payments Design

## Problem

Settle lets a client create wallets, deposit funds, transfer funds, and inspect an account's
append-only ledger. A request retry must never move money twice, and concurrent transfers must
never spend the same balance twice. Merchants are told about transfers through signed webhooks
that survive crashes and outages on either side.

## Goals and non-goals

The API provides accounts, deposits, transfers, ledger history, API-key authentication, per-client
transfer rate limiting, PostgreSQL persistence, and RFC 7807 errors. Transfer events flow through
a transactional outbox to Kafka; the webhook service turns them into signed, retried merchant
webhooks with a dead-letter queue. Fraud scoring and multi-region operation are outside the
current boundary.

## Event flow

1. `payments-api` commits the transfer and an `outbox_events` row in one transaction.
2. The outbox relay publishes unpublished rows to `transfers.events` (key = source account) and
   marks them published after Kafka acknowledges.
3. The webhook consumer records the `event_id` in `processed_events` and creates one
   `webhook_deliveries` row per matching subscription, in one transaction, then commits the Kafka
   offset.
4. The delivery worker leases due deliveries, sends them with `Settle-Timestamp` and
   `Settle-Signature` (HMAC-SHA256 of `timestamp.body`), and retries failures with exponential
   backoff and jitter (1 s doubling, capped at one hour, 8 attempts).
5. Exhausted deliveries become `DEAD`, are published to `transfers.events.dlq`, and can be
   replayed with `POST /admin/dlq/{id}/replay`.

## API sketch

- All `/v1` routes require `X-API-Key`; health endpoints remain public.
- `POST /v1/accounts`; `GET /v1/accounts/{id}`
- `GET /v1/accounts/{id}/entries?cursor=&limit=`
- `POST /v1/deposits` with `Idempotency-Key`
- `POST /v1/transfers` with `Idempotency-Key`; `GET /v1/transfers/{id}`
- Webhook service (`X-Admin-Token`): `POST /admin/subscriptions`, `GET /admin/deliveries`,
  `GET /admin/dlq`, `POST /admin/dlq/{id}/replay`

## Data model

`accounts` caches the current balance. `transfers` records the business operation.
`ledger_entries` records one debit and one credit per completed transfer. Insufficient funds and
currency mismatches are recorded as `REJECTED` transfers with no ledger entries. `outbox_events`
holds events until the relay publishes them. `idempotency_keys`
binds an API client and key to a request hash and stored response. `api_clients` stores only a
SHA-256 key hash. The external funding account is allowed to go negative so deposits remain
balanced ledger movements.

## Failure scenarios

- A duplicate request returns the stored response; a changed body with the same key is rejected.
- Account rows are locked in a stable order to prevent double-spending and avoid deadlocks.
- Any unexpected database failure rolls back the key, transfer, entries, and balance updates.
- Database constraints reject zero entries, non-positive transfers, and invalid account balances.
- Redis atomically enforces a token bucket per API client; transfers fail closed if it is down.
- Kafka down: events wait in the outbox and are published when it recovers. Transfers continue.
- Relay or consumer crash: events and messages are redelivered; `processed_events` makes the
  repeat a no-op, so merchants may see a webhook twice but deliveries are never lost.
- Merchant down: deliveries back off and retry; after 8 attempts they go to the DLQ for replay.
- Delivery worker crash mid-send: the lease expires and another pass retries the delivery.

## Open questions

Key rotation and revocation workflows are not defined yet. Webhook secrets are stored in plain
text and published outbox rows are never pruned. Additional currencies need one external
funding account per currency. Retention and archival rules remain to be decided.
