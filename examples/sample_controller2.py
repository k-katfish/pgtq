from pgtq import PGTQ
from datetime import timedelta

pgtq = PGTQ(dsn="postgresql://demo:demo@localhost:5432/demo", log_fn=print)

pgtq.install()

with pgtq.batch_enqueue():
    for i in range(1000):
        pgtq.enqueue(
            "add_numbers",
            args={"a": i, "b": i * 2},
            expected_duration=timedelta(minutes=2),
        )

        pgtq.enqueue(
            "multiply",
            args={"a": i, "b": i + 3},
            expected_duration=timedelta(minutes=1),
        )


with pgtq.batch_enqueue():
    for i in range(1000, 2000):
        pgtq.enqueue(
            "subtract",
            args={"a": i * 5, "b": i},
            expected_duration=timedelta(minutes=1),
        )
