#!/usr/bin/env python3
import time
from datetime import timedelta
from pgtq import PGTQ

DSN = "postgresql://demo:demo@localhost:5432/demo"


def log(msg):
    print(f"[controller] {msg}")


pgtq = PGTQ(DSN, log_fn=log)
pgtq.install()

# Enqueue a few tasks
with pgtq.batch_enqueue():
    for i in range(10):
        pgtq.enqueue(
            "slow_add",
            args={"a": i, "b": i * 10},
            expected_duration=timedelta(seconds=5),  # short on purpose
            notify=False,
        )

log("Enqueued tasks. Supervisor will monitor for failures.")

# Run supervisor forever (or until you Ctrl+C)
pgtq.run_supervisor_forever(
    interval=4.0,  # check frequently for stale tasks
    default_grace=timedelta(seconds=4),
)
