"""Primary integration factory. Inactive unless explicitly enabled after rollout."""

import os
from functools import lru_cache


@lru_cache(maxsize=1)
def get_outbox():
    import boto3
    from botocore.config import Config

    from orders.shared.dr_outbox.dynamo_outbox import DynamoOutbox
    from orders.shared.dr_outbox.outbox import Outbox

    region = os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION")
    table = os.getenv("ORDERS_OUTBOX_TABLE")
    if region != "us-east-1" or table != "cosmotech-orders-outbox":
        raise ValueError("Explicit reviewed primary journal configuration required")
    session = boto3.Session(region_name=region)
    config = Config(
        connect_timeout=2,
        read_timeout=3,
        retries={"mode": "standard", "total_max_attempts": 2},
    )
    identity = session.client("sts", config=config).get_caller_identity()
    if identity["Account"] != "414053644145" or ":assumed-role/" not in identity["Arn"]:
        raise ValueError("Temporary authorized application role required")
    return Outbox(
        DynamoOutbox(session.resource("dynamodb", config=config).Table(table))
    )
