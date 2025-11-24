#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import time

from pgtq import PGTQ

# Point this at your database
DSN = "postgresql://demo:demo@localhost:5432/demo"


pgtq = PGTQ(dsn=DSN, log_fn=print)


@pgtq.task("add_numbers")
def add(a, b):
    # Simulate some work so wait_for_batch has something to watch
    time.sleep(1)
    result = a + b
    print(f"add_numbers({a}, {b}) = {result}")
    return result


if __name__ == "__main__":
    print("Starting batch worker...")
    pgtq.start_worker()
