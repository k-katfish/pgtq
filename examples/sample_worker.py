#!/usr/bin/env python3
# -*- coding: utf-8 -*-

# Import PGTQ (you need to install pgtq via `pip install pgtq` first)
from pgtq import PGTQ

# Connect to the database
pgtq = PGTQ(dsn="postgresql://demo:demo@localhost:5432/demo", log_fn=print)

# Define tasks
@pgtq.task("add_numbers")
def add(a, b):
    print("running add:", a, b)
    return a + b

@pgtq.task("multiply")
def mul(a, b):
    print("running mul:", a, b)
    return a * b

# Start worker loop
pgtq.start_worker()