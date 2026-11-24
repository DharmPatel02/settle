# ADR 0008: Use Kafka for transfer events

- Status: Accepted
- Date: 2026-11-24

## Context

Transfer events fan out to the webhook service now and to other consumers later (analytics,
notifications, fraud features). Consumers need per-account ordering and the ability to re-read
history after a bug fix.

## Decision

Use Kafka (single broker in KRaft mode locally, no ZooKeeper) with topic `transfers.events`,
3 partitions, keyed by source account ID. Each consumer group tracks its own offsets and commits
them manually after its own database write. Webhook deliveries that exhaust their retries are
published to `transfers.events.dlq`.

## Alternatives

- **Amazon SQS:** fully managed and simpler to run, but a message is gone once consumed, so a new
  consumer cannot replay history, and ordering needs FIFO queues with lower throughput.
- **RabbitMQ:** flexible routing, but retention and replay are not its strengths.
- **Polling the outbox table directly from each consumer:** no broker, but every consumer would
  load the payments database and depend on its schema.

## Consequences

Consumers are decoupled from payments-api and can replay from any offset. Ordering holds only
within a partition, so it is per account, not global. Kafka is another system to operate; in
production it would be a managed cluster with replication factor 3.
