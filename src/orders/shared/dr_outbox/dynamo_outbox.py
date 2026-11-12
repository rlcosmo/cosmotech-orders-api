"""Boto3 persistence adapter; conditional writes, strong reads and paginated queries."""

from __future__ import annotations

from boto3.dynamodb.conditions import Attr, Key

from .outbox import SHARDS, LostLease


class DynamoOutbox:
    def __init__(self, table):
        self.table = table
        self.conditional = table.meta.client.exceptions.ConditionalCheckFailedException

    def put_if_absent(self, record):
        try:
            self.table.put_item(
                Item=record, ConditionExpression=Attr("order_id").not_exists()
            )
            return record
        except self.conditional:
            existing = self.table.get_item(
                Key={"order_id": record["order_id"]}, ConsistentRead=True
            ).get("Item")
            if not existing:
                raise RuntimeError(
                    "Conflicting write cannot be verified; do not acknowledge"
                )
            return existing

    def claim(self, key, token, epoch, now, until):
        try:
            result = self.table.update_item(
                Key={"order_id": key},
                ConditionExpression=Attr("state").eq("PENDING")
                & Attr("due_at").lte(now)
                & (Attr("epoch").not_exists() | Attr("epoch").lte(epoch)),
                UpdateExpression="SET lease_token = :token, epoch = :epoch, due_at = :until ADD attempts :one",
                ExpressionAttributeValues={
                    ":token": token,
                    ":epoch": epoch,
                    ":until": until,
                    ":one": 1,
                },
                ReturnValues="ALL_NEW",
            )
            return result["Attributes"]
        except self.conditional:
            return None

    def sent(self, key, token, epoch, now):
        try:
            self.table.update_item(
                Key={"order_id": key},
                ConditionExpression=Attr("lease_token").eq(token)
                & Attr("epoch").eq(epoch)
                & Attr("state").eq("PENDING")
                & Attr("due_at").gt(now),
                UpdateExpression="SET #s = :sent, published_at = :now REMOVE lease_token, dispatch_bucket, due_at",
                ExpressionAttributeNames={"#s": "state"},
                ExpressionAttributeValues={":sent": "SENT", ":now": now},
            )
        except self.conditional as error:
            raise LostLease(
                "Publication record not changed by expired/stale owner"
            ) from error

    def due(self, now, limit=100):
        if not 1 <= limit <= 1000:
            raise ValueError("Bounded page scan required")
        emitted = 0
        paginator = self.table.meta.client.get_paginator("query")
        for shard in range(SHARDS):
            # GSIs are eventually consistent. Delay is safe: the durable record remains pending.
            for page in paginator.paginate(
                TableName=self.table.name,
                IndexName="dispatch-due",
                KeyConditionExpression=Key("dispatch_bucket").eq(
                    "pending-" + str(shard)
                )
                & Key("due_at").lte(now),
                ProjectionExpression="order_id",
                PaginationConfig={"PageSize": 25},
            ):
                for item in page["Items"]:
                    yield item["order_id"]
                    emitted += 1
                    if emitted >= limit:
                        return

    def records(self):
        paginator = self.table.meta.client.get_paginator("scan")
        for page in paginator.paginate(TableName=self.table.name, ConsistentRead=True):
            yield from page.get("Items", [])

    def schedule_replay(self, record, *, epoch, now):
        """Called only after external fencing/reconciliation. Never blind-overwrite.

        Missing records are imported conditionally; existing completed business
        orders must already have been excluded by the reconciler. A new epoch is
        mandatory to reset a possibly SENT record, including stale send leases.
        """
        if type(epoch) is not int or epoch < 1 or type(now) is not int or now < 0:
            raise ValueError("Explicit replay epoch/time required")
        from .outbox import bucket
        from .reconciliation import immutable

        r = immutable(record)
        proposed = {
            **r,
            "state": "PENDING",
            "dispatch_bucket": bucket(r["order_id"]),
            "due_at": now,
            "epoch": epoch,
            "attempts": 0,
        }
        try:
            self.table.put_item(
                Item=proposed, ConditionExpression=Attr("order_id").not_exists()
            )
            return "imported"
        except self.conditional:
            try:
                self.table.update_item(
                    Key={"order_id": r["order_id"]},
                    ConditionExpression=Attr("payload_hash").eq(r["payload_hash"])
                    & Attr("event_json").eq(r["event_json"])
                    & (Attr("epoch").not_exists() | Attr("epoch").lt(epoch)),
                    UpdateExpression="SET #s = :pending, dispatch_bucket = :bucket, due_at = :now, epoch = :epoch REMOVE lease_token, published_at",
                    ExpressionAttributeNames={"#s": "state"},
                    ExpressionAttributeValues={
                        ":pending": "PENDING",
                        ":bucket": bucket(r["order_id"]),
                        ":now": now,
                        ":epoch": epoch,
                    },
                )
                return "rescheduled"
            except self.conditional:
                existing = self.table.get_item(
                    Key={"order_id": r["order_id"]}, ConsistentRead=True
                ).get("Item", {})
                if (
                    existing.get("payload_hash") == r["payload_hash"]
                    and existing.get("event_json") == r["event_json"]
                    and existing.get("epoch") == epoch
                ):
                    return "already_scheduled"  # retry must not reset a current active lease
                raise LostLease("Conflicting payload/newer replay epoch; no mutation")
