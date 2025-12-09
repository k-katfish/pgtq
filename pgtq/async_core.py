#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Async definitions of PGTQ class
"""

import asyncio
import json
import time
from contextlib import asynccontextmanager
from datetime import timedelta
from typing import (
    Any,
    AsyncGenerator,
    Awaitable,
    Callable,
    Dict,
    List,
    Optional,
    Sequence,
    Union,
)
from uuid import UUID

import psycopg
from psycopg.rows import dict_row

from .task import Task

Log = Callable[[str], None]


class AsyncPGTQ:
    """
    Async PostgreSQL Task Queue.

    Async equivalent of the sync PGTQ class, using psycopg3 async connections.

    Usage:

        from pgtq.async_core import AsyncPGTQ

        async def main():
            pgtq = await AsyncPGTQ.create(dsn="postgresql://...")
            await pgtq.install()

            @pgtq.task("add_numbers")
            async def add(a, b):
                return a + b

            await pgtq.enqueue("add_numbers", args={"a": 1, "b": 2})
            await pgtq.start_worker()

        asyncio.run(main())
    """

    def __init__(
        self,
        dsn: str,
        conn: psycopg.AsyncConnection,
        listen_conn: psycopg.AsyncConnection,
        table_name: str = "pgtq_tasks",
        channel_name: str = "pgtq_new_tasks",
        log_fn: Optional[Log] = None,
    ) -> None:
        self.dsn = dsn
        self._conn = conn
        self._listen_conn = listen_conn
        self.table_name = table_name
        self.channel_name = channel_name
        self.log: Log = log_fn or (lambda _msg: None)

        # registry for task handlers
        self._registry: Dict[str, Callable[..., Any]] = {}
        # batching flag for batch_enqueue()
        self._batching: bool = False
        self._batch_batch_id: Optional[UUID] = None

    # ------------------------------------------------------------------
    # Construction helpers
    # ------------------------------------------------------------------

    @classmethod
    async def create(
        cls,
        dsn: str,
        table_name: str = "pgtq_tasks",
        channel_name: str = "pgtq_new_tasks",
        log_fn: Optional[Log] = None,
    ) -> "AsyncPGTQ":
        """
        Async constructor – creates async connections and returns an instance.
        """
        conn = await psycopg.AsyncConnection.connect(dsn, autocommit=True)
        listen_conn = await psycopg.AsyncConnection.connect(dsn, autocommit=True)
        return cls(
            dsn=dsn,
            conn=conn,
            listen_conn=listen_conn,
            table_name=table_name,
            channel_name=channel_name,
            log_fn=log_fn,
        )

    async def close(self) -> None:
        """
        Close underlying async connections.
        """
        await self._conn.close()
        await self._listen_conn.close()
        self.log("[pgtq-async] connections closed")

    # ------------------------------------------------------------------
    # Schema
    # ------------------------------------------------------------------

    async def install(self) -> None:
        """
        Create the tasks table and indexes if they don't exist.
        Safe to call multiple times.
        """
        async with self._conn.cursor() as cur:
            self.log(
                f"[pgtq-async] installing table '{self.table_name}' if not exists."
            )
            await cur.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {self.table_name} (
                    id BIGSERIAL PRIMARY KEY,
                    call TEXT NOT NULL,
                    args JSONB NOT NULL DEFAULT '{{}}'::jsonb,

                    status TEXT NOT NULL DEFAULT 'queued',
                    priority INTEGER NOT NULL DEFAULT 0,

                    inserted_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
                    started_at TIMESTAMP WITH TIME ZONE,
                    last_heartbeat TIMESTAMP WITH TIME ZONE,
                    expected_duration INTERVAL,
                    batch_id UUID,

                    retry_count INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT
                );
                """
            )

            self.log(f"[pgtq-async] ensuring indexes on table '{self.table_name}'.")
            await cur.execute(
                f"""
                CREATE INDEX IF NOT EXISTS {self.table_name}_status_priority_id_idx
                ON {self.table_name} (status, priority, id);
                """
            )

            self.log(
                f"[pgtq-async] ensuring heartbeat index on table '{self.table_name}'."
            )
            await cur.execute(
                f"""
                CREATE INDEX IF NOT EXISTS {self.table_name}_status_heartbeat_idx
                ON {self.table_name} (status, last_heartbeat);
                """
            )

            self.log(
                f"[pgtq-async] ensuring batch_id column exists on '{self.table_name}'."
            )
            await cur.execute(
                f"""
                ALTER TABLE {self.table_name}
                ADD COLUMN IF NOT EXISTS batch_id UUID;
                """
            )

            self.log(f"[pgtq-async] ensuring batch index on table '{self.table_name}'.")
            await cur.execute(
                f"""
                CREATE INDEX IF NOT EXISTS {self.table_name}_batch_status_idx
                ON {self.table_name} (batch_id, status);
                """
            )

        self.log("[pgtq-async] install complete")

    # ------------------------------------------------------------------
    # Task registration
    # ------------------------------------------------------------------

    def task(self, name: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        """
        Decorator to register a function as a task handler.
        """

        def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
            if name in self._registry:
                raise ValueError(f"Task '{name}' already registered")

            self._registry[name] = func
            self.log(f"[pgtq-async] registered task '{name}' → {func.__name__}")
            return func

        return decorator

    @property
    def registered_task_names(self) -> List[str]:
        return list(self._registry.keys())

    # ------------------------------------------------------------------
    # Enqueue / batching / notify
    # ------------------------------------------------------------------

    async def enqueue(
        self,
        call: str,
        args: Optional[Dict[str, Any]] = None,
        *,
        priority: int = 0,
        expected_duration: Optional[timedelta] = None,
        notify: bool = True,
        batch_id: Optional[UUID] = None,
    ) -> int:
        """
        Add a new task to the queue and optionally NOTIFY workers.

        Returns the new task id.
        """
        if args is None:
            args = {}

        effective_batch_id = batch_id if batch_id is not None else self._batch_batch_id

        async with self._conn.cursor() as cur:
            await cur.execute(
                f"""
                INSERT INTO {self.table_name} (call, args, priority, expected_duration, batch_id)
                VALUES (%s, %s, %s, %s, %s)
                RETURNING id;
                """,
                (
                    call,
                    json.dumps(args),
                    priority,
                    expected_duration,
                    effective_batch_id,
                ),
            )
            row = await cur.fetchone()
            task_id = row[0]

            if notify and not self._batching:
                await self._notify_new_tasks(cur)
                self.log(f"[pgtq-async] sent NOTIFY for new task {task_id} ({call})")

        self.log(f"[pgtq-async] enqueued task {task_id} ({call})")
        return task_id

    async def _notify_new_tasks(self, cur) -> None:
        await cur.execute(f"NOTIFY {self.channel_name};")

    async def notify(self) -> None:
        """
        Manually send a NOTIFY to wake up workers.
        Useful after bulk enqueues with notify=False or batch_enqueue().
        """
        async with self._conn.cursor() as cur:
            await self._notify_new_tasks(cur)
        self.log(f"[pgtq-async] manual NOTIFY on '{self.channel_name}'")

    @asynccontextmanager
    async def batch_enqueue(
        self, *, batch_id: Optional[UUID] = None
    ) -> AsyncGenerator[None, None]:
        """
        Context manager to batch many enqueue() calls and send a single NOTIFY
        at the end.

        Example:

            async with pgtq.batch_enqueue():
                for i in range(1000):
                    await pgtq.enqueue("task", args={...}, notify=False)
        """
        already_batching = self._batching
        previous_batch_id = self._batch_batch_id

        if already_batching:
            if batch_id is not None and previous_batch_id not in (None, batch_id):
                raise ValueError("Cannot override batch_id while already batching")
            effective_batch_id = previous_batch_id
        else:
            effective_batch_id = batch_id

        self._batching = True
        self._batch_batch_id = effective_batch_id
        try:
            yield
        finally:
            if not already_batching:
                self._batching = False
                self._batch_batch_id = None
                await self.notify()
                self.log("[pgtq-async] batch_enqueue complete, sent NOTIFY")
            else:
                self._batch_batch_id = previous_batch_id

    async def wait_for_batch(
        self,
        batch_id: Union[UUID, str],
        *,
        poll_interval: float = 1.0,
        timeout: Optional[float] = None,
    ) -> None:
        """
        Block until there are no queued or in-progress tasks for the batch_id.
        """
        start = time.monotonic()

        while True:
            async with self._conn.cursor() as cur:
                await cur.execute(
                    f"""
                    SELECT COUNT(*)
                    FROM {self.table_name}
                    WHERE batch_id = %s
                      AND status IN ('queued', 'in_progress');
                    """,
                    (batch_id,),
                )
                row = await cur.fetchone()
                remaining = row[0] if row else 0

            if remaining == 0:
                self.log(f"[pgtq-async] batch {batch_id} has no pending tasks")
                return

            elapsed = time.monotonic() - start
            if timeout is not None and elapsed >= timeout:
                raise TimeoutError(
                    f"Batch {batch_id} did not finish within {timeout} seconds"
                )

            sleep_for = poll_interval
            if timeout is not None:
                sleep_for = max(0.0, min(poll_interval, timeout - elapsed))
            await asyncio.sleep(sleep_for)

    # ------------------------------------------------------------------
    # Dequeue / listen
    # ------------------------------------------------------------------

    async def dequeue_one(
        self,
        acceptable_tasks: Optional[Sequence[str]] = None,
        invert_priority: bool = False,
    ) -> Optional[Task]:
        """
        Atomically claim one queued task using SKIP LOCKED.
        Returns a Task or None.
        """
        where_fragments = ["status = 'queued'"]
        params: List[Any] = []

        if acceptable_tasks:
            where_fragments.append("call = ANY(%s)")
            params.append(list(acceptable_tasks))

        where_clause = " AND ".join(where_fragments)

        if invert_priority:
            order_clause = "priority ASC, id ASC"
        else:
            order_clause = "priority DESC, id ASC"

        query = f"""
            WITH next AS (
                SELECT id
                FROM {self.table_name}
                WHERE {where_clause}
                ORDER BY {order_clause}
                FOR UPDATE SKIP LOCKED
                LIMIT 1
            )
            UPDATE {self.table_name} t
            SET
                status = 'in_progress',
                started_at = COALESCE(started_at, now()),
                last_heartbeat = now()
            FROM next
            WHERE t.id = next.id
            RETURNING
                t.id, t.call, t.args, t.priority, t.status,
                t.inserted_at, t.started_at, t.last_heartbeat,
                t.expected_duration, t.batch_id;
        """

        async with self._conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(query, params)
            row = await cur.fetchone()
            if not row:
                return None

        task = self._row_to_task(row)
        self.log(f"[pgtq-async] claimed task {task.id} ({task.call})")
        return task

    async def listen(
        self,
        acceptable_tasks: Optional[Sequence[str]] = None,
        *,
        idle_poll_interval: float = 30.0,
    ) -> AsyncGenerator[Task, None]:
        """
        Async generator that yields tasks indefinitely.

        - Tries to dequeue immediately.
        - If none, awaits NOTIFY or timeout.
        - On wake/timeout, tries dequeue again.
        """

        if acceptable_tasks is None:
            acceptable_tasks = self.registered_task_names

        async with self._listen_conn.cursor() as lcur:
            await lcur.execute(f"LISTEN {self.channel_name};")

        self.log(
            f"[pgtq-async] listening on channel '{self.channel_name}' "
            f"for tasks {acceptable_tasks}"
        )

        while True:
            task = await self.dequeue_one(acceptable_tasks=acceptable_tasks)
            if task is not None:
                yield task
                continue

            # Wait for a NOTIFY, but with a timeout so we can re-check
            try:
                notify = await asyncio.wait_for(
                    anext(self._listen_conn.notifies()),
                    timeout=idle_poll_interval,
                )
                self.log(f"[pgtq-async] got NOTIFY: {notify.payload}")
                # loop, try dequeue again
            except asyncio.TimeoutError:
                # timeout, just loop and re-check
                continue

    # ------------------------------------------------------------------
    # Task lifecycle
    # ------------------------------------------------------------------

    async def heartbeat(self, task_id: int) -> None:
        async with self._conn.cursor() as cur:
            await cur.execute(
                f"""
                UPDATE {self.table_name}
                SET last_heartbeat = now()
                WHERE id = %s AND status = 'in_progress';
                """,
                (task_id,),
            )
        self.log(f"[pgtq-async] heartbeat for task {task_id}")

    async def complete(self, task_id: int, delete: bool = True) -> None:
        async with self._conn.cursor() as cur:
            if delete:
                await cur.execute(
                    f"DELETE FROM {self.table_name} WHERE id = %s;", (task_id,)
                )
                self.log(f"[pgtq-async] completed and deleted task {task_id}")
            else:
                await cur.execute(
                    f"""
                    UPDATE {self.table_name}
                    SET status = 'done'
                    WHERE id = %s;
                    """,
                    (task_id,),
                )
                self.log(f"[pgtq-async] completed task {task_id} (status=done)")

    async def fail(
        self,
        task_id: int,
        *,
        requeue: bool = False,
        error: Optional[str] = None,
        max_retries: Optional[int] = None,
    ) -> None:
        async with self._conn.cursor() as cur:
            await cur.execute(
                f"""
                SELECT retry_count
                FROM {self.table_name}
                WHERE id = %s;
                """,
                (task_id,),
            )
            row = await cur.fetchone()
            if not row:
                return
            retry_count = row[0]

            if requeue and (max_retries is None or retry_count < max_retries):
                await cur.execute(
                    f"""
                    UPDATE {self.table_name}
                    SET
                        status = 'queued',
                        retry_count = retry_count + 1,
                        last_error = %s
                    WHERE id = %s;
                    """,
                    (error, task_id),
                )
                await self._notify_new_tasks(cur)
                self.log(
                    f"[pgtq-async] task {task_id} failed, requeued (retry={retry_count+1})"
                )
            else:
                await cur.execute(
                    f"""
                    UPDATE {self.table_name}
                    SET
                        status = 'failed',
                        last_error = %s
                    WHERE id = %s;
                    """,
                    (error, task_id),
                )
                self.log(f"[pgtq-async] task {task_id} failed permanently")

    async def requeue_stale_in_progress(
        self,
        *,
        default_grace: timedelta = timedelta(minutes=5),
    ) -> int:
        """
        Requeue tasks that have been 'in_progress' longer than their
        expected_duration (or default_grace if expected_duration is NULL).
        """
        async with self._conn.cursor() as cur:
            await cur.execute(
                f"""
                UPDATE {self.table_name}
                SET status = 'queued'
                WHERE status = 'in_progress'
                  AND now() - COALESCE(last_heartbeat, started_at, inserted_at)
                      > COALESCE(expected_duration, %s)
                RETURNING id;
                """,
                (default_grace,),
            )
            rows = await cur.fetchall()
            requeued_count = len(rows)

            if requeued_count > 0:
                await self._notify_new_tasks(cur)
                self.log(
                    f"[pgtq-async] requeued {requeued_count} stale in-progress tasks"
                )

            return requeued_count

    # ------------------------------------------------------------------
    # Supervisor / worker
    # ------------------------------------------------------------------

    async def run_supervisor_forever(
        self,
        *,
        interval: float = 60.0,
        default_grace: timedelta = timedelta(minutes=5),
        log_intervals: bool = False,
    ) -> None:
        """
        Async supervisor loop:

        - Periodically requeues stale in-progress tasks.
        - Safe to run in multiple controller processes.
        """
        self.log(
            f"[pgtq-async] supervisor started (interval={interval}s, grace={default_grace})"
        )

        while True:
            if log_intervals:
                self.log(f"[pgtq-async] supervisor sleeping for {interval} seconds...")

            try:
                requeued = await self.requeue_stale_in_progress(
                    default_grace=default_grace
                )
                if requeued:
                    self.log(f"[pgtq-async] supervisor requeued {requeued} tasks")
            except Exception as e:
                self.log(f"[pgtq-async] supervisor error: {e!r}")
            finally:
                await asyncio.sleep(interval)

    async def start_worker(
        self,
        *,
        idle_poll_interval: float = 30.0,
    ) -> None:
        """
        Start an async worker loop that:

        - listens for tasks (filtered by registered handlers),
        - runs each task via run_task().
        """
        self.log(
            f"[pgtq-async] worker starting with handlers: {self.registered_task_names}"
        )

        async for task in self.listen(
            acceptable_tasks=self.registered_task_names,
            idle_poll_interval=idle_poll_interval,
        ):
            await self.run_task(task)

    # ------------------------------------------------------------------
    # Task dispatch
    # ------------------------------------------------------------------

    async def run_task(self, task: Task) -> Any:
        """
        Run a task by looking up the registered function.
        Handles both sync and async handlers.
        Automatically completes or fails the task.
        """
        func = self._registry.get(task.call)
        if func is None:
            self.log(
                f"[pgtq-async] no handler for '{task.call}', failing task {task.id}"
            )
            await self.fail(task.id, error="unregistered task", requeue=False)
            return None

        try:
            # call handler
            result = func(**task.args)
            if asyncio.iscoroutine(result) or isinstance(result, Awaitable):
                result = await result

            await self.complete(task.id, delete=True)
            self.log(f"[pgtq-async] task {task.id} ({task.call}) completed")
            return result

        except Exception as e:
            self.log(f"[pgtq-async] task {task.id} ({task.call}) failed: {e!r}")
            await self.fail(task.id, error=str(e), requeue=True)
            return None

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_interval(value: Any) -> Optional[timedelta]:
        if value is None:
            return None
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
            batch_id=row.get("batch_id"),
        )
