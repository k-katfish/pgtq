# PostGresql Task Queue (PGTQ)

A very simple task queue system built on PostgreSQL. PGTQ was designed to solve
the following use case:

1. I have one or more 'controllers' which will enqueue many tasks to be run in a
   PostgreSQL database.
2. I have one or more 'workers' which will dequeue tasks from the database and
   execute them.

## Installation

You can install PGTQ via pip:

```bash
pip install pgtq
```

## Usage

Here's a simple example of how to use PGTQ:

```python
# controller.py
from pgtq import PGTQ

# Initialize the task queue
pgtq = PGTQ(dsn="postgresql://user:password@localhost/dbname")

pgtq.install() # Create necessary tables (only needed once, but it's safe to call multiple times)

# Enqueue a task
pgtq.enqueue("add_numbers", args={"a": i, "b": i * 2})
```

```python
# worker.py
from pgtq import PGTQ

# Initialize the task queue
pgtq = PGTQ(dsn="postgresql://user:password@localhost/dbname")

@pgtq.task("add_numbers")
def add_numbers(a, b):
    print("Adding:", a, "+", b, "=", a + b)
    return a + b

pgtq.start_worker()
```

For more detailed examples, see the `examples/` directory in the repository.

## Batching and waiting for completion

You can group tasks together with a `batch_id` (UUID) and wait for them to
finish before continuing:

```python
import uuid
from pgtq import PGTQ

pgtq = PGTQ(dsn="postgresql://user:password@localhost/dbname")
pgtq.install()

batch_id = uuid.uuid4()

with pgtq.batch_enqueue(batch_id=batch_id):
    for i in range(10):
        pgtq.enqueue("add_numbers", args={"a": i, "b": i * 2})

# blocks until no queued/in-progress tasks remain for this batch
pgtq.wait_for_batch(batch_id)
```

Async users can call `AsyncPGTQ.wait_for_batch(batch_id)` in the same way.

## License

This project is licensed under the MIT License. See the [LICENSE](LICENSE) file
for details.
