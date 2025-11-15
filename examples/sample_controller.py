from pgtq import PGTQ
from datetime import timedelta

pgtq = PGTQ(dsn="postgresql://demo:demo@localhost:5432/demo", log_fn=print)

pgtq.install()

pgtq.enqueue(
    "add_numbers",
    args={"a": 5, "b": 10},
    expected_duration=timedelta(minutes=2)
)

pgtq.enqueue(
    "multiply",
    args={"a": 3, "b": 7},
    expected_duration=timedelta(minutes=1)
)