"""Shared Sentry span helpers for explicit async/IO instrumentation."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from contextlib import contextmanager
from typing import Any

import sentry_sdk


@contextmanager
def sentry_span(
    *,
    op: str,
    name: str,
    data: dict[str, Any] | None = None,
) -> Any:
    """Create a Sentry span with optional structured data."""
    with sentry_sdk.start_span(op=op, name=name) as span:
        for key, value in (data or {}).items():
            if value is not None:
                span.set_data(key, value)
        try:
            yield span
        except Exception as exc:
            span.set_status("internal_error")
            span.set_data("error_type", type(exc).__name__)
            raise


async def traced_to_thread[T](
    fn: Callable[..., T],
    *args: Any,
    op: str,
    name: str,
    data: dict[str, Any] | None = None,
    **kwargs: Any,
) -> T:
    """Run a sync function in a thread while measuring it as a Sentry span."""
    with sentry_span(op=op, name=name, data=data):
        return await asyncio.to_thread(fn, *args, **kwargs)


async def traced_await[T](
    awaitable: Awaitable[T],
    *,
    op: str,
    name: str,
    data: dict[str, Any] | None = None,
) -> T:
    """Await a coroutine while measuring it as a Sentry span."""
    with sentry_span(op=op, name=name, data=data):
        return await awaitable
