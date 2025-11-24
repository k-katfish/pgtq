#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import uuid
from datetime import timedelta

from pgtq import PGTQ

# Point this at your database
DSN = "postgresql://demo:demo@localhost:5432/demo"


def main():
    pgtq = PGTQ(dsn=DSN, log_fn=print)
    pgtq.install()

    batch_id = uuid.uuid4()
    print(f"Enqueuing batch {batch_id}")

    with pgtq.batch_enqueue(batch_id=batch_id):
        for i in range(5):
            pgtq.enqueue(
                "add_numbers",
                args={"a": i, "b": i * 2},
                expected_duration=timedelta(seconds=5),
            )

    # Block until the worker has cleared the batch (queued or in-progress)
    pgtq.wait_for_batch(batch_id)
    print(f"Batch {batch_id} finished")


if __name__ == "__main__":
    main()
