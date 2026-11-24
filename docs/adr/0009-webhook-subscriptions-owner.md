# ADR 0009: The webhook service owns webhook subscriptions

- Status: Accepted
- Date: 2026-11-24

## Context

A subscription maps an account to a merchant URL and a signing secret. Either payments-api owns
subscriptions and replicates them to the webhook service through events, or the webhook service
owns them directly.

## Decision

The webhook service owns `webhook_subscriptions` in its own database and exposes
`POST /admin/subscriptions` and `GET /admin/subscriptions`, protected by an admin token. It also
owns `webhook_deliveries` and `processed_events`. Payments-api knows nothing about webhooks; it
only publishes transfer events.

## Alternatives

Owning subscriptions in payments-api would keep all account configuration in one place and let it
reuse API-key authentication, but it would need new event types and a replication path, and the
webhook service could deliver to a stale URL until the change arrived.

## Consequences

Subscription changes take effect immediately, and the payments ledger stays focused on money.
The trade-off is a second admin surface with its own credential. Signing secrets are stored in
plain text because HMAC signing needs the original value; production would encrypt them at rest
with a key from a secrets manager.
