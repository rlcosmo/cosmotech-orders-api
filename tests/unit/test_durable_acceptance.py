"""The optional primary journal must commit before acknowledgment and survive transport outages."""

import json
from unittest.mock import Mock

import boto3
from botocore.exceptions import ClientError
from moto import mock_aws
from test_durable_outbox_sdk import create_table

from orders.producer.handler import handler
from orders.runtime.api import normalize_request
from orders.shared.dr_outbox.dynamo_outbox import DynamoOutbox
from orders.shared.dr_outbox.outbox import Outbox

ORDER = {
    "order_id": "source-durable-one",
    "items": [{"sku": "LAB-SKU-001", "quantity": 1}],
}


@mock_aws
def test_acceptance_commits_one_record_and_duplicate_does_not_send_synchronously(
    monkeypatch,
):
    monkeypatch.setenv("ORDERS_DURABLE_OUTBOX", "true")
    table = create_table(boto3.resource("dynamodb", region_name="us-east-1"))
    journal = Outbox(DynamoOutbox(table), clock=lambda: 1000)
    unavailable_queue = Mock()
    unavailable_queue.send_message.side_effect = AssertionError(
        "Acceptance must not depend on SQS"
    )
    event = {"body": json.dumps(ORDER)}
    for _ in range(2):
        response = handler(
            event, None, sqs_client=unavailable_queue, durable_outbox=journal
        )
        assert response["statusCode"] == 202
        assert json.loads(response["body"])["acceptance"] == "durable-journal"
    unavailable_queue.send_message.assert_not_called()
    rows = list(journal.store.records())
    assert len(rows) == 1
    assert rows[0]["state"] == "PENDING"
    assert rows[0]["accepted_at"] == 1000000


@mock_aws
def test_conflicting_id_does_not_modify_durable_acceptance(monkeypatch):
    monkeypatch.setenv("ORDERS_DURABLE_OUTBOX", "true")
    journal = Outbox(
        DynamoOutbox(create_table(boto3.resource("dynamodb", region_name="us-east-1")))
    )
    original = journal.accept(ORDER)
    changed = {**ORDER, "items": [{"sku": "LAB-SKU-001", "quantity": 2}]}
    response = handler({"body": json.dumps(changed)}, None, durable_outbox=journal)
    assert response["statusCode"] == 409
    assert list(journal.store.records()) == [original]


def test_journal_failure_never_acknowledges_or_exposes_sdk_message(monkeypatch, caplog):
    monkeypatch.setenv("ORDERS_DURABLE_OUTBOX", "true")
    journal = Mock()
    journal.accept.side_effect = ClientError(
        {"Error": {"Code": "AccessDeniedException", "Message": "private-test-marker"}},
        "PutItem",
    )
    response = handler({"body": json.dumps(ORDER)}, None, durable_outbox=journal)
    assert response["statusCode"] == 503
    assert "private-test-marker" not in response["body"]
    assert "private-test-marker" not in caplog.text


def test_durable_clients_need_stable_explicit_order_ids(monkeypatch):
    monkeypatch.setenv("ORDERS_DURABLE_OUTBOX", "true")
    payload = normalize_request(
        {
            "customer_id": "legacy",
            "items": [{"product_id": "LAB-SKU-001", "quantity": 1}],
        }
    )
    assert "order_id" not in payload
    assert (
        handler({"body": json.dumps(payload)}, None, durable_outbox=Mock())[
            "statusCode"
        ]
        == 400
    )
