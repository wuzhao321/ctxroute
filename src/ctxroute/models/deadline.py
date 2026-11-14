"""Wall-time deadline also expires across host suspension; never grants extra time."""

import asyncio
import time
from collections.abc import AsyncGenerator, Callable
from contextlib import asynccontextmanager, suppress


def suspension_aware_clock(
    monotonic: Callable[[], float] = time.monotonic,
    wall: Callable[[], float] = time.time,
) -> Callable[[], float]:
    start, wall_start = monotonic(), wall()
    latest = start

    def now() -> float:
        nonlocal latest
        latest = max(latest, monotonic(), start + wall() - wall_start)
        return latest

    return now


@asynccontextmanager
async def wall_timeout(
    seconds: float, clock: Callable[[], float] = time.time
) -> AsyncGenerator[None]:
    deadline = clock() + seconds
    owner = asyncio.current_task()
    expired = False

    async def watch() -> None:
        nonlocal expired
        while clock() < deadline:
            await asyncio.sleep(min(1.0, max(0, deadline - clock())))
        expired = True
        if owner is not None:
            owner.cancel()

    async with asyncio.timeout(seconds):
        watcher = asyncio.create_task(watch())
        try:
            yield
        except asyncio.CancelledError:
            if expired:
                raise TimeoutError("Wall-time deadline reached") from None
            raise
        finally:
            watcher.cancel()
            with suppress(asyncio.CancelledError):
                await watcher
