import unittest
from unittest.mock import patch

import boto3
from moto import mock_aws

from orders.shared.dr_outbox.dynamo_outbox import DynamoOutbox
from orders.shared.dr_outbox.outbox import LostLease, Outbox, make_record


def create_table(resource, name="cosmotech-orders-outbox"):
    return resource.create_table(
        TableName=name,
        KeySchema=[{"AttributeName": "order_id", "KeyType": "HASH"}],
        AttributeDefinitions=[
            {"AttributeName": "order_id", "AttributeType": "S"},
            {"AttributeName": "dispatch_bucket", "AttributeType": "S"},
            {"AttributeName": "due_at", "AttributeType": "N"},
        ],
        BillingMode="PAY_PER_REQUEST",
        GlobalSecondaryIndexes=[
            {
                "IndexName": "dispatch-due",
                "KeySchema": [
                    {"AttributeName": "dispatch_bucket", "KeyType": "HASH"},
                    {"AttributeName": "due_at", "KeyType": "RANGE"},
                ],
                "Projection": {"ProjectionType": "KEYS_ONLY"},
            }
        ],
    )


@mock_aws
class DynamoAdapterTests(unittest.TestCase):
    def setUp(self):
        self.resource = boto3.resource("dynamodb", region_name="us-east-1")
        self.table = create_table(self.resource)
        self.store = DynamoOutbox(self.table)
        self.now = 1000
        self.box = Outbox(self.store, clock=lambda: self.now)
        self.order = {
            "order_id": "dr-sdk-one",
            "items": [{"sku": "LAB-SKU-001", "quantity": 1}],
        }

    def test_real_sdk_conditional_put_consistent_duplicate_and_new_client_restart(self):
        first = self.box.accept(self.order)
        fresh = DynamoOutbox(
            boto3.resource("dynamodb", region_name="us-east-1").Table(self.table.name)
        )
        again = Outbox(fresh, clock=lambda: 2000).accept(self.order)
        self.assertEqual(first["event_json"], again["event_json"])
        self.assertEqual(len(list(fresh.records())), 1)

    def test_index_query_and_pagination_are_bounded_without_lost_records(self):
        with self.table.batch_writer() as batch:
            for i in range(240):
                batch.put_item(
                    Item=make_record(
                        {**self.order, "order_id": "dr-page-" + str(i)}, 1000
                    )
                )
        due = list(self.store.due(1000000, limit=240))
        self.assertEqual(len(due), 240)
        self.assertEqual(len(set(due)), 240)
        self.assertEqual(len(list(self.store.due(1000000, limit=5))), 5)

    def test_lease_takeover_rejects_stale_mark_sent_and_older_epoch(self):
        self.box.accept(self.order)
        self.assertIsNotNone(self.store.claim("dr-sdk-one", "old", 1, 1000000, 1060000))
        self.assertIsNone(self.store.claim("dr-sdk-one", "other", 1, 1000000, 1060000))
        self.assertIsNotNone(self.store.claim("dr-sdk-one", "new", 2, 1061000, 1121000))
        with self.assertRaises(LostLease):
            self.store.sent("dr-sdk-one", "old", 1, 1062000)
        self.store.sent("dr-sdk-one", "new", 2, 1062000)
        r = self.table.get_item(Key={"order_id": "dr-sdk-one"})["Item"]
        self.assertEqual(r["state"], "SENT")
        self.assertNotIn("dispatch_bucket", r)

    def test_successful_sqs_send_unmarked_by_crash_is_recoverable_at_least_once(self):
        sqs = boto3.client("sqs", region_name="us-east-1")
        url = sqs.create_queue(QueueName="dr-test")["QueueUrl"]

        class Q:
            def send_message(inner, **args):
                return sqs.send_message(QueueUrl=url, **args)

        self.box.accept(self.order)
        with patch.object(
            self.store, "sent", side_effect=ConnectionError("Crash after SQS commit")
        ), self.assertRaises(ConnectionError):
            self.box.publish("dr-sdk-one", Q(), epoch=1)
        self.now += 61
        self.box.publish("dr-sdk-one", Q(), epoch=2)
        messages = sqs.receive_message(QueueUrl=url, MaxNumberOfMessages=10)["Messages"]
        self.assertEqual(len(messages), 2)
        self.assertEqual(messages[0]["Body"], messages[1]["Body"])
        self.assertEqual(
            self.table.get_item(Key={"order_id": "dr-sdk-one"})["Item"]["state"], "SENT"
        )

    def test_replay_new_epoch_is_conditional_and_rerun_does_not_reset_active_lease(
        self,
    ):
        record = self.box.accept(self.order)
        self.assertEqual(
            self.store.schedule_replay(record, epoch=2, now=1000000), "rescheduled"
        )
        claimed = self.store.claim("dr-sdk-one", "active", 2, 1000000, 1060000)
        self.assertEqual(
            self.store.schedule_replay(record, epoch=2, now=1000001),
            "already_scheduled",
        )
        current = self.table.get_item(Key={"order_id": "dr-sdk-one"})["Item"]
        self.assertEqual(current["due_at"], claimed["due_at"])
        with self.assertRaises(LostLease):
            self.store.schedule_replay(record, epoch=1, now=1000001)

    def test_expired_owner_cannot_mark_sent_even_without_takeover(self):
        self.box.accept(self.order)
        self.store.claim("dr-sdk-one", "expired", 1, 1000000, 1000001)
        with self.assertRaises(LostLease):
            self.store.sent("dr-sdk-one", "expired", 1, 1000001)
