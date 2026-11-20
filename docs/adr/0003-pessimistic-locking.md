# ADR 0003: Use pessimistic account locking

- Status: Accepted
- Date: 2026-11-18

## Context

Concurrent transfers can both observe the same balance and overspend it.

## Decision

Lock both account rows with `SELECT ... FOR UPDATE` in ascending UUID order before validating or
changing balances.

## Alternatives

Optimistic locking with a version column avoids waiting when collisions are rare, but callers must
detect conflicts and retry the entire transaction.

## Consequences

The transfer algorithm is direct and prevents double-spending. Transfers involving a busy account
serialize, and every code path must preserve the same lock order to avoid deadlocks.
