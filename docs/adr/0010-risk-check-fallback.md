# ADR 0010: Score transfers before locking, and fall back when the risk service fails

- Status: Accepted
- Date: 2026-11-24

## Context

Every account-to-account transfer is scored by `risk-service` before money moves. That puts a
network call on the transfer path. Two questions follow: where in the request the call happens,
and what a transfer does when `risk-service` is slow, failing, or down.

## Decision

- **Before the locking transaction.** payments-api reads the risk features (account age,
  transfers in the last hour, 30-day average amount, new-recipient flag) in a short read-only
  transaction, closes it, calls `POST /v1/score`, and only then opens the transaction that locks
  the accounts. Replays of a completed idempotency key skip the call. Deposits are not scored.
- **Bounded wait.** The call has a 150 ms timeout. A pybreaker circuit breaker opens after 5
  consecutive failures and fails fast for 30 seconds before letting a trial call through.
- **Fallback policy.** When the call times out, errors, returns an invalid body, or the breaker
  is open, amounts up to `RISK_FALLBACK_APPROVE_MAX_MINOR` (default 10,000 minor units, $100) are
  approved and larger amounts become `PENDING_REVIEW`. The fallback never declines. Fallback
  decisions are stored with `risk_model_version = "fallback"` and no score.
- **Outcomes.** `DECLINE` records a `DECLINED` transfer with no ledger entries (422).
  `REVIEW` records `PENDING_REVIEW` with no ledger entries (202). An admin approves it with
  `POST /admin/transfers/{id}/approve`, which locks the transfer and accounts and moves the money
  through the same path as a normal transfer, checking the balance again.

## Alternatives

- **Call inside the transaction:** simpler code, but every transfer would hold its account locks
  for up to the full timeout, and a slow model would block all other transfers on those accounts.
- **Fail closed (reject all transfers while risk-service is down):** safest against fraud, but a
  model outage would become a payments outage. The rate limiter fails closed (ADR 0006) because
  it protects the system itself; the risk check reduces fraud loss and can degrade instead.
- **Fail open (approve everything):** keeps payments flowing but removes fraud protection exactly
  when an attacker might cause the outage.
- **Score asynchronously after the transfer:** no latency cost, but by then the money has moved.

## Consequences

Account locks are never held across a network call, and a risk-service outage costs at most
150 ms per transfer until the breaker opens, then nothing. During an outage, fraud exposure is
capped at the fallback limit per transfer, and larger transfers queue for a person. Because the
features are read before the locks are taken, two transfers racing from the same account can each
see a slightly stale velocity count; this is acceptable for a score, though it would not be for a
balance check. Each process has its own breaker, so instances open their breakers independently.
