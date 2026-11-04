"""
executors.py
~~~~~~~~~~~~
Thread pools for blocking work called from async code.

HTTP requests run their blocking calls on a small bounded pool
(:data:`REQUEST_EXECUTOR`), selected through a context variable that the
request middleware sets; everything else (the background loop) uses the event
loop's default executor. A flood of requests can therefore never take every
worker away from the rain/landing loop.
"""

from __future__ import annotations

import asyncio
import contextvars
import functools
from concurrent.futures import Executor, ThreadPoolExecutor
from typing import Any, Callable, Final, TypeVar

T = TypeVar("T")

REQUEST_WORKERS: Final = 8
REQUEST_EXECUTOR: Final = ThreadPoolExecutor(
    max_workers=REQUEST_WORKERS, thread_name_prefix="rot-request"
)

#: Executor for blocking calls made in the current context (None = default)
current_executor: contextvars.ContextVar[Executor | None] = contextvars.ContextVar(
    "rot_executor", default=None
)


async def to_thread(fn: Callable[..., T], /, *args: Any, **kwargs: Any) -> T:
    """Like :func:`asyncio.to_thread`, on the context's executor.

    Context variables are copied into the worker, as asyncio.to_thread does.
    """
    loop = asyncio.get_running_loop()
    ctx = contextvars.copy_context()
    call = functools.partial(ctx.run, fn, *args, **kwargs)
    return await loop.run_in_executor(current_executor.get(), call)
