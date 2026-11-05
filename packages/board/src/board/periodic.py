import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

logger = logging.getLogger(__name__)


@asynccontextmanager
async def every(seconds: float, fn: Callable[[], Awaitable[None]]) -> AsyncIterator[None]:
    task = asyncio.create_task(_repeat(seconds, fn))
    try:
        yield
    finally:
        task.cancel()
        await asyncio.wait({task})


async def _repeat(seconds: float, fn: Callable[[], Awaitable[None]]) -> None:
    while True:
        await asyncio.sleep(seconds)
        try:
            await fn()
        except Exception:
            logger.exception("periodic task %r failed; retrying in %ss", fn, seconds)
