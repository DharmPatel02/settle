"""Structured logs and distributed tracing.

Tracing is off unless OTEL_ENABLED=true. Instrumentation is installed either way: without a
configured tracer provider, OpenTelemetry's spans are cheap no-ops, so tests need no collector.
"""

import logging
import sys

import structlog
from opentelemetry import propagate, trace
from opentelemetry.context import Context
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor


def _add_trace_ids(
    _logger: object, _method: str, event_dict: structlog.typing.EventDict
) -> structlog.typing.EventDict:
    # Puts the current trace id on every log line, so a log search leads straight to the trace.
    context = trace.get_current_span().get_span_context()
    if context.is_valid:
        event_dict["trace_id"] = format(context.trace_id, "032x")
        event_dict["span_id"] = format(context.span_id, "016x")
    return event_dict


def configure_logging(service: str, level: int = logging.INFO) -> None:
    """Send every log record, including the standard library's and uvicorn's, out as JSON."""
    shared = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        _add_trace_ids,
        structlog.processors.CallsiteParameterAdder(
            {structlog.processors.CallsiteParameter.LINENO}
        ),
    ]
    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=[*shared, structlog.stdlib.ExtraAdder()],
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            lambda _l, _m, event: {"service": service, **event},
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
    )
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        uvicorn_logger = logging.getLogger(name)
        uvicorn_logger.handlers.clear()
        uvicorn_logger.propagate = True
    # httpx logs every request at INFO; traces already record those calls.
    logging.getLogger("httpx").setLevel(logging.WARNING)

    structlog.configure(
        processors=[*shared, structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )


def configure_tracing(service: str, *, enabled: bool, otlp_endpoint: str) -> None:
    if not enabled:
        return
    provider = TracerProvider(resource=Resource.create({"service.name": service}))
    provider.add_span_processor(
        BatchSpanProcessor(OTLPSpanExporter(endpoint=f"{otlp_endpoint.rstrip('/')}/v1/traces"))
    )
    trace.set_tracer_provider(provider)


def shutdown_tracing() -> None:
    """Flush buffered spans before a worker process exits."""
    provider = trace.get_tracer_provider()
    if isinstance(provider, TracerProvider):
        provider.shutdown()


def inject_context() -> dict[str, str]:
    """The current trace context as W3C `traceparent` headers (empty when not tracing)."""
    carrier: dict[str, str] = {}
    propagate.inject(carrier)
    return carrier


def extract_context(carrier: dict[str, str] | None) -> Context:
    return propagate.extract(carrier or {})
