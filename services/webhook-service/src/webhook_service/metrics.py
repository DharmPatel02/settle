"""Prometheus metrics for the webhook consumer and delivery worker."""

from prometheus_client import Counter, Gauge, Histogram

EVENTS_CONSUMED = Counter(
    "settle_events_consumed_total",
    "Kafka transfer events handled by the consumer.",
    ["result"],  # processed, duplicate, invalid
)
WEBHOOK_DELIVERIES = Counter(
    "settle_webhook_deliveries_total",
    "Webhook delivery attempts by result.",
    ["result"],  # succeeded, retry, dead
)
WEBHOOK_DELIVERY_DURATION = Histogram(
    "settle_webhook_delivery_duration_seconds",
    "Time for one signed webhook HTTP call to a merchant, success or not.",
    buckets=(0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5),
)
DLQ_SIZE = Gauge("settle_dlq_size", "Deliveries in the DEAD state waiting for replay.")
DELIVERY_BACKLOG = Gauge(
    "settle_webhook_backlog", "PENDING deliveries that are due now and not yet attempted."
)
