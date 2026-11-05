"""Optional OpenTelemetry propagation without recording order bodies or secrets."""

from __future__ import annotations

import os
import time
from collections.abc import Iterator
from contextlib import contextmanager

try:
    from opentelemetry import propagate, trace
    from opentelemetry.trace import SpanKind, StatusCode
except ImportError:
    propagate = trace = None


_span_counter = None
_span_duration = None


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
        started = time.monotonic()
        try:
            yield current
        except Exception as error:
            current.set_attribute("error.type", type(error).__name__)
            current.set_status(StatusCode.ERROR)
            raise
        finally:
            if _span_counter is not None:
                # Span names/kinds are fixed by source code, never URLs, IDs or payloads.
                fields = {"operation": name, "span.kind": kind,
                          "status": "error" if current.status.status_code == StatusCode.ERROR else "ok"}
                _span_counter.add(1, fields)
                _span_duration.record(time.monotonic() - started, fields)


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

    resource = Resource.create({"service.name": service_name})
    provider = TracerProvider(resource=resource)
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(timeout=3)))
    trace.set_tracer_provider(provider)

    from opentelemetry import metrics
    from opentelemetry.exporter.otlp.proto.http.metric_exporter import (
        OTLPMetricExporter,
    )
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader

    reader = PeriodicExportingMetricReader(OTLPMetricExporter(timeout=3), export_interval_millis=60000)
    metrics_provider = MeterProvider(resource=resource, metric_readers=[reader])
    metrics.set_meter_provider(metrics_provider)
    meter = metrics_provider.get_meter("cosmotech.orders")
    global _span_counter, _span_duration
    _span_counter = meter.create_counter("cosmotech.application.operations", unit="{operation}")
    _span_duration = meter.create_histogram("cosmotech.application.operation.duration", unit="s")
