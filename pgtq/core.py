#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Definitions of PGTQ class
"""

import json
import select
import threading
import time
from datetime import timedelta
from typing import Any, Callable, Dict, Generator, Optional, Sequence
from contextlib import contextmanager

import psycopg
from psycopg import sql

from .task import Task

Log = Callable[[str], None]


class PGTQ:
    """
    PostgreSQL Task Queue (pgtq)

    - Uses a tasks table in Postgres
    - Dequeue uses SKIP LOCKED to safely distribute work across many workers
    - LISTEN/NOTIFY used to wake workers instead of pure polling

    Requires psycopg.

    Parameters:
        dsn: PostgreSQL DSN for connecting to the database.
        table_name: name of the tasks table (default: "pgtq_tasks").
        channel_name: name of the NOTIFY channel (default: "pgtq_new_tasks").
        log_fn: optional callable for logging messages (e.g., print or logger.info).
    """

    def __init__(
        self,
        dsn: str,
        table_name: str = "pgtq_tasks",
        channel_name: str = "pgtq_new_tasks",
        log_fn: Optional[Callable[[str], None]] = None,
    ) -> None:
        self.dsn = dsn
        self.table_name = table_name
        self.channel_name = channel_name

        # One connection for DDL/DML
        self._conn = psycopg.connect(dsn)
        self._conn.autocommit = True

        # One connection dedicated to LISTEN/NOTIFY
        self._listen_conn = psycopg.connect(dsn)
        self._listen_conn.autocommit = True

        self._install_lock = threading.Lock()

        if log_fn is None:

            def log_fn(msg: str) -> None:
                # default to silent; user can pass their own logger
                pass

        self.log = log_fn

        self._registry = {}
        self._batching = False

    # ----------------------------------------------------------------------
    # Setup / schema helpers
    # ----------------------------------------------------------------------

    def install(self) -> None:
        """
        Create the tasks table and indexes if they don't exist.
        Safe to call multiple times.
        """
        with self._install_lock:
            with self._conn.cursor() as cur:
                self.log(f"[pgtq] installing table '{self.table_name}' if not exists.")
                cur.execute(
                    sql.SQL(
                        """
                        CREATE TABLE IF NOT EXISTS {table} (
                            id BIGSERIAL PRIMARY KEY,
                            call TEXT NOT NULL,
                            args JSONB NOT NULL DEFAULT '{{}}'::jsonb,

                            status TEXT NOT NULL DEFAULT 'queued',
                            priority INTEGER NOT NULL DEFAULT 0,

                            inserted_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
                            started_at TIMESTAMP WITH TIME ZONE,
                            last_heartbeat TIMESTAMP WITH TIME ZONE,
                            expected_duration INTERVAL,

                            retry_count INTEGER NOT NULL DEFAULT 0,
                            last_error TEXT
                        );
                        """
                    ).format(table=sql.Identifier(self.table_name))
                )

                # Index for dequeue
                self.log(f"[pgtq] ensuring indexes on table '{self.table_name}'.")
                cur.execute(
                    sql.SQL(
                        """
                        CREATE INDEX IF NOT EXISTS {idx_status_prio}
                        ON {table} (status, priority, id);
                        """
                    ).format(
                        idx_status_prio=sql.Identifier(
                            f"{self.table_name}_status_priority_id_idx"
                        ),
                        table=sql.Identifier(self.table_name),
                    )
                )

                # Index for requeueing stale in-progress tasks
                self.log(
                    f"[pgtq] ensuring heartbeat index on table '{self.table_name}'."
                )
                cur.execute(
                    sql.SQL(
                        """
                        CREATE INDEX IF NOT EXISTS {idx_inprogress_heartbeat}
                        ON {table} (status, last_heartbeat);
                        """
                    ).format(
                        idx_inprogress_heartbeat=sql.Identifier(
                            f"{self.table_name}_status_heartbeat_idx"
                        ),
                        table=sql.Identifier(self.table_name),
                    )
                )

    # ----------------------------------------------------------------------
    # Enqueue
    # ----------------------------------------------------------------------

    def enqueue(
        self,
        call: str,
        args: Optional[Dict[str, Any]] = None,
        *,
        priority: int = 0,
        expected_duration: Optional[timedelta] = None,
        notify: bool = True,
    ) -> int:
        """
        Add a new task to the queue and optionally NOTIFY workers.

        Returns the new task id.
        """

        if args is None:
            args = {}

        with self._conn.cursor() as cur:
            cur.execute(
                sql.SQL(
                    """
                    INSERT INTO {table} (call, args, priority, expected_duration)
                    VALUES (%s, %s, %s, %s)
                    RETURNING id;
                    """
                ).format(table=sql.Identifier(self.table_name)),
                (
                    call,
                    json.dumps(args),
                    priority,
                    expected_duration,
                ),
            )
            task_id = cur.fetchone()[0]

            if notify and not self._batching:
                self._notify_new_tasks(cur)
                self.log(f"[pgtq] sent NOTIFY on channel '{self.channel_name}'")

        self.log(f"[pgtq] enqueued task {task_id} ({call})")
        return task_id

    @contextmanager
    def batch_enqueue(self):
        """
        Context manager to batch multiple enqueue calls without notifying
        workers until the end of the block.

        Usage:

            with pgtq.batch_enqueue():
                pgtq.enqueue(...)
                pgtq.enqueue(...)
                ...
        """
        already_batching = self._batching

        self._batching = True

        try:
            yield
        finally:
            if not already_batching:
                self._batching = False
                self.notify()

    def notify(self) -> None:
        """
        Send a NOTIFY to wake up listening workers. You do _not_ need to call
        this if you used enqueue(..., notify=True). This is only useful if you
        queued a thousand tasks or so without notifying, and now want to wake up
        workers.
        """
        with self._conn.cursor() as cur:
            self._notify_new_tasks(cur)
            self.log(f"[pgtq] sent manual NOTIFY on channel '{self.channel_name}'")

    def _notify_new_tasks(self, cur: psycopg.Cursor) -> None:
        cur.execute(
            sql.SQL("NOTIFY {chan};").format(chan=sql.Identifier(self.channel_name))
        )

    # ----------------------------------------------------------------------
    # Dequeue / worker-facing API
    # ----------------------------------------------------------------------

    def dequeue_one(
        self,
        acceptable_tasks: Optional[Sequence[str]] = None,
    ) -> Optional[Task]:
        """
        Atomically claim one queued task (status='queued') using SKIP LOCKED.

        Returns a Task or None if no suitable task is available.
        """
        with self._conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            where_fragments = ["status = 'queued'"]
            params: list[Any] = []

            if acceptable_tasks:
                where_fragments.append("call = ANY(%s)")
                params.append(list(acceptable_tasks))

            where_clause = " AND ".join(where_fragments)

            query = sql.SQL(
                f"""
                WITH next AS (
                    SELECT id
                    FROM {{table}}
                    WHERE {where_clause}
                    ORDER BY priority ASC, id ASC
                    FOR UPDATE SKIP LOCKED
                    LIMIT 1
                )
                UPDATE {{table}} t
                SET
                    status = 'in_progress',
                    started_at = COALESCE(started_at, now()),
                    last_heartbeat = now()
                FROM next
                WHERE t.id = next.id
                RETURNING
                    t.id, t.call, t.args, t.priority, t.status,
                    t.inserted_at, t.started_at, t.last_heartbeat,
                    t.expected_duration;
                """
            ).format(table=sql.Identifier(self.table_name))

            cur.execute(query, params)
            row = cur.fetchone()
            if not row:
                return None

            task = self._row_to_task(row)
            self.log(f"[pgtq] claimed task {task.id} ({task.call})")
            return task

    def listen(
        self,
        acceptable_tasks: Optional[Sequence[str]] = None,
        *,
        idle_poll_interval: float = 30.0,
    ) -> Generator[Task, None, None]:
        """
        Generator that yields tasks indefinitely.

        It:
        - Tries to dequeue immediately.
        - If none available, blocks on LISTEN/NOTIFY (with a timeout).
        - When notified or timeout fires, tries dequeue again.

        Usage:

            pgtq = PGTQ(dsn=...)
            pgtq.install()

            for task in pgtq.listen(["add_numbers", "multiply_numbers"]):
                handle(task)
        """
        # Set up LISTEN on the dedicated connection
        with self._listen_conn.cursor() as lcur:
            lcur.execute(
                sql.SQL("LISTEN {chan};").format(chan=sql.Identifier(self.channel_name))
            )
        self.log(
            f"[pgtq] listening on channel '{self.channel_name}' "
            f"for tasks {acceptable_tasks or 'all'}"
        )

        if acceptable_tasks is None:
            acceptable_tasks = self.registered_task_names

        while True:
            # First attempt a dequeue without waiting
            task = self.dequeue_one(acceptable_tasks)
            if task is not None:
                yield task
                continue

            # Nothing available - wait for NOTIFY or timeout
            if select.select([self._listen_conn], [], [], idle_poll_interval)[0]:
                # There is data to read (a NOTIFY)
                self._listen_conn.pgconn.consume_input()

                while True:
                    notify = self._listen_conn.notifies()
                    if notify is None:
                        break

                self.log(f"[pgtq] got NOTIFY on channel '{self.channel_name}'")
            else:
                # Timeout expired, just loop and try dequeue again
                continue

    # ----------------------------------------------------------------------
    # Task lifecycle helpers
    # ----------------------------------------------------------------------

    def heartbeat(self, task_id: int) -> None:
        """
        Update last_heartbeat for a running task.
        """
        with self._conn.cursor() as cur:
            cur.execute(
                sql.SQL(
                    """
                    UPDATE {table}
                    SET last_heartbeat = now()
                    WHERE id = %s AND status = 'in_progress';
                    """
                ).format(table=sql.Identifier(self.table_name)),
                (task_id,),
            )

    def complete(self, task_id: int, delete: bool = True) -> None:
        """
        Mark task as completed.

        If delete=True, remove the row.
        Otherwise, set status='done'.
        """
        with self._conn.cursor() as cur:
            if delete:
                cur.execute(
                    sql.SQL("DELETE FROM {table} WHERE id = %s;").format(
                        table=sql.Identifier(self.table_name)
                    ),
                    (task_id,),
                )
            else:
                cur.execute(
                    sql.SQL(
                        """
                        UPDATE {table}
                        SET status = 'done'
                        WHERE id = %s;
                        """
                    ).format(table=sql.Identifier(self.table_name)),
                    (task_id,),
                )

    def fail(
        self,
        task_id: int,
        *,
        requeue: bool = False,
        error: Optional[str] = None,
        max_retries: Optional[int] = None,
    ) -> None:
        """
        Mark a task as failed, optionally requeue.

        If max_retries is not None and retry_count >= max_retries,
        the task is left in 'failed' status.
        """
        with self._conn.cursor() as cur:
            # Fetch retry_count
            cur.execute(
                sql.SQL(
                    """
                    SELECT retry_count
                    FROM {table}
                    WHERE id = %s;
                    """
                ).format(table=sql.Identifier(self.table_name)),
                (task_id,),
            )
            row = cur.fetchone()
            if not row:
                return

            (retry_count,) = row

            if requeue and (max_retries is None or retry_count < max_retries):
                cur.execute(
                    sql.SQL(
                        """
                        UPDATE {table}
                        SET
                            status = 'queued',
                            retry_count = retry_count + 1,
                            last_error = %s
                        WHERE id = %s;
                        """
                    ).format(table=sql.Identifier(self.table_name)),
                    (error, task_id),
                )
                self._notify_new_tasks(cur)
            else:
                cur.execute(
                    sql.SQL(
                        """
                        UPDATE {table}
                        SET
                            status = 'failed',
                            last_error = %s
                        WHERE id = %s;
                        """
                    ).format(table=sql.Identifier(self.table_name)),
                    (error, task_id),
                )

    def requeue_stale_in_progress(
        self,
        *,
        default_grace: timedelta = timedelta(minutes=5),
    ) -> int:
        """
        Requeue tasks that have been 'in_progress' longer than their
        expected_duration (or default_grace if expected_duration is NULL).

        Returns number of tasks requeued.
        """
        with self._conn.cursor() as cur:
            cur.execute(
                sql.SQL(
                    """
                    UPDATE {table}
                    SET status = 'queued'
                    WHERE status = 'in_progress'
                      AND now() - COALESCE(last_heartbeat, started_at, inserted_at)
                          > COALESCE(expected_duration, %s)
                    RETURNING id;
                    """
                ).format(table=sql.Identifier(self.table_name)),
                (default_grace,),
            )
            rows = cur.fetchall()
            requeued_count = len(rows)

            if requeued_count > 0:
                self._notify_new_tasks(cur)

            return requeued_count

    # ----------------------------------------------------------------------
    # supervisor and worker loops
    # ----------------------------------------------------------------------

    def run_supervisor_forever(
        self,
        *,
        interval: float = 60.0,
        default_grace: timedelta = timedelta(minutes=5),
    ) -> None:
        """
        Run a simple supervisor loop forever.

        Responsibilities:
        - Periodically requeue stale in-progress tasks.

        This is safe to run in multiple controller processes:
        - The UPDATE in requeue_stale_in_progress is idempotent and guarded by WHERE.
        - Multiple supervisors may try, but only one will actually change a given row.
        - Others will see it already updated and do nothing.

        Parameters:
            interval: seconds between supervisor iterations.
            default_grace: time allowed before in-progress tasks are considered stale.
        """
        self.log(
            f"[pgtq] supervisor started (interval={interval}s, grace={default_grace})."
        )

        while True:
            try:
                requeued = self.requeue_stale_in_progress(default_grace=default_grace)
                if requeued:
                    self.log(f"[pgtq] supervisor requeued {requeued} stale tasks.")
            except Exception as e:
                # We deliberately swallow exceptions to keep the supervisor alive.
                self.log(f"[pgtq] supervisor error: {e!r}")
            finally:
                time.sleep(interval)

    def start_worker(
        self,
        *,
        idle_poll_interval: float = 30.0,
    ):
        """
        Start a worker loop that listens for tasks and runs them using the
        registered task handlers via run_task().

        Automatically filters tasks based on registered handlers.
        """
        self.log(f"[pgtq] worker starting with handlers: {self.registered_task_names}")

        for task in self.listen(
            acceptable_tasks=self.registered_task_names,
            idle_poll_interval=idle_poll_interval,
        ):
            self.run_task(task)

    # ----------------------------------------------------------------------
    # Internal helpers
    # ----------------------------------------------------------------------

    @staticmethod
    def _parse_interval(value: Any) -> Optional[timedelta]:
        if value is None:
            return None
        # psycopg converts INTERVAL to timedelta by default
        if isinstance(value, timedelta):
            return value
        return None

    @staticmethod
    def _parse_json(value: Any) -> Dict[str, Any]:
        if value is None:
            return {}
        if isinstance(value, dict):
            return value
        try:
            return json.loads(value)
        except Exception:
            return {}

    def _row_to_task(self, row: Dict[str, Any]) -> Task:
        return Task(
            id=row["id"],
            call=row["call"],
            args=self._parse_json(row["args"]),
            priority=row["priority"],
            status=row["status"],
            inserted_at=row["inserted_at"],
            started_at=row.get("started_at"),
            last_heartbeat=row.get("last_heartbeat"),
            expected_duration=self._parse_interval(row.get("expected_duration")),
        )

    # ----------------------------------------------------------------------
    # Task registration decorator
    # ----------------------------------------------------------------------

    def task(self, name: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        """
        Decorator to register a function as a task handler.
        """

        def decorator(func):
            if name in self._registry:
                raise ValueError(f"Task '{name}' already registered")

            self._registry[name] = func
            self.log(f"[pgtq] registered task '{name}' → {func.__name__}")
            return func

        return decorator

    def run_task(self, task: Task) -> None:
        """
        Run a registered task by its Task object.
        """

        func = self._registry.get(task.call)

        if func is None:
            self.log(f"[pgtq] no registered handler for '{task.call}', failing task")
            self.fail(task.id, error="unregistered task", requeue=False)
            return

        try:
            result = func(**task.args)
            self.complete(task.id, delete=True)
            self.log(f"[pgtq] task {task.id} ({task.call}) completed successfully")
            return result

        except Exception as e:
            self.log(f"[pgtq] task {task.id} ({task.call}) failed: {e}")
            self.fail(task.id, error=str(e), requeue=True)
            return None

    # ----------------------------------------------------------------------
    # Properties
    # ----------------------------------------------------------------------

    @property
    def registered_task_names(self):
        return list(self._registry.keys())
