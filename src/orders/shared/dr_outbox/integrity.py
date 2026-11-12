"""Lossless logical DynamoDB dataset fingerprints, including stock/checkpoints."""

import base64
import hashlib
import json
from decimal import Decimal


def typed(value):
    if isinstance(value, dict):
        return ["M", [[k, typed(v)] for k, v in sorted(value.items())]]
    if isinstance(value, list):
        return ["L", [typed(v) for v in value]]
    if value is None:
        return ["NULL"]
    if type(value) is bool:
        return ["BOOL", value]
    if isinstance(value, (int, Decimal)):
        number = Decimal(value)
        if not number.is_finite():
            raise ValueError("Invalid DynamoDB number")
        return ["N", format(number.normalize(), "f")]
    if isinstance(value, str):
        return ["S", value]
    if isinstance(value, (bytes, bytearray)):
        return ["B", base64.b64encode(value).decode()]
    if isinstance(value, set):
        return [
            "SET",
            sorted(
                [typed(v) for v in value], key=lambda v: json.dumps(v, sort_keys=True)
            ),
        ]
    if (
        type(value).__module__ == "boto3.dynamodb.types"
        and type(value).__name__ == "Binary"
    ):
        return typed(value.value)
    raise ValueError("Unrecognized/non-exact dataset type")


def fingerprint(rows):
    indexed = {}
    for row in rows:
        key = row.get("order_id")
        if not isinstance(key, str) or key in indexed:
            raise ValueError("Unique order_id schema required")
        indexed[key] = hashlib.sha256(
            json.dumps(typed(row), separators=(",", ":")).encode()
        ).hexdigest()
    digest = hashlib.sha256(
        json.dumps(sorted(indexed.items()), separators=(",", ":")).encode()
    ).hexdigest()
    return indexed, digest


def compare_datasets(expected, recovered):
    source, source_hash = fingerprint(expected)
    target, target_hash = fingerprint(recovered)
    missing = sorted(source.keys() - target.keys())
    extra = sorted(target.keys() - source.keys())
    changed = sorted(k for k in source.keys() & target.keys() if source[k] != target[k])
    return {
        "matches": not (missing or extra or changed),
        "source_digest": source_hash,
        "target_digest": target_hash,
        "source_count": len(source),
        "target_count": len(target),
        "missing_keys": missing,
        "extra_keys": extra,
        "changed_keys": changed,
    }
