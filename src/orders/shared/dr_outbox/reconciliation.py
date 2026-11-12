"""Integrity-checked, offline journal recovery plans. No queue or business mutation."""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal

from .control import replay_events
from .outbox import bucket, canonical, order_hash


def integer(value):
    if (
        type(value) is int
        or isinstance(value, Decimal)
        and value == value.to_integral_value()
    ):
        return int(value)
    raise ValueError("Expected exact integer")


def immutable(record):
    event = json.loads(record["event_json"])
    replay_events([event], set())
    if (
        record["order_id"] != event["order_id"]
        or order_hash(event["order"]) != record["payload_hash"]
    ):
        raise ValueError("Journal body/hash/key mismatch")
    return {
        "order_id": record["order_id"],
        "payload_hash": record["payload_hash"],
        "event_json": canonical(event),
        "accepted_at": integer(record["accepted_at"]),
    }


def snapshot(records, *, snapshot_id, region, fenced):
    if (
        fenced is not True
        or not snapshot_id
        or region not in {"us-east-1", "us-east-2"}
    ):
        raise ValueError(
            "Export requires verified quiescence/fence and an immutable snapshot reference"
        )
    rows = [immutable(r) for r in records]
    rows.sort(key=lambda r: r["order_id"])
    if len({r["order_id"] for r in rows}) != len(rows):
        raise ValueError("Duplicate journal key in snapshot")
    data = {"schema": 1, "snapshot_id": snapshot_id, "region": region, "records": rows}
    return {**data, "sha256": hashlib.sha256(canonical(data).encode()).hexdigest()}


def verify_snapshot(document):
    data = {k: v for k, v in document.items() if k != "sha256"}
    if document.get("schema") != 1 or hashlib.sha256(
        canonical(data).encode()
    ).hexdigest() != document.get("sha256"):
        raise ValueError("Snapshot integrity check failed")
    validated = snapshot(
        document["records"],
        snapshot_id=document["snapshot_id"],
        region=document["region"],
        fenced=True,
    )
    if validated != document:
        raise ValueError("Noncanonical or malformed snapshot")
    return {r["order_id"]: r for r in document["records"]}


def reconcile(
    source,
    target,
    business_records,
    receipts,
    *,
    receipt_inventory_complete=False,
    expected_business_records=None,
):
    """Require trusted independent receipts to claim coverage; hash is not authenticity.

    Restore business table + inventory/idempotency checkpoints consistently before
    replay. This compares logical orders, not all stock/payment side effects.
    """
    dataset_consistent = None
    dataset_digest = None
    if expected_business_records is not None:
        from .integrity import compare_datasets

        comparison = compare_datasets(expected_business_records, business_records)
        dataset_consistent = comparison["matches"]
        dataset_digest = comparison["source_digest"]
        if not dataset_consistent:
            raise ValueError(
                "Business/inventory/checkpoint dataset divergence; replay blocked"
            )
    expected, recovered = verify_snapshot(source), verify_snapshot(target)
    divergent = [
        key
        for key in expected.keys() & recovered.keys()
        if expected[key] != recovered[key]
    ]
    if divergent:
        raise ValueError("Cross-region immutable journal divergence; promotion blocked")
    receipt_map = {}
    for receipt in receipts:
        key = receipt["order_id"]
        if key in receipt_map or not isinstance(receipt.get("payload_hash"), str):
            raise ValueError("Duplicate/invalid independent receipt")
        receipt_map[key] = receipt["payload_hash"]
        if key in expected and expected[key]["payload_hash"] != receipt["payload_hash"]:
            raise ValueError("Receipt and source journal disagree")
    unknown_acks = sorted(receipt_map.keys() - expected.keys())
    business = {}
    for row in business_records:
        key = row["order_id"]
        if key.startswith(("INVENTORY#", "IDEMPOTENCY#")):
            continue
        if key in business or key not in expected:
            raise ValueError(
                "Unexplained business record or duplicate; reconcile datasets first"
            )
        items = [
            {
                "sku": v.get("sku", v.get("product_id")),
                "quantity": integer(v["quantity"]),
            }
            for v in row["items"]
        ]
        if (
            order_hash({"order_id": key, "items": items})
            != expected[key]["payload_hash"]
        ):
            raise ValueError("Recovered business record has conflicting items")
        business[key] = row["status"]
    completed = sorted(
        key for key, status in business.items() if status in {"COMPLETED", "REJECTED"}
    )
    pending = sorted(expected.keys() - set(completed))
    missing = sorted(expected.keys() - recovered.keys())
    # Same restored journal does NOT prove that no acknowledged writes were lost.
    coverage = (
        receipt_inventory_complete is True
        and set(receipt_map) == set(expected)
        and not unknown_acks
    )
    return {
        "schema": 1,
        "source_snapshot": source["snapshot_id"],
        "target_snapshot": target["snapshot_id"],
        "source_digest": source["sha256"],
        "target_digest": target["sha256"],
        "accepted_count": len(expected),
        "recovered_journal_count": len(recovered),
        "completed_count": len(completed),
        "missing_journal_ids": missing,
        "pending_ids": pending,
        "unknown_ack_ids": unknown_acks,
        "extra_target_ids": sorted(recovered.keys() - expected.keys()),
        "receipt_coverage_verified": coverage,
        "message_loss_count": 0 if coverage and not missing else None,
        "unrecovered_ack_count": len(receipt_map.keys() - recovered.keys()),
        "business_dataset_consistent": dataset_consistent,
        "business_dataset_digest": dataset_digest,
        "safe_to_promote": False,  # independent fence + whole-business integrity + E2E still required
        "replay_requires": [
            "verified-fence",
            "consistent-business-table-inventory-checkpoints",
            "restore-missing-journal",
            "new-epoch",
        ],
    }


def replay_records(source, report, *, epoch, now_ms):
    if type(epoch) is not int or epoch < 1 or type(now_ms) is not int:
        raise ValueError("Explicit target epoch/time required")
    rows = verify_snapshot(source)
    if (
        report["source_digest"] != source["sha256"]
        or report["unknown_ack_ids"]
        or report["extra_target_ids"]
    ):
        raise ValueError("Unreconciled snapshot/acknowledgements; no replay")
    return [
        {
            **rows[key],
            "state": "PENDING",
            "epoch": epoch,
            "due_at": now_ms,
            "dispatch_bucket": bucket(key),
            "attempts": 0,
        }
        for key in report["pending_ids"]
    ]
