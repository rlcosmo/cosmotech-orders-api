"""Durable acceptance and lease-based at-least-once publication.

SQS send + DynamoDB mark is NOT an atomic transaction. An uncertain send can
redeliver; stable order IDs and consumer business transactions remain mandatory.
No TTL deletes the authoritative acceptance journal.
"""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from datetime import UTC, datetime

from .control import replay_events

SHARDS = 8


class Conflict(ValueError):
    """An immutable order ID was reused with a different business payload."""


class LostLease(RuntimeError):
    """A stale publisher must not change the journal after lease takeover."""


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def order_hash(order):
    return hashlib.sha256(canonical(order).encode()).hexdigest()


def bucket(order_id):
    return "pending-" + str(
        int(hashlib.sha256(order_id.encode()).hexdigest()[:8], 16) % SHARDS
    )


def make_record(order, now):
    if set(order) != {"order_id", "items"}:
        raise ValueError("Explicit immutable order_id and items are required")
    if not isinstance(order["order_id"], str) or not isinstance(order["items"], list):
        raise ValueError("Invalid order shape")  # noqa: TRY004 - Shared order validation contract.
    order = {
        "order_id": order["order_id"].strip(),
        "items": [
            {"sku": v["sku"].strip(), "quantity": v["quantity"]} for v in order["items"]
        ],
    }
    event = {
        "event_type": "order.created",
        "version": 2,
        "order_id": order["order_id"],
        "order": order,
        "created_at": datetime.fromtimestamp(now, UTC).isoformat(),
    }
    replay_events(
        [event], set()
    )  # same v2 contract as recovery; reject invalid/reserved IDs
    body = canonical(event)
    if len(body.encode()) > 256 * 1024:
        raise ValueError("Order event exceeds supported size")
    return {
        "order_id": order["order_id"],
        "payload_hash": order_hash(order),
        "event_json": body,
        "accepted_at": int(now * 1000),
        "state": "PENDING",
        "dispatch_bucket": bucket(order["order_id"]),
        "due_at": int(now * 1000),
        "attempts": 0,
    }


class Outbox:
    def __init__(self, store, *, clock=time.time):
        self.store, self.clock = store, clock

    def accept(self, order):
        """Return 202 only AFTER put-if-absent or a consistent duplicate read succeeds."""
        proposed = make_record(order, self.clock())
        existing = self.store.put_if_absent(proposed)
        if existing["payload_hash"] != proposed["payload_hash"]:
            raise Conflict("Order ID already carries a different immutable payload")
        from .reconciliation import immutable

        try:
            immutable(existing)
        except (ValueError, KeyError, TypeError) as error:
            raise RuntimeError("Committed journal cannot be verified") from error
        # Replay returns the ORIGINAL created_at/body, never a newly generated timestamp.
        return existing

    def publish(self, key, queue, *, epoch, lease_seconds=60):
        if type(epoch) is not int or epoch < 1 or not 30 <= lease_seconds <= 300:
            raise ValueError("Explicit fencing epoch and bounded lease required")
        now = int(self.clock() * 1000)
        token = str(uuid.uuid4())
        record = self.store.claim(key, token, epoch, now, now + lease_seconds * 1000)
        if record is None:
            return "not_claimed"
        from .reconciliation import immutable

        immutable(record)
        # Do not mark SENT after an SDK/network failure, including an uncertain send.
        result = queue.send_message(MessageBody=record["event_json"])
        if not result.get("MessageId"):
            raise RuntimeError(
                "Uncertain SQS acknowledgement; retain journal for retry"
            )
        self.store.sent(key, token, epoch, int(self.clock() * 1000))
        return "sent"

    def sweep(self, queue, *, epoch, limit=100, lease_live=lambda: False):
        sent = 0
        for key in self.store.due(int(self.clock() * 1000), limit):
            if not lease_live():
                break
            sent += self.publish(key, queue, epoch=epoch) == "sent"
        return sent
