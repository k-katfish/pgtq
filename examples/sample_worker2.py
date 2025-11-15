#!/usr/bin/env python3
# -*- coding: utf-8 -*-

# Import PGTQ (you need to install pgtq via `pip install pgtq` first)
from pgtq import PGTQ

# Connect to the database
pgtq = PGTQ(dsn="postgresql://demo:demo@localhost:5432/demo", log_fn=print)


# Define tasks
@pgtq.task("subtract")
def subtract(a, b):
    print("running subtract:", a, b)
    return a - b


# Another way to start the worker loop if you want more control
for task in pgtq.listen():
    pgtq.run_task(task)
