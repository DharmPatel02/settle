# ADR 0006: Fail closed when transfer rate limiting is unavailable

- Status: Accepted
- Date: 2026-11-21

## Context

Redis holds each API client's transfer token bucket. If Redis cannot be reached, the application
cannot tell whether that client has exhausted its allowance.

## Decision

Reject new transfer requests with `503 Service Unavailable` when the limiter fails. Account reads,
account creation, deposits, and health endpoints remain available.

## Alternatives

Failing open would keep transfers available during a Redis outage, but a client could send an
unlimited burst precisely when the protection is offline. A local fallback bucket would differ
between application instances and would not enforce one global limit.

## Consequences

Transfer safety and abuse control are preserved, but Redis becomes a transfer-path dependency.
Alerts and a highly available Redis deployment will be needed before production use.
