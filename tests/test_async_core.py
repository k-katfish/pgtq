from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest

from pgtq.async_core import AsyncPGTQ
from pgtq.task import Task


def run(coro):
    return asyncio.run(coro)


class FakeAsyncCursor:
    def __init__(self, connection, row_factory=None):
        self.connection = connection
        self.row_factory = row_factory
        self._result = {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def execute(self, query, params=None):
        self.connection.executed.append((query, params))
        if self.connection.results:
            self._result = self.connection.results.pop(0)
        else:
            self._result = {}

    async def fetchone(self):
        return self._result.get("fetchone")

    async def fetchall(self):
        return self._result.get("fetchall", [])


class FakeNotifyIterator:
    def __init__(self, connection):
        self.connection = connection

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self.connection.notify_queue:
            payload = self.connection.notify_queue.pop(0)
            return SimpleNamespace(payload=payload)
        raise StopAsyncIteration


class FakeAsyncConnection:
    def __init__(self, *, name="conn"):
        self.name = name
        self.executed = []
        self.results = []
        self.notify_queue = []
        self.closed = False

    def cursor(self, *args, **kwargs):
        return FakeAsyncCursor(self, kwargs.get("row_factory"))

    def queue_result(self, *, fetchone=None, fetchall=None):
        self.results.append(
            {
                "fetchone": fetchone,
                "fetchall": [] if fetchall is None else fetchall,
            }
        )

    def queue_notification(self, payload):
        self.notify_queue.append(payload)

    def notifies(self):
        return FakeNotifyIterator(self)

    async def close(self):
        self.closed = True


@pytest.fixture
def fake_async_psycopg(monkeypatch):
    connections = []
    calls = []

    async def fake_connect(dsn, autocommit=True):
        conn = FakeAsyncConnection(name=f"conn{len(connections)}")
        connections.append(conn)
        calls.append((dsn, autocommit))
        return conn

    monkeypatch.setattr(
        "pgtq.async_core.psycopg.AsyncConnection.connect",
        fake_connect,
    )
    return SimpleNamespace(connections=connections, calls=calls)


@pytest.fixture
def async_queue(fake_async_psycopg):
    logs: list[str] = []
    queue = run(AsyncPGTQ.create(dsn="postgresql://fake", log_fn=logs.append))
    assert len(fake_async_psycopg.connections) == 2
    yield queue, fake_async_psycopg.connections[0], fake_async_psycopg.connections[1], logs, fake_async_psycopg
    run(queue.close())


def make_task(**overrides):
    data = {
        "id": 1,
        "call": "default",
        "args": {},
        "priority": 0,
        "status": "queued",
        "inserted_at": datetime.now(timezone.utc),
        "started_at": None,
        "last_heartbeat": None,
        "expected_duration": None,
        "batch_id": None,
    }
    data.update(overrides)
    return Task(**data)


def test_create_uses_async_connections(fake_async_psycopg):
    queue = run(AsyncPGTQ.create(dsn="postgresql://fake"))
    try:
        assert fake_async_psycopg.calls == [
            ("postgresql://fake", True),
            ("postgresql://fake", True),
        ]
    finally:
        run(queue.close())


def test_close_logs(async_queue):
    queue, main_conn, listen_conn, logs, _ = async_queue
    run(queue.close())
    assert main_conn.closed and listen_conn.closed
    assert logs[-1] == "[pgtq-async] connections closed"


def test_install_executes_schema(async_queue):
    queue, main_conn, _, logs, _ = async_queue
    run(queue.install())
    assert len(main_conn.executed) == 5
    assert "[pgtq-async] install complete" in logs


def test_task_registration_and_duplicates(async_queue):
    queue, _, _, logs, _ = async_queue

    @queue.task("job")
    async def handler():
        return None

    assert queue.registered_task_names == ["job"]
    assert logs[-1].startswith("[pgtq-async] registered")

    with pytest.raises(ValueError):
        @queue.task("job")
        async def other():
            return None


def test_enqueue_inserts_and_notifies(async_queue):
    queue, main_conn, _, logs, _ = async_queue
    main_conn.queue_result(fetchone=(99,))
    run(
        queue.enqueue(
            "job",
            args={"x": 1},
            priority=5,
            expected_duration=timedelta(seconds=2),
        )
    )
    assert json.loads(main_conn.executed[0][1][1]) == {"x": 1}
    assert main_conn.executed[0][1][-1] is None
    assert "NOTIFY" in main_conn.executed[1][0]
    assert any("enqueued task 99" in msg for msg in logs)


def test_batch_enqueue_defers_notify(async_queue, monkeypatch):
    queue, main_conn, _, logs, _ = async_queue
    main_conn.queue_result(fetchone=(1,))
    batch_id = uuid4()

    notifications = []

    async def fake_notify():
        notifications.append("sent")

    monkeypatch.setattr(queue, "notify", fake_notify)

    async def do_work():
        async with queue.batch_enqueue(batch_id=batch_id):
            await queue.enqueue("job")

    run(do_work())
    assert notifications == ["sent"]
    assert logs[-1] == "[pgtq-async] batch_enqueue complete, sent NOTIFY"
    assert main_conn.executed[0][1][-1] == batch_id


def test_batch_enqueue_rejects_conflicts(async_queue):
    queue, _, _, _logs, _ = async_queue

    async def do_work():
        async with queue.batch_enqueue(batch_id=uuid4()):
            with pytest.raises(ValueError):
                async with queue.batch_enqueue(batch_id=uuid4()):
                    await queue.enqueue("job")

    run(do_work())


def test_batch_enqueue_is_task_local(async_queue, monkeypatch):
    queue, main_conn, _, _logs, _ = async_queue
    batch_value = uuid4()

    notify_calls = []
    manual_notifies = []

    original_notify_new = queue._notify_new_tasks

    async def wrapped_notify_new(cur):
        notify_calls.append("notify")
        return await original_notify_new(cur)

    monkeypatch.setattr(queue, "_notify_new_tasks", wrapped_notify_new)

    async def wrapped_notify():
        manual_notifies.append("manual")
        # Do not call through to avoid triggering wrapped _notify_new_tasks

    monkeypatch.setattr(queue, "notify", wrapped_notify)

    started = asyncio.Event()
    release = asyncio.Event()

    async def run_batch_enqueue():
        async with queue.batch_enqueue(batch_id=batch_value):
            started.set()
            await asyncio.wait_for(release.wait(), timeout=2)
            main_conn.queue_result(fetchone=(2,))
            await queue.enqueue("batched-task")

    async def run_regular_enqueue():
        await asyncio.wait_for(started.wait(), timeout=2)
        main_conn.queue_result(fetchone=(1,))
        await queue.enqueue("regular-task")
        release.set()

    async def run_concurrent():
        await asyncio.gather(run_batch_enqueue(), run_regular_enqueue())

    run(run_concurrent())

    insert_params = [
        params for query, params in main_conn.executed if "INSERT INTO" in str(query)
    ]

    assert notify_calls == ["notify"]
    assert manual_notifies == ["manual"]
    assert insert_params[0][-1] is None
    assert insert_params[1][-1] == batch_value


def test_notify_sends_manual_signal(async_queue):
    queue, main_conn, _, logs, _ = async_queue
    run(queue.notify())
    assert "NOTIFY" in main_conn.executed[-1][0]
    assert logs[-1].startswith("[pgtq-async] manual NOTIFY")


def test_dequeue_one_returns_task(async_queue):
    queue, main_conn, _, _logs, _ = async_queue
    now = datetime.now(timezone.utc)
    row = {
        "id": 7,
        "call": "job",
        "args": '{"value": 3}',
        "priority": 0,
        "status": "queued",
        "inserted_at": now,
        "started_at": now,
        "last_heartbeat": now,
        "expected_duration": timedelta(seconds=5),
        "batch_id": uuid4(),
    }
    main_conn.queue_result(fetchone=row)

    task = run(queue.dequeue_one(["job"]))
    assert task.id == 7
    assert task.args == {"value": 3}
    assert task.batch_id == row["batch_id"]


def test_dequeue_one_returns_none(async_queue):
    queue, _, _, _, _ = async_queue
    task = run(queue.dequeue_one())
    assert task is None


def test_dequeue_one_orders_by_desc_priority_by_default(async_queue):
    queue, main_conn, _, _, _ = async_queue
    main_conn.queue_result(fetchone=None)

    run(queue.dequeue_one())

    executed_sql = main_conn.executed[0][0]
    assert "ORDER BY priority DESC, id ASC" in executed_sql


def test_dequeue_one_can_invert_priority(async_queue):
    queue, main_conn, _, _, _ = async_queue
    main_conn.queue_result(fetchone=None)

    run(queue.dequeue_one(invert_priority=True))

    executed_sql = main_conn.executed[0][0]
    assert "ORDER BY priority ASC, id ASC" in executed_sql


def test_listen_yields_after_notify(async_queue, monkeypatch):
    queue, _, listen_conn, logs, _ = async_queue
    listen_conn.queue_notification("wake")
    tasks = [
        None,
        make_task(id=11, call="job"),
        make_task(id=12, call="job"),
    ]

    async def fake_dequeue(*, acceptable_tasks=None, invert_priority=False):
        return tasks.pop(0)

    async def fake_wait_for(awaitable, timeout):
        return await awaitable

    monkeypatch.setattr(queue, "dequeue_one", fake_dequeue)
    monkeypatch.setattr("pgtq.async_core.asyncio.wait_for", fake_wait_for)

    async def consume():
        agen = queue.listen()
        first = await agen.__anext__()
        second = await agen.__anext__()
        await agen.aclose()
        return first, second

    task_one, task_two = run(consume())
    assert [task_one.id, task_two.id] == [11, 12]
    assert logs[-1].startswith("[pgtq-async] got NOTIFY")


def test_listen_handles_timeout(async_queue, monkeypatch):
    queue, _, listen_conn, logs, _ = async_queue
    tasks = [None, make_task(id=12, call="job")]

    async def fake_dequeue(*, acceptable_tasks=None, invert_priority=False):
        return tasks.pop(0)

    calls = {"timeout": 0}

    async def fake_wait_for(awaitable, timeout):
        if calls["timeout"] == 0:
            calls["timeout"] += 1
            awaitable.close()
            raise asyncio.TimeoutError
        listen_conn.queue_notification("later")
        return await awaitable

    monkeypatch.setattr(queue, "dequeue_one", fake_dequeue)
    monkeypatch.setattr("pgtq.async_core.asyncio.wait_for", fake_wait_for)

    async def consume():
        agen = queue.listen()
        task = await agen.__anext__()
        await agen.aclose()
        return task

    task = run(consume())
    assert task.id == 12
    assert calls["timeout"] == 1
    assert any("listening on channel" in msg for msg in logs)


def test_wait_for_batch_until_done(async_queue, monkeypatch):
    queue, main_conn, _, logs, _ = async_queue
    main_conn.queue_result(fetchone=(2,))
    main_conn.queue_result(fetchone=(0,))

    sleeps: list[float] = []

    async def fake_sleep(duration):
        sleeps.append(duration)

    monkeypatch.setattr("pgtq.async_core.asyncio.sleep", fake_sleep)

    run(queue.wait_for_batch("batch-async", poll_interval=0.25))

    assert main_conn.executed[0][1] == ("batch-async",)
    assert main_conn.executed[1][1] == ("batch-async",)
    assert sleeps == [0.25]
    assert any("batch batch-async has no pending tasks" in msg for msg in logs)


def test_wait_for_batch_timeout(async_queue, monkeypatch):
    queue, main_conn, _, _logs, _ = async_queue
    main_conn.queue_result(fetchone=(1,))
    main_conn.queue_result(fetchone=(1,))
    main_conn.queue_result(fetchone=(1,))

    timeline = {"now": 0.0}

    async def fake_sleep(duration):
        timeline["now"] += duration

    def fake_monotonic():
        return timeline["now"]

    monkeypatch.setattr("pgtq.async_core.time.monotonic", fake_monotonic)
    monkeypatch.setattr("pgtq.async_core.asyncio.sleep", fake_sleep)

    with pytest.raises(TimeoutError):
        run(queue.wait_for_batch("batch-timeout", poll_interval=0.1, timeout=0.2))


def test_heartbeat_updates(async_queue):
    queue, main_conn, _, logs, _ = async_queue
    run(queue.heartbeat(10))
    assert "SET last_heartbeat" in main_conn.executed[-1][0]
    assert logs[-1] == "[pgtq-async] heartbeat for task 10"


def test_complete_delete_and_done(async_queue):
    queue, main_conn, _, logs, _ = async_queue
    run(queue.complete(5))
    assert "DELETE FROM" in main_conn.executed[-1][0]
    run(queue.complete(6, delete=False))
    assert "SET status = 'done'" in main_conn.executed[-1][0]
    assert "[pgtq-async] completed task 6 (status=done)" in logs


def test_fail_requeues_and_permanent(async_queue):
    queue, main_conn, _, logs, _ = async_queue
    main_conn.queue_result(fetchone=(0,))
    run(queue.fail(22, requeue=True, error="boom"))
    assert "retry_count" in main_conn.executed[1][0]
    assert "[pgtq-async] task 22 failed, requeued" in logs[-1]

    main_conn.queue_result(fetchone=(10,))
    run(queue.fail(23, requeue=True, error="boom", max_retries=5))
    assert "[pgtq-async] task 23 failed permanently" in logs[-1]


def test_fail_returns_when_task_missing(async_queue):
    queue, main_conn, _, _, _ = async_queue
    main_conn.queue_result(fetchone=None)
    run(queue.fail(50, requeue=True))
    assert len(main_conn.executed) == 1


def test_requeue_stale_in_progress(async_queue):
    queue, main_conn, _, logs, _ = async_queue
    main_conn.queue_result(fetchall=[(1,), (2,)])
    count = run(queue.requeue_stale_in_progress(default_grace=timedelta(seconds=1)))
    assert count == 2
    assert "[pgtq-async] requeued 2 stale in-progress tasks" in logs[-1]


def test_run_supervisor_logs_and_errors(async_queue, monkeypatch):
    queue, _, _, logs, _ = async_queue

    async def fake_requeue(*, default_grace):
        return 3

    async def fake_sleep(_interval):
        raise RuntimeError("stop")

    monkeypatch.setattr(queue, "requeue_stale_in_progress", fake_requeue)
    monkeypatch.setattr("pgtq.async_core.asyncio.sleep", fake_sleep)

    with pytest.raises(RuntimeError):
        run(queue.run_supervisor_forever(interval=0.01))

    assert any("supervisor requeued 3" in msg for msg in logs)

    async def failing_requeue(*, default_grace):
        raise ValueError("boom")

    monkeypatch.setattr(queue, "requeue_stale_in_progress", failing_requeue)
    monkeypatch.setattr("pgtq.async_core.asyncio.sleep", fake_sleep)

    with pytest.raises(RuntimeError):
        run(queue.run_supervisor_forever(interval=0.01))

    assert any("supervisor error" in msg for msg in logs)


def test_start_worker_processes_tasks(async_queue, monkeypatch):
    queue, _, _, logs, _ = async_queue
    tasks = [make_task(id=70, call="job")]

    @queue.task("job")
    async def handler():
        return None

    async def fake_listen(*, acceptable_tasks, idle_poll_interval, invert_priority):
        for task in tasks:
            assert invert_priority is False
            yield task

    processed = []

    async def fake_run_task(task):
        processed.append(task.id)

    queue.run_task = fake_run_task
    monkeypatch.setattr(queue, "listen", fake_listen)
    run(queue.start_worker())
    assert processed == [70]
    assert any(msg.startswith("[pgtq-async] worker starting") for msg in logs)


def test_run_task_handles_async_handler(async_queue):
    queue, _, _, logs, _ = async_queue
    completions = []

    async def fake_complete(task_id, delete=True):
        completions.append((task_id, delete))

    queue.complete = fake_complete

    @queue.task("async_job")
    async def handler(value):
        await asyncio.sleep(0)
        return value * 2

    task = make_task(id=80, call="async_job", args={"value": 3})
    result = run(queue.run_task(task))
    assert result == 6
    assert completions == [(80, True)]
    assert logs[-1].endswith("completed")


def test_run_task_failure(async_queue):
    queue, _, _, _logs, _ = async_queue
    failures = []

    async def fake_fail(task_id, *, requeue=False, error=None, max_retries=None):
        failures.append((task_id, requeue, error))

    queue.fail = fake_fail

    @queue.task("explode")
    async def handler():
        raise RuntimeError("boom")

    task = make_task(id=81, call="explode")
    result = run(queue.run_task(task))
    assert result is None
    assert failures == [(81, True, "boom")]


def test_run_task_unregistered(async_queue):
    queue, _, _, _logs, _ = async_queue
    failures = []

    async def fake_fail(task_id, *, requeue=False, error=None, max_retries=None):
        failures.append((task_id, requeue, error))

    queue.fail = fake_fail

    task = make_task(id=82, call="missing")
    result = run(queue.run_task(task))
    assert result is None
    assert failures == [(82, False, "unregistered task")]


def test_parse_interval_variants():
    assert AsyncPGTQ._parse_interval(None) is None
    delta = timedelta(seconds=4)
    assert AsyncPGTQ._parse_interval(delta) is delta
    assert AsyncPGTQ._parse_interval("bad") is None


def test_parse_json_variants():
    assert AsyncPGTQ._parse_json(None) == {}
    payload = {"a": 1}
    assert AsyncPGTQ._parse_json(payload) is payload
    assert AsyncPGTQ._parse_json("not-json") == {}
