# ADR 0005: Keep transfer work in one synchronous database transaction

- Status: Accepted
- Date: 2026-11-18

## Context

The key claim, account locks, transfer, ledger entries, balances, and stored response must represent
one indivisible operation.

## Decision

Use one SQLAlchemy transaction and synchronous sessions. FastAPI runs plain `def` handlers in its
thread pool, so blocking database work does not block the event loop. Do external calls before this
transaction.

## Alternatives

Async SQLAlchemy can support high I/O concurrency but adds async boundaries to locking logic.
Multiple transactions shorten lock duration but can expose partial money movements.

## Consequences

Rollback removes every partial write, and the code mirrors the database operation closely. Each
concurrent request still needs a pool connection, and slow work must never be added while locks are
held.
