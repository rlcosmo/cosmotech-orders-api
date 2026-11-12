"""Opt-in primary dispatcher; staged integration only, no default activation."""

import json
import os
import time
from pathlib import Path


def live_epoch(path):
    try:
        v = json.loads(Path(path).read_text())
        now = time.time()
        if (
            v["mode"] == "run"
            and v["region"] == "us-east-1"
            and type(v["epoch"]) is int
            and v["epoch"] > 0
            and now + 15 < v["expires_at"] <= now + 300
        ):
            return v["epoch"]
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return None


def main():
    if os.getenv("ORDERS_DURABLE_OUTBOX") != "true":
        raise ValueError("Explicit outbox opt-in required")
    if (
        os.getenv("ORDERS_QUEUE_URL")
        != "https://sqs.us-east-1.amazonaws.com/414053644145/aws-eks-istio-lab-dev-orders-eks"
    ):
        raise ValueError("Reviewed primary queue required")
    import boto3
    from botocore.config import Config

    from orders.producer.durable import get_outbox

    outbox = get_outbox()
    sqs = boto3.client(
        "sqs",
        region_name="us-east-1",
        config=Config(
            connect_timeout=2,
            read_timeout=3,
            retries={"mode": "standard", "total_max_attempts": 2},
        ),
    )

    class Queue:
        def send_message(self, **args):
            return sqs.send_message(QueueUrl=os.environ["ORDERS_QUEUE_URL"], **args)

    control = os.environ["ORDERS_OUTBOX_ACTIVATION_FILE"]
    while True:
        epoch = live_epoch(control)
        if epoch:
            try:
                outbox.sweep(
                    Queue(),
                    epoch=epoch,
                    lease_live=lambda epoch=epoch: live_epoch(control) == epoch,
                )
            except Exception as error:  # noqa: BLE001 - Persisted records remain pending for bounded retries.
                print(
                    json.dumps(
                        {"event": "outbox_retry", "error_type": type(error).__name__}
                    ),
                    flush=True,
                )
        time.sleep(2)


if __name__ == "__main__":
    main()
