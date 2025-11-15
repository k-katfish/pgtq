#!/usr/bin/env python3
import os
import asyncio
from pgtq import AsyncPGTQ

DSN = "postgresql://demo:demo@localhost:5432/demo"

CRASH_AFTER_N = int(os.environ.get("CRASH_AFTER_N", "-1"))
task_counter = 0

def log(msg: str):
    print(f"[async-worker {os.getpid()}] {msg}")

async def main():
    pgtq = await AsyncPGTQ.create(DSN, log_fn=log)

    @pgtq.task("append_line")
    async def append_line(filepath: str, text: str):
        global task_counter

        log(f"appending to file: {filepath} → {text}")

        # simulate async file IO
        await asyncio.sleep(2)

        # do actual async I/O
        # NOTE: aiofiles is optional; here we do "fake async" w/ run_in_executor
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(
            None,
            lambda: open(filepath, "a").write(text + "\n")
        )

        task_counter += 1
        if CRASH_AFTER_N >= 0 and task_counter >= CRASH_AFTER_N:
            log(f"INTENTIONALLY CRASHING after {task_counter} tasks!")
            os._exit(1)

        return True

    log("worker starting!")
    await pgtq.start_worker()

if __name__ == "__main__":
    asyncio.run(main())
