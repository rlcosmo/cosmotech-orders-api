"""Small container API preserving the existing EKS health and order routes."""

from __future__ import annotations

import json
import os
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any
from urllib.parse import unquote

from botocore.exceptions import BotoCoreError, ClientError

from orders.consumer.repository import DynamoDBOrderRepository, create_dynamodb_resource
from orders.producer.handler import handler
from orders.runtime.connectivity import receive_ping, start_connectivity_checks
from orders.runtime.release import VERSION
from orders.shared.tracing import configure_tracing
from orders.shared.validation import MAX_REQUEST_BYTES


def normalize_request(payload: Any) -> Any:
    """Support legacy product_id requests and explicit v2 idempotency IDs."""
    if isinstance(payload, dict) and "customer_id" in payload and "order_id" not in payload:
        payload = {**payload, "order_id": str(uuid.uuid4())}
    if isinstance(payload, dict) and isinstance(payload.get("items"), list):
        payload = {**payload, "items": [
            {"sku": item.get("sku", item.get("product_id")), "quantity": item.get("quantity")}
            if isinstance(item, dict) else item for item in payload["items"]
        ]}
    return payload


def main() -> None:
    """Serve serially so the injected DynamoDB resource remains thread-local."""
    configure_tracing("orders-api")
    if not os.getenv("ORDERS_QUEUE_URL") and os.getenv("SQS_QUEUE_URL"):
        os.environ["ORDERS_QUEUE_URL"] = os.environ["SQS_QUEUE_URL"]
    table_name = os.getenv("ORDERS_TABLE_NAME") or os.environ["DYNAMODB_TABLE"]
    resource = create_dynamodb_resource(region_name=os.getenv("AWS_DEFAULT_REGION", "us-east-1"))
    repository = DynamoDBOrderRepository(table_name, dynamodb_resource=resource,
                                         key_schema=os.getenv("ORDERS_KEY_SCHEMA", "legacy"))

    class RequestHandler(BaseHTTPRequestHandler):
        def setup(self):
            self.request.settimeout(5)
            super().setup()

        def log_message(self, format, *args):
            # Request paths may contain user identifiers. Use application logs.
            return

        def respond(self, code: int, body: Any) -> None:
            encoded = json.dumps(body, default=str).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def do_GET(self):
            if self.path == "/diagnostics/ping":
                carrier = {k.lower():v for k,v in self.headers.items() if k.lower() in {"traceparent", "tracestate"}}
                return self.respond(200, receive_ping("orders-api", carrier))
            if self.path in {"/health/live", "/health/ready"}:
                return self.respond(200, {"status": "ready"})
            if self.path == "/version":
                return self.respond(200, {"service": "orders-api", "version": VERSION})
            if self.path.startswith("/orders/"):
                order_id = unquote(self.path.removeprefix("/orders/"))
                if not 0 < len(order_id) <= 128 or order_id.startswith(("INVENTORY#", "IDEMPOTENCY#")):
                    return self.respond(400, {"error": "Invalid order ID"})
                try:
                    item = repository.table.get_item(Key=repository._key(order_id)).get("Item")
                    return self.respond(200 if item else 404, item or {"error": "Order not found"})
                except (BotoCoreError, ClientError):
                    return self.respond(503, {"error": "Order storage unavailable"})
            return self.respond(404, {"error": "Not found"})

        def do_POST(self):
            if self.path != "/orders":
                return self.respond(404, {"error": "Not found"})
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= MAX_REQUEST_BYTES:
                    return self.respond(400, {"error": "Invalid request size"})
                payload = normalize_request(json.loads(self.rfile.read(length)))
            except (ValueError, TypeError):
                return self.respond(400, {"error": "Invalid request"})
            result = handler({"body": json.dumps(payload), "headers": dict(self.headers)}, None)
            self.respond(result["statusCode"], json.loads(result["body"]))

    server = HTTPServer(("0.0.0.0", 8000), RequestHandler)
    start_connectivity_checks("orders-api")
    server.serve_forever()


if __name__ == "__main__":
    main()
