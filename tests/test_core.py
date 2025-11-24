from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest

from pgtq.core import PGTQ
from pgtq.task import Task


class FakeCursor:
    def __init__(self, connection, row_factory=None):
        self.connection = connection
        self.row_factory = row_factory
        self._result = {}

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def execute(self, query, params=None):
        self.connection.executed.append((query, params))
        if self.connection.results:
            self._result = self.connection.results.pop(0)
        else:
            self._result = {}

    def fetchone(self):
        return self._result.get("fetchone")

    def fetchall(self):
        return self._result.get("fetchall", [])


class FakeConnection:
    def __init__(self):
        self.autocommit = False
        self.executed = []
        self.results = []
        self.pgconn = SimpleNamespace(consume_input=lambda: None)
        self.notify_queue = []

    def cursor(self, *args, **kwargs):
        return FakeCursor(self, kwargs.get("row_factory"))

    def queue_result(self, *, fetchone=None, fetchall=None):
        self.results.append(
            {
                "fetchone": fetchone,
                "fetchall": [] if fetchall is None else fetchall,
            }
        )

    def notifies(self):
        if self.notify_queue:
            return self.notify_queue.pop(0)
        return None


@pytest.fixture
def fake_psycopg(monkeypatch):
    connections = []

    def fake_connect(dsn):
        conn = FakeConnection()
        connections.append(conn)
        return conn

    monkeypatch.setattr("pgtq.core.psycopg.connect", fake_connect)
    return connections


@pytest.fixture
def pgtq_env(fake_psycopg):
    logs = []
    queue = PGTQ(dsn="postgresql://fake", log_fn=logs.append)
    assert len(fake_psycopg) == 2  # connection + listen connection
    return queue, fake_psycopg[0], fake_psycopg[1], logs


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


def test_default_logger_is_noop(fake_psycopg):
    queue = PGTQ(dsn="postgresql://fake")
    queue.log("hello world")  # should not raise
    assert len(fake_psycopg) == 2


def test_install_executes_schema_statements(pgtq_env):
    queue, conn, _, _ = pgtq_env

    queue.install()

    assert len(conn.executed) == 5
    assert "CREATE TABLE" in str(conn.executed[0][0])
    assert "CREATE INDEX" in str(conn.executed[1][0])
    assert "CREATE INDEX" in str(conn.executed[2][0])
    assert "ALTER TABLE" in str(conn.executed[3][0])
    assert "batch_id" in str(conn.executed[3][0]).lower()


def test_enqueue_inserts_row_and_notifies(pgtq_env):
    queue, conn, _, _ = pgtq_env
    conn.queue_result(fetchone=(42,))

    args = {"alpha": "beta"}
    expected = timedelta(seconds=30)
    task_id = queue.enqueue(
        "run_job",
        args=args,
        priority=5,
        expected_duration=expected,
    )

    assert task_id == 42
    assert len(conn.executed) == 2
    _, params = conn.executed[0]
    assert params == ("run_job", json.dumps(args), 5, expected, None)
    assert "NOTIFY" in str(conn.executed[1][0])


def test_batch_enqueue_defers_notifications(pgtq_env, monkeypatch):
    queue, conn, _, _ = pgtq_env
    conn.queue_result(fetchone=(1,))

    notifications = []
    monkeypatch.setattr(queue, "notify", lambda: notifications.append("sent"))

    with queue.batch_enqueue():
        queue.enqueue("batched")

    assert notifications == ["sent"]
    assert len(conn.executed) == 1  # insert only, notify happened via patched method


def test_batch_enqueue_sets_batch_id_for_enqueues(pgtq_env):
    queue, conn, _, _ = pgtq_env
    conn.queue_result(fetchone=(1,))
    batch_value = uuid4()

    with queue.batch_enqueue(batch_id=batch_value):
        queue.enqueue("batched")

    _, params = conn.executed[0]
    assert params[-1] == batch_value


def test_batch_enqueue_rejects_conflicting_batch_id(pgtq_env):
    queue, conn, _, _ = pgtq_env
    conn.queue_result(fetchone=(1,))

    with queue.batch_enqueue(batch_id=uuid4()):
        with pytest.raises(ValueError):
            with queue.batch_enqueue(batch_id=uuid4()):
                queue.enqueue("batched")


def test_notify_sends_manual_signal(pgtq_env):
    queue, conn, _, logs = pgtq_env

    queue.notify()

    assert "NOTIFY" in str(conn.executed[-1][0])
    assert logs[-1].startswith("[pgtq] sent manual NOTIFY")


def test_dequeue_one_returns_task_with_parsed_fields(pgtq_env):
    queue, conn, _, _ = pgtq_env
    now = datetime.now(timezone.utc)
    row = {
        "id": 5,
        "call": "add",
        "args": '{"value": 7}',
        "priority": 2,
        "status": "queued",
        "inserted_at": now,
        "started_at": now,
        "last_heartbeat": now,
        "expected_duration": timedelta(seconds=3),
        "batch_id": uuid4(),
    }
    conn.queue_result(fetchone=row)

    task = queue.dequeue_one(["add"])

    assert task is not None
    assert task.id == 5
    assert task.args == {"value": 7}
    assert task.expected_duration == timedelta(seconds=3)
    assert task.batch_id == row["batch_id"]
    assert conn.executed[0][1] == [["add"]]


def test_dequeue_one_returns_none_when_no_rows(pgtq_env):
    queue, conn, _, _ = pgtq_env

    task = queue.dequeue_one()

    assert task is None
    assert len(conn.executed) == 1


def test_listen_waits_for_notify_then_yields_task(pgtq_env, monkeypatch):
    queue, _, listen_conn, logs = pgtq_env
    listen_conn.notify_queue = ["ping"]
    dequeue_values = iter(
        [
            None,
            make_task(id=99, call="job"),
            make_task(id=100, call="job"),
        ]
    )

    def fake_dequeue(acceptable_tasks=None):
        return next(dequeue_values)

    def fake_select(read_list, *_rest):
        assert read_list == [listen_conn]
        return (read_list, [], [])

    monkeypatch.setattr(queue, "dequeue_one", fake_dequeue)
    monkeypatch.setattr("pgtq.core.select.select", fake_select)

    gen = queue.listen()
    first = next(gen)
    second = next(gen)

    assert [first.id, second.id] == [99, 100]
    assert any("listening on channel" in message for message in logs)
    assert logs[-1].startswith("[pgtq] got NOTIFY")
    gen.close()


def test_listen_handles_timeout_without_notify(pgtq_env, monkeypatch):
    queue, _, listen_conn, _ = pgtq_env

    def fake_dequeue(acceptable_tasks=None):
        return None

    calls = {"count": 0}

    class StopListening(Exception):
        pass

    def fake_select(*args):
        if calls["count"] == 0:
            calls["count"] += 1
            return ([], [], [])
        raise StopListening

    monkeypatch.setattr(queue, "dequeue_one", fake_dequeue)
    monkeypatch.setattr("pgtq.core.select.select", fake_select)

    gen = queue.listen()

    with pytest.raises(StopListening):
        next(gen)


def test_wait_for_batch_polls_until_complete(pgtq_env, monkeypatch):
    queue, conn, _, logs = pgtq_env
    conn.queue_result(fetchone=(2,))
    conn.queue_result(fetchone=(0,))

    sleeps = []
    monkeypatch.setattr("pgtq.core.time.sleep", lambda duration: sleeps.append(duration))

    queue.wait_for_batch("batch-1", poll_interval=0.5)

    assert conn.executed[0][1] == ("batch-1",)
    assert conn.executed[1][1] == ("batch-1",)
    assert sleeps == [0.5]
    assert any("batch batch-1 has no pending tasks" in msg for msg in logs)


def test_wait_for_batch_times_out(pgtq_env, monkeypatch):
    queue, conn, _, _ = pgtq_env
    conn.queue_result(fetchone=(1,))
    conn.queue_result(fetchone=(1,))
    conn.queue_result(fetchone=(1,))

    timeline = {"now": 0.0}

    def fake_monotonic():
        return timeline["now"]

    def fake_sleep(duration):
        timeline["now"] += duration

    monkeypatch.setattr("pgtq.core.time.monotonic", fake_monotonic)
    monkeypatch.setattr("pgtq.core.time.sleep", fake_sleep)

    with pytest.raises(TimeoutError):
        queue.wait_for_batch("batch-2", poll_interval=0.25, timeout=0.5)


def test_complete_delete_and_mark_done(pgtq_env):
    queue, conn, _, _ = pgtq_env

    queue.complete(7)
    assert "DELETE FROM" in str(conn.executed[-1][0])
    assert conn.executed[-1][1] == (7,)

    queue.complete(8, delete=False)
    assert "SET status = 'done'" in str(conn.executed[-1][0])
    assert conn.executed[-1][1] == (8,)


def test_heartbeat_updates_last_heartbeat(pgtq_env):
    queue, conn, _, _ = pgtq_env

    queue.heartbeat(10)

    assert "SET last_heartbeat = now()" in str(conn.executed[-1][0])
    assert conn.executed[-1][1] == (10,)


def test_fail_requeues_and_notifies(pgtq_env):
    queue, conn, _, _ = pgtq_env
    conn.queue_result(fetchone=(0,))

    queue.fail(11, requeue=True, error="boom")

    assert len(conn.executed) == 3  # select, update, notify
    assert "retry_count" in str(conn.executed[1][0])
    assert conn.executed[1][1] == ("boom", 11)
    assert "NOTIFY" in str(conn.executed[2][0])


def test_fail_respects_max_retries(pgtq_env):
    queue, conn, _, _ = pgtq_env
    conn.queue_result(fetchone=(3,))

    queue.fail(12, requeue=True, error="boom", max_retries=3)

    assert len(conn.executed) == 2
    assert "status = 'failed'" in str(conn.executed[-1][0])
    assert conn.executed[-1][1] == ("boom", 12)


def test_fail_returns_when_task_missing(pgtq_env):
    queue, conn, _, _ = pgtq_env
    conn.queue_result(fetchone=None)

    queue.fail(13, requeue=True)

    assert len(conn.executed) == 1


def test_requeue_stale_in_progress_notifies_when_rows(pgtq_env):
    queue, conn, _, _ = pgtq_env
    conn.queue_result(fetchall=[(1,), (2,)])

    count = queue.requeue_stale_in_progress(default_grace=timedelta(seconds=1))

    assert count == 2
    assert len(conn.executed) == 2  # update + notify
    assert "NOTIFY" in str(conn.executed[-1][0])


def test_requeue_stale_in_progress_no_notify_when_none(pgtq_env):
    queue, conn, _, _ = pgtq_env
    conn.queue_result(fetchall=[])

    count = queue.requeue_stale_in_progress()

    assert count == 0
    assert len(conn.executed) == 1


def test_task_decorator_registers_once(pgtq_env):
    queue, _, _, _ = pgtq_env

    @queue.task("job")
    def handler():
        return "ok"

    assert queue.registered_task_names == ["job"]

    with pytest.raises(ValueError):
        @queue.task("job")
        def handler_dup():
            return "ok"


def test_run_task_success_calls_complete(pgtq_env):
    queue, _, _, _ = pgtq_env
    completed = []

    @queue.task("add")
    def handler(a, b):
        return a + b

    queue.complete = lambda task_id, delete=True: completed.append((task_id, delete))
    task = make_task(id=22, call="add", args={"a": 1, "b": 2})

    result = queue.run_task(task)

    assert result == 3
    assert completed == [(22, True)]


def test_run_task_failure_calls_fail(pgtq_env):
    queue, _, _, _ = pgtq_env
    failures = []

    @queue.task("explode")
    def handler():
        raise RuntimeError("boom")

    def fake_fail(task_id, *, requeue=False, error=None, max_retries=None):
        failures.append((task_id, requeue, error))

    queue.fail = fake_fail
    task = make_task(id=33, call="explode")

    result = queue.run_task(task)

    assert result is None
    assert failures == [(33, True, "boom")]


def test_run_task_unregistered_calls_fail(pgtq_env):
    queue, _, _, _ = pgtq_env
    failures = []

    def fake_fail(task_id, *, requeue=False, error=None, max_retries=None):
        failures.append((task_id, requeue, error))

    queue.fail = fake_fail
    task = make_task(id=44, call="missing")

    result = queue.run_task(task)

    assert result is None
    assert failures == [(44, False, "unregistered task")]


def test_run_supervisor_logs_requeue_and_sleeps(pgtq_env, monkeypatch):
    queue, _, _, logs = pgtq_env

    def fake_requeue(*, default_grace):
        return 2

    def stop_sleep(_interval):
        raise RuntimeError("stop")

    queue.requeue_stale_in_progress = fake_requeue
    monkeypatch.setattr("pgtq.core.time.sleep", stop_sleep)

    with pytest.raises(RuntimeError):
        queue.run_supervisor_forever(interval=0.01, default_grace=timedelta(seconds=1))

    assert logs[0].startswith("[pgtq] supervisor started")
    assert any("requeued 2" in message for message in logs)


def test_run_supervisor_logs_errors(pgtq_env, monkeypatch):
    queue, _, _, logs = pgtq_env

    def failing_supervisor(*, default_grace):
        raise ValueError("boom")

    def stop_sleep(_interval):
        raise RuntimeError("halt")

    queue.requeue_stale_in_progress = failing_supervisor
    monkeypatch.setattr("pgtq.core.time.sleep", stop_sleep)

    with pytest.raises(RuntimeError):
        queue.run_supervisor_forever(interval=0.01)

    assert any("supervisor error" in message for message in logs)


def test_start_worker_processes_tasks(pgtq_env):
    queue, _, _, _ = pgtq_env
    processed = []
    task = make_task(id=55, call="job")

    @queue.task("job")
    def handler():
        return None

    def fake_listen(*, acceptable_tasks, idle_poll_interval):
        assert acceptable_tasks == ["job"]
        yield task

    queue.listen = fake_listen
    queue.run_task = lambda t: processed.append(t.id)

    queue.start_worker()

    assert processed == [55]


def test_parse_interval_handles_none():
    assert PGTQ._parse_interval(None) is None


def test_parse_interval_handles_timedelta():
    delta = timedelta(seconds=5)
    assert PGTQ._parse_interval(delta) is delta


def test_parse_interval_handles_invalid_values():
    assert PGTQ._parse_interval("bad") is None


def test_parse_json_handles_none():
    assert PGTQ._parse_json(None) == {}


def test_parse_json_passthrough_dict():
    payload = {"x": 1}
    assert PGTQ._parse_json(payload) is payload


def test_parse_json_handles_invalid_input():
    assert PGTQ._parse_json("not-json") == {}
