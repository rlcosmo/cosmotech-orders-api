"""Read-only scheduled connection checks with bounded HTTP calls and correlated traces."""
from __future__ import annotations

import os
import time
import urllib.error
import urllib.request
import uuid
from threading import Event, Thread
from urllib.parse import urlsplit

from orders.shared.logging import get_logger
from orders.shared.tracing import inject_trace, span, trace_fields

logger = get_logger(__name__)


def receive_ping(service: str, carrier: dict[str, str]) -> dict:
    """Observe an inbound synthetic request without accepting business data."""
    with span("synthetic.receive", carrier, kind="SERVER", attributes={"synthetic": True, "http.request.method": "GET", "http.route": "/diagnostics/ping"}):
        logger.info("synthetic_inbound", extra={"event": "synthetic_inbound", "service": service,
            "direction": "inbound", "status_code": 200, "synthetic": True, **trace_fields()})
        return {"service": service, "synthetic": True, "status": "ok", **trace_fields()}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def check_target(service: str, direction: str, url: str, opener=None) -> bool:
    """Log status/latency only; never response bodies, arbitrary headers or credentials."""
    parsed = urlsplit(url)
    start = time.monotonic()
    with span("synthetic." + direction, kind="CLIENT", attributes={"synthetic": True,
            "http.request.method": "GET", "server.address": parsed.hostname or "", "url.scheme": parsed.scheme}) as current:
        headers = {**inject_trace(), "User-Agent": "CosmoTechSynthetic/1.0", "X-Synthetic-Source": service}
        request = urllib.request.Request(url, headers=headers)
        status, error_type = 0, ""
        try:
            client = opener or urllib.request.build_opener(NoRedirect())
            with client.open(request, timeout=5) as response:
                status = response.status
                response.read(4096)
        except urllib.error.HTTPError as error:
            status, error_type = error.code, type(error).__name__
        except (OSError, ValueError) as error:
            error_type = type(error).__name__
        succeeded = 200 <= status < 300
        if current is not None:
            current.set_attribute("http.response.status_code", status)
            if not succeeded:
                from opentelemetry.trace import StatusCode
                current.set_status(StatusCode.ERROR)
        fields = {"event": "synthetic_connection", "service": service, "direction": direction,
            "target": parsed.hostname, "status_code": status, "duration_ms": round((time.monotonic()-start)*1000, 2),
            "synthetic": True, "result": "success" if succeeded else "failure", **trace_fields()}
        if error_type:
            fields["error_type"] = error_type
        logger.log(20 if succeeded else 40, "synthetic_connection", extra=fields)
        return succeeded


def run_cycle(service: str, peer_url: str, external_url: str) -> None:
    """One internal call and one external call, at most five seconds each."""
    with span("synthetic.cycle", attributes={"synthetic": True}):
        logger.info("synthetic_heartbeat", extra={"event": "synthetic_heartbeat", "service": service,
            "synthetic": True, "interval_seconds": 300, "cycle_id": str(uuid.uuid4()), **trace_fields()})
        check_target(service, "internal", peer_url)
        check_target(service, "external", external_url)


def start_connectivity_checks(service: str) -> Event | None:
    """Start checks only when enabled by versioned application configuration."""
    if os.getenv("SYNTHETIC_INTERVAL_SECONDS") != "300":
        return None
    peer = os.environ["SYNTHETIC_PEER_URL"]
    external = os.environ["SYNTHETIC_EXTERNAL_URL"]
    peer_target, external_target = urlsplit(peer), urlsplit(external)
    allowed_peers = {"orders-api.orders-api.svc.cluster.local", "orders-worker.orders-worker.svc.cluster.local"}
    if (peer_target.scheme != "http" or peer_target.hostname not in allowed_peers
            or peer_target.port != 8000 or peer_target.path != "/diagnostics/ping"
            or peer_target.username or peer_target.query or peer_target.fragment):
        raise ValueError("Invalid fixed internal synthetic target")
    if external != "https://example.com/" or external_target.username:
        raise ValueError("Invalid fixed external synthetic target")
    stop = Event()

    def run() -> None:
        deadline = time.monotonic()
        while not stop.is_set():
            run_cycle(service, peer, external)
            deadline += 300
            stop.wait(max(0, deadline-time.monotonic()))

    Thread(target=run, daemon=True, name="synthetic-connectivity").start()
    return stop
