# ADR 0004: Store idempotency in PostgreSQL

- Status: Accepted
- Date: 2026-11-18

## Context

Clients retry timed-out payment requests, and a retry must return the original result without
moving money again.

## Decision

Store the client ID, key, canonical request hash, status, and response in PostgreSQL. Claim the key
inside the same transaction as the transfer.

## Alternatives

Redis is fast but introduces a second system whose state cannot commit atomically with the ledger.
An in-process cache is lost on restart and is not shared between instances.

## Consequences

The ledger and idempotency outcome commit or roll back together. The request hash detects accidental
key reuse with a different body. Old keys require a safe expiry process.
