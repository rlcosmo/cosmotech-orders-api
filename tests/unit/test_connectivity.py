"""Exercise bounded connection checks and prevent arbitrary synthetic targets."""
import urllib.error
from io import BytesIO
from unittest.mock import Mock, patch

import pytest

from orders.runtime.connectivity import (
    check_target,
    receive_ping,
    start_connectivity_checks,
)


def test_success_propagates_trace_and_logs_only_safe_fields():
    response = Mock(status=200)
    response.read.return_value = b"do-not-record-body"
    response.__enter__ = Mock(return_value=response)
    response.__exit__ = Mock(return_value=False)
    opener = Mock()
    opener.open.return_value = response
    with patch("orders.runtime.connectivity.inject_trace", return_value={"traceparent": "w3c-context"}), patch("orders.runtime.connectivity.logger") as logger:
        assert check_target("orders-api", "internal", "http://orders-worker.orders-worker.svc.cluster.local:8000/diagnostics/ping", opener)
    request = opener.open.call_args.args[0]
    assert request.get_header("Traceparent") == "w3c-context"
    assert opener.open.call_args.kwargs["timeout"] == 5
    fields = logger.log.call_args.kwargs["extra"]
    assert fields["status_code"] == 200 and fields["direction"] == "internal"
    assert "do-not-record-body" not in str(fields)


def test_http_failure_is_reported_without_response_or_auth_values():
    opener = Mock()
    opener.open.side_effect = urllib.error.HTTPError("https://example.com/", 503, "failed", {}, BytesIO(b"sensitive response"))
    with patch("orders.runtime.connectivity.logger") as logger:
        assert not check_target("orders-worker", "external", "https://example.com/", opener)
    fields = logger.log.call_args.kwargs["extra"]
    assert fields["status_code"] == 503 and fields["result"] == "failure"
    assert "sensitive response" not in str(fields)


def test_diagnostic_ping_is_read_only_and_does_not_return_request_headers():
    result = receive_ping("orders-api", {"authorization": "must-not-return"})
    assert result["service"] == "orders-api" and result["synthetic"] is True
    assert "must-not-return" not in str(result)


def test_config_cannot_redirect_synthetic_requests_to_instance_metadata(monkeypatch):
    monkeypatch.setenv("SYNTHETIC_INTERVAL_SECONDS", "300")
    monkeypatch.setenv("SYNTHETIC_PEER_URL", "http://169.254.169.254/latest/meta-data/")
    monkeypatch.setenv("SYNTHETIC_EXTERNAL_URL", "https://example.com/")
    with pytest.raises(ValueError, match="internal"):
        start_connectivity_checks("orders-api")
