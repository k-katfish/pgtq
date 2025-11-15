#!/usr/bin/env python3
import os
import time
from pgtq import PGTQ

DSN = "postgresql://demo:demo@localhost:5432/demo"


def log(msg):
    print(f"[worker {os.getpid()}] {msg}")


pgtq = PGTQ(DSN, log_fn=log)

CRASH_AFTER_N = int(os.environ.get("CRASH_AFTER_N", "-1"))
task_counter = 0


@pgtq.task("slow_add")
def slow_add(a, b):
    global task_counter

    log(f"Running slow_add({a}, {b}) ...")

    # simulate slow work
    time.sleep(3)

    task_counter += 1
    if CRASH_AFTER_N >= 0 and task_counter >= CRASH_AFTER_N:
        log(f"INTENTIONALLY CRASHING after {task_counter} tasks!")
        os._exit(1)

    return a + b


# Start worker loop
pgtq.start_worker()
