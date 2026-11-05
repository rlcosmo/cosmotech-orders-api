import json

import boto3
from moto import mock_aws
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from orders.consumer.handler import handler as consume
from orders.producer.handler import handler as produce
from orders.shared import tracing


@mock_aws
def test_http_sqs_and_processing_spans_share_one_trace(monkeypatch):
    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(trace, "get_tracer", lambda name, *args, **kwargs: provider.get_tracer(name))
    sqs = boto3.client("sqs", region_name="us-east-1")
    url = sqs.create_queue(QueueName="tracing-test")["QueueUrl"]
    monkeypatch.setenv("ORDERS_QUEUE_URL", url)
    resource = boto3.resource("dynamodb", region_name="us-east-1")
    table = resource.create_table(
        TableName="tracing-test",
        KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"},
                   {"AttributeName": "sk", "KeyType": "RANGE"}],
        AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"},
                              {"AttributeName": "sk", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )
    table.put_item(Item={"pk": "INVENTORY#LAB", "sk": "CATALOG", "stock": {"x": 10}})
    monkeypatch.setenv("ORDERS_TABLE_NAME", table.name)
    monkeypatch.setenv("ORDERS_PROCESSING_MODE", "complete")
    body = {"order_id": "trace-order", "items": [{"sku": "x", "quantity": 1}]}
    parent = "00-0123456789abcdef0123456789abcdef-0123456789abcdef-01"
    assert produce({"body": json.dumps(body), "headers": {"traceparent": parent,
                    "Authorization": "never-export-this-secret"}}, None, sqs_client=sqs)["statusCode"] == 202
    message = sqs.receive_message(QueueUrl=url, MessageAttributeNames=["All"])["Messages"][0]
    attributes = {key: {"stringValue": value["StringValue"]}
                  for key, value in message["MessageAttributes"].items()}
    assert set(attributes) <= {"traceparent", "tracestate"}
    assert consume({"Records": [{"messageId": message["MessageId"], "body": message["Body"],
                    "messageAttributes": attributes}]}, None, dynamodb_resource=resource) == {
        "batchItemFailures": [],
    }
    spans = exporter.get_finished_spans()
    assert {item.name for item in spans} == {
        "orders.enqueue", "orders.consume", "orders.payment", "orders.inventory", "orders.fulfillment",
    }
    assert {item.context.trace_id for item in spans} == {int(parent.split("-")[1], 16)}
    assert "never-export-this-secret" not in str([(s.attributes, s.events) for s in spans])
    provider.shutdown()


@mock_aws
def test_tracing_does_not_record_sensitive_exception_text(monkeypatch):
    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(tracing.trace, "get_tracer", lambda name: provider.get_tracer(name))
    try:
        with tracing.span("controlled-error"):
            raise RuntimeError("private-token-never-export")
    except RuntimeError:
        pass
    result = exporter.get_finished_spans()[0]
    assert result.attributes["error.type"] == "RuntimeError"
    assert result.status.status_code == trace.StatusCode.ERROR
    assert not result.events
    assert "private-token-never-export" not in str(result.attributes)
    provider.shutdown()


def test_metrics_record_failure_without_sensitive_or_unbounded_attributes(monkeypatch):
    from unittest.mock import Mock
    provider = TracerProvider()
    monkeypatch.setattr(trace, "get_tracer", lambda name: provider.get_tracer(name))
    counter, duration = Mock(), Mock()
    monkeypatch.setattr(tracing, "_span_counter", counter)
    monkeypatch.setattr(tracing, "_span_duration", duration)
    try:
        with tracing.span("fixed.operation", attributes={"private": "never-export-to-metrics"}):
            raise ValueError("private exception")
    except ValueError:
        pass
    counter.add.assert_called_once_with(1, {"operation": "fixed.operation", "span.kind": "INTERNAL", "status": "error"})
    value, attributes = duration.record.call_args.args
    assert value >= 0
    assert attributes == counter.add.call_args.args[1]
    assert "private" not in str(attributes)
    provider.shutdown()
