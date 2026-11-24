# ADR 0007: Publish transfer events through a transactional outbox

- Status: Accepted
- Date: 2026-11-24

## Context

Other services need to know when a transfer completes or is rejected. The transfer is committed
to PostgreSQL and the event goes to Kafka. Writing to both directly (a dual write) can leave them
disagreeing: the database commit succeeds and the Kafka publish fails, so the event is lost; or
the publish succeeds and the commit then rolls back, so consumers hear about a transfer that never
happened.

## Decision

`TransferService` inserts an `outbox_events` row in the same transaction as the transfer, its
ledger entries, and the balance updates. A separate relay process
(`python -m payments_api.outbox_relay`) polls unpublished rows with
`FOR UPDATE SKIP LOCKED`, publishes them to `transfers.events` with `acks=all`, waits for the
acknowledgements, and only then sets `published_at`. The Kafka key is the source account ID, so
events for one account stay in order within a partition.

## Alternatives

- **Publish right after commit:** simple, but a crash between commit and publish loses the event.
- **Change data capture (Debezium):** no polling and no extra table, but it adds Kafka Connect
  and couples consumers to table layout.
- **Distributed transaction (2PC):** Kafka does not take part in XA transactions with PostgreSQL.

## Consequences

An event exists if and only if its transfer committed. Delivery is at least once: if the relay
dies after Kafka acknowledges but before it commits, the batch is published again, so every
consumer must deduplicate by `event_id`. Events reach Kafka after a short delay (the poll
interval, 500 ms by default), and the outbox table needs periodic cleanup of published rows.
Several relays can run at once because `SKIP LOCKED` hands each one different rows.
