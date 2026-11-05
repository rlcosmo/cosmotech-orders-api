"""Optional OpenTelemetry propagation without recording order bodies or secrets."""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager

try:
    from opentelemetry import propagate, trace
    from opentelemetry.trace import SpanKind, StatusCode
except ImportError:
    propagate = trace = None


@contextmanager
def span(name: str, carrier: dict[str, str] | None = None, *, kind: str = "INTERNAL", attributes: dict | None = None) -> Iterator:
    """Create a span, recording only exception types on failure."""
    if trace is None:
        yield
        return
    parent = propagate.extract(carrier) if carrier else None
    with trace.get_tracer("cosmotech.orders").start_as_current_span(
        name, context=parent, record_exception=False, set_status_on_exception=False,
        kind=getattr(SpanKind, kind), attributes=attributes,
    ) as current:
        try:
            yield current
        except Exception as error:
            current.set_attribute("error.type", type(error).__name__)
            current.set_status(StatusCode.ERROR)
            raise


def trace_fields() -> dict[str, str]:
    """Return correlation identifiers without request payloads or auth data."""
    if trace is None:
        return {}
    context = trace.get_current_span().get_span_context()
    return {"trace_id": format(context.trace_id, "032x"), "span_id": format(context.span_id, "016x")} if context.is_valid else {}


def inject_trace() -> dict[str, str]:
    """Return W3C propagation fields only, never arbitrary request headers."""
    carrier: dict[str, str] = {}
    if propagate is not None:
        propagate.inject(carrier)
    return {key: value for key, value in carrier.items() if key in {"traceparent", "tracestate"}}


def configure_tracing(service_name: str) -> None:
    """Configure the container's OTLP exporter when an endpoint is supplied."""
    if not os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT"):
        return
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    provider = TracerProvider(resource=Resource.create({"service.name": service_name}))
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(timeout=3)))
    trace.set_tracer_provider(provider)
