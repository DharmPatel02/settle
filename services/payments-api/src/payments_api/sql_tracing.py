"""One trace span per SQL statement, from SQLAlchemy's cursor events.

The OpenTelemetry SQLAlchemy instrumentation does not support SQLAlchemy 2.1 yet, so this does
the small part we need. Statement text is recorded; bound parameters (which hold customer data)
are not.
"""

from typing import Any

from opentelemetry import trace
from opentelemetry.trace import SpanKind, Status, StatusCode
from sqlalchemy import Engine, event

_tracer = trace.get_tracer(__name__)
_SPANS = "settle_sql_spans"


def trace_sql(engine: Engine) -> None:
    @event.listens_for(engine, "before_cursor_execute")
    def _start(conn: Any, _cursor: Any, statement: str, *_args: Any) -> None:
        operation = statement.split(None, 1)[0].upper() if statement.strip() else "SQL"
        span = _tracer.start_span(
            operation,
            kind=SpanKind.CLIENT,
            attributes={"db.system": "postgresql", "db.statement": statement[:2000]},
        )
        conn.info.setdefault(_SPANS, []).append(span)

    @event.listens_for(engine, "after_cursor_execute")
    def _end(conn: Any, *_args: Any) -> None:
        spans = conn.info.get(_SPANS)
        if spans:
            spans.pop().end()

    @event.listens_for(engine, "handle_error")
    def _error(context: Any) -> None:
        spans = context.connection.info.get(_SPANS) if context.connection is not None else None
        if spans:
            span = spans.pop()
            span.set_status(Status(StatusCode.ERROR, type(context.original_exception).__name__))
            span.end()
