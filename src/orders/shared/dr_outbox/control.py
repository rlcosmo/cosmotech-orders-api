"""Fail-closed offline recovery gate. No AWS or Kubernetes mutation is performed."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime

ACCOUNT = "414053644145"
PRIMARY = "us-east-1"
SECONDARY = "us-east-2"
REQUIRED = {
    "fenced",
    "snapshot_verified",
    "queue_reconciled",
    "images_verified",
    "secrets_verified",
    "storage_verified",
    "dependencies_verified",
}


def canonical_hash(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def promotion_plan(evidence: dict, now: datetime | None = None) -> dict:
    """Operator evidence must be independently verified; booleans are not a fence.

    This is a planning gate, NOT a globally consistent lease or production controller.
    A regional table/conditional write cannot fence another partitioned region.
    """
    now = now or datetime.now(UTC)
    preparation_gate(evidence, now)
    if evidence.get("inflight_operations", -1) != 0 or evidence.get("writers") != 0:
        raise ValueError("Concurrent writer/inflight operation detected")
    checks = evidence.get("checks", {})
    if set(checks) != REQUIRED or any(v is not True for v in checks.values()):
        raise ValueError("Recovery checks incomplete; no promotion")
    if not evidence.get("fence_reference") or not evidence.get("snapshot_version"):
        raise ValueError(
            "Require an independently verifiable fence and immutable backup"
        )
    if (
        not evidence.get("change_reference")
        or type(evidence.get("epoch")) is not int
        or evidence["epoch"] < 1
    ):
        raise ValueError("Require a change record and increasing fencing epoch")
    if not re.fullmatch(r"[0-9a-f]{64}", evidence.get("protected_primary_hash", "")):
        raise ValueError("Missing protected baseline")
    return {
        "mode": "plan-only",
        "target_region": SECONDARY,
        "epoch": evidence["epoch"],
        "evidence_hash": canonical_hash(evidence),
        "dns_mutation": False,
        "steps": [
            "verify-destination-backup",
            "restore-orders-table",
            "restore-volumes",
            "verify-regional-secrets-and-image-digests",
            "replay-verified-journal",
            "render-passive-gitops",
            "isolated-e2e",
            "compare-protected-primary-hash",
        ],
        "activation_requires": "independent-fence + reviewed-GitOps-commit + new-e2e-evidence",
    }


def preparation_gate(evidence: dict, now: datetime | None = None) -> None:
    now = now or datetime.now(UTC)
    if evidence.get("account") != ACCOUNT or evidence.get("target_region") != SECONDARY:
        raise ValueError("Only the isolated Ohio standby can be targeted")
    if (
        evidence.get("source_region") != PRIMARY
        or evidence.get("environment") != "dr-test"
    ):
        raise ValueError("Primary mutations and production failover are forbidden")
    expires = datetime.fromisoformat(evidence["expires_at"])
    observed = datetime.fromisoformat(evidence["observed_at"])
    if (
        now.tzinfo is None
        or expires.tzinfo is None
        or observed.tzinfo is None
        or not observed <= now < expires
        or (expires - observed).total_seconds() > 900
    ):
        raise ValueError("Expired, future or unbounded evidence")
    if evidence.get("account_released") is not True or not evidence.get(
        "aws_release_reference"
    ):
        raise ValueError("AWS account release is not verified")


def replay_events(events: list[dict], completed_ids: set[str]) -> list[dict]:
    """Build a replay list, without sending/receiving/deleting any SQS messages.

    Only an audited durable journal is acceptable. SQS metadata cannot reconstruct
    lost message bodies. Conflicting IDs abort; duplicate equivalent events collapse.
    """
    found = {}
    for event in events:
        order_id = event.get("order_id")
        if (
            not isinstance(order_id, str)
            or not order_id
            or len(order_id) > 128
            or order_id.startswith(("INVENTORY#", "IDEMPOTENCY#"))
            or event.get("event_type") != "order.created"
            or not isinstance(event.get("order"), dict)
            or event["order"].get("order_id") != order_id
            or type(event.get("version")) is not int
            or event["version"] != 2
            or not event.get("created_at")
        ):
            raise ValueError("Invalid durable journal event")
        stamp = datetime.fromisoformat(event["created_at"])
        items = event["order"].get("items")
        if (
            stamp.tzinfo is None
            or not isinstance(items, list)
            or not 1 <= len(items) <= 100
            or any(
                not isinstance(item, dict)
                or not isinstance(item.get("sku"), str)
                or not 1 <= len(item["sku"].strip()) <= 128
                or type(item.get("quantity")) is not int
                or not 1 <= item["quantity"] <= 10000
                for item in items
            )
        ):
            raise ValueError("Invalid journal timestamp/items")
        if order_id in found and canonical_hash(found[order_id]) != canonical_hash(
            event
        ):
            raise ValueError("Conflicting immutable order ID")
        found[order_id] = event
    return [found[key] for key in sorted(found) if key not in completed_ids]


def guard_chaos_target(target: dict) -> None:
    if (
        target.get("account") != ACCOUNT
        or target.get("region") != SECONDARY
        or target.get("cluster") != "cosmotech-dr"
        or target.get("namespace") != "dr-test"
        or target.get("environment") != "dr-test"
        or target.get("synthetic_only") is not True
        or type(target.get("duration_seconds")) is not int
        or target["duration_seconds"] not in range(1, 121)
        or target.get("kind")
        not in {"pod-loss", "node-loss", "az-loss", "dependency-outage"}
        or target.get("telemetry_ready") is not True
    ):
        raise ValueError(
            "Chaos is restricted to synthetic isolated DR targets for <=120s"
        )
