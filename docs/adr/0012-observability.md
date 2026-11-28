# ADR 0012: Metrics, traces and logs, with trace context carried through the outbox

- Status: Accepted
- Date: 2026-11-25

## Context

A transfer touches eight processes: payments-api, risk-service, the outbox relay, Kafka, the
webhook consumer, the delivery worker, the merchant, and the reconciliation job that checks the
ledger. When a webhook arrives late, or a transfer is slow, an engineer has to find out where
the time went. The hard part is the asynchronous hops: the outbox row sits in Postgres until the
relay picks it up, and a delivery can wait hours between retries.

## Decision

- **Metrics:** every FastAPI service exposes `GET /metrics` (prometheus-fastapi-instrumentator:
  request rate, status class and duration per route, with millisecond buckets). Worker processes
  serve `prometheus-client` metrics on their own ports. Business metrics use the `settle_`
  prefix. Prometheus scrapes them; Grafana dashboards are JSON files in Git and provisioned at
  startup, so they are reviewed like code.
- **Traces:** OpenTelemetry, exported over OTLP to Jaeger, switched on with `OTEL_ENABLED=true`.
  The W3C `traceparent` crosses each boundary in the medium of that hop:
  - HTTP: headers, injected by the httpx instrumentation and read by the FastAPI one.
  - Outbox: a `trace_context` JSONB column on `outbox_events`, written in the same transaction
    as the transfer.
  - Kafka: message headers, set by the relay and read by the consumer.
  - Retries: a `trace_context` column on `webhook_deliveries`, so an attempt hours later joins
    the original trace.
- **Logs:** structlog renders every record, including uvicorn's and the standard library's, as
  one JSON object per line with `service`, `trace_id` and `span_id`.
- **SQL spans:** a small SQLAlchemy cursor-event hook (`sql_tracing.py`). The OpenTelemetry
  SQLAlchemy instrumentation does not support SQLAlchemy 2.1 yet. Statements are recorded,
  bound parameters are not.
- **Reconciliation:** a separate process re-checks the ledger invariant every minute in one
  `REPEATABLE READ` snapshot and exports `settle_reconciliation_mismatches`, which must be 0.

## Alternatives

- **Put the trace context inside the event payload:** no schema change, but it mixes tracing
  data into the business contract that merchants receive.
- **Start a new trace at the relay and link it:** simpler, but an engineer would have to jump
  between traces to follow one transfer.
- **One `/metrics` port shared by all processes (Pushgateway):** fewer scrape targets, but
  Pushgateway is meant for batch jobs and hides which process is down.
- **Grafana dashboards built in the UI:** fast to make, but lost when the container is recreated
  and impossible to review.

## Consequences

One transfer appears as a single trace from the API request to the merchant's response, across
six services, with the SQL statements inside it. Two nullable columns carry a few hundred bytes
of tracing data per row. Tracing is off by default and in load tests; production would sample
rather than record every request. Prometheus scrapes processes on fixed host ports locally; on
Kubernetes these become service discovery targets.
