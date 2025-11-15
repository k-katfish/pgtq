#!/usr/bin/env python3
import asyncio
from datetime import timedelta
from pgtq import AsyncPGTQ

DSN = "postgresql://demo:demo@localhost:5432/demo"


async def log(msg: str):
    print(f"[async-controller] {msg}")


async def main():
    pgtq = await AsyncPGTQ.create(DSN, log_fn=lambda m: print(f"[controller] {m}"))
    await pgtq.install()

    # enqueue multiple tasks
    async with pgtq.batch_enqueue():
        for i in range(10):
            await pgtq.enqueue(
                "append_line",
                args={"filepath": f"/tmp/pgtq_async_demo.txt", "text": f"line {i}"},
                expected_duration=timedelta(seconds=5),
                notify=False,
            )

    print("[controller] enqueued tasks. Supervisor starting...")

    # async supervisor loop runs forever (CTRL+C to stop)
    await pgtq.run_supervisor_forever(
        interval=4.0,
        default_grace=timedelta(seconds=4),
    )


if __name__ == "__main__":
    asyncio.run(main())
