"""API Gateway compatible Lambda handler that enqueues validated orders."""

import base64
import binascii
import json
import os
from datetime import UTC, datetime
from typing import Any

import boto3
from botocore.config import Config

from orders.shared.logging import get_logger
from orders.shared.tracing import inject_trace, span
from orders.shared.validation import (
    MAX_REQUEST_BYTES,
    ORDER_EVENT_VERSION,
    OrderValidationError,
    validate_order,
)

logger = get_logger(__name__)


def _response(status_code: int, body: dict[str, Any]) -> dict[str, Any]:
    return {
        "statusCode": status_code,
        "headers": {"content-type": "application/json"},
        "body": json.dumps(body),
    }


def handler(
    event: dict[str, Any], context: Any, *, sqs_client: Any | None = None, durable_outbox: Any | None = None
) -> dict[str, Any]:
    """Validate an API request and publish the complete order event to SQS."""
    try:
        raw_body = event.get("body")
        if event.get("isBase64Encoded") is True:
            if not isinstance(raw_body, str):
                raise OrderValidationError("Request body must be text")
            if len(raw_body) > ((MAX_REQUEST_BYTES + 2) // 3) * 4:
                raise OrderValidationError("Request body exceeds the maximum size")
            try:
                raw_body = base64.b64decode(raw_body, validate=True).decode("utf-8")
            except (binascii.Error, UnicodeDecodeError, ValueError) as exc:
                raise OrderValidationError("Request body is not valid Base64-encoded UTF-8") from exc
        if isinstance(raw_body, str):
            if len(raw_body.encode("utf-8")) > MAX_REQUEST_BYTES:
                raise OrderValidationError("Request body exceeds the maximum size")
            payload = json.loads(raw_body)
        elif raw_body is None:
            payload = None
        else:
            raise OrderValidationError("Request body must be text")
        order = validate_order(payload)
    except (json.JSONDecodeError, OrderValidationError, AttributeError, TypeError):
        logger.info("order_rejected", extra={"event": "order_rejected", "reason": "invalid_request"})
        return _response(400, {"error": "Invalid request"})

    if os.getenv("ORDERS_DURABLE_OUTBOX") == "true":
        try:
            from orders.producer.durable import get_outbox
            from orders.shared.dr_outbox.outbox import Conflict
        except ImportError:
            return _response(503, {"error": "Durable acceptance not configured"})
        try:
            journal = durable_outbox if durable_outbox is not None else get_outbox()
            stored = journal.accept(order)
            logger.info("order_accepted", extra={"event": "order_accepted", "acceptance": "durable-journal"})
            return _response(202, {"order_id": stored["order_id"], "status": "PENDING", "acceptance": "durable-journal"})
        except Conflict:
            return _response(409, {"error": "Order ID conflicts with accepted content"})
        except Exception as error:  # noqa: BLE001 - HTTP boundary must not disclose SDK errors.
            logger.error("durable_acceptance_failed", extra={"event": "durable_acceptance_failed", "error_type": type(error).__name__})
            return _response(503, {"error": "Durable acceptance unavailable"})

    queue_url = os.environ.get("ORDERS_QUEUE_URL")
    if not queue_url:
        logger.error("queue_configuration_missing", extra={"event": "configuration_error"})
        return _response(500, {"error": "Service configuration error"})

    client = sqs_client or boto3.client(
        "sqs",
        region_name=os.getenv("AWS_REGION", "us-east-1"),
        config=Config(
            connect_timeout=2,
            read_timeout=3,
            retries={"max_attempts": 2, "mode": "standard"},
        ),
    )
    message = {
        "event_type": "order.created",
        "version": ORDER_EVENT_VERSION,
        "order_id": order["order_id"],
        "order": order,
        "created_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
    }
    try:
        headers = event.get("headers") or {}
        carrier = {
            key.lower(): value for key, value in headers.items()
            if isinstance(key, str) and key.lower() in {"traceparent", "tracestate"}
            and isinstance(value, str)
        }
        with span("orders.enqueue", carrier):
            trace_attributes = {
                key: {"DataType": "String", "StringValue": value}
                for key, value in inject_trace().items()
            }
            parameters = {
                "QueueUrl": queue_url,
                "MessageBody": json.dumps(message, separators=(",", ":")),
            }
            if trace_attributes:
                parameters["MessageAttributes"] = trace_attributes
            client.send_message(**parameters)
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "order_enqueue_failed",
            extra={"event": "order_enqueue_failed", "error_type": type(exc).__name__},
        )
        return _response(503, {"error": "Order could not be queued"})

    logger.info("order_accepted", extra={"event": "order_accepted"})
    return _response(202, {"order_id": order["order_id"], "status": "PENDING"})
