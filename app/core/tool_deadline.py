"""Deadline of the MCP tool call that is running in the current context.

``ToolTimeoutMiddleware`` sets this for the span of one bounded ``tools/call``
(the value is the loop-clock deadline of its ``asyncio.timeout``). Code inside a
tool handler reads it to bound optional work, so that work cannot push the call
past its budget. Outside a bounded call the value is ``None``.
"""

from __future__ import annotations

import asyncio
from contextvars import ContextVar, Token

_TOOL_DEADLINE: ContextVar[float | None] = ContextVar("mcp_tool_deadline", default=None)


def set_tool_deadline(deadline: float | None) -> Token[float | None]:
    """Publish the running call's loop-clock deadline; pass the token to reset."""

    return _TOOL_DEADLINE.set(deadline)


def reset_tool_deadline(token: Token[float | None]) -> None:
    _TOOL_DEADLINE.reset(token)


def remaining_tool_budget_s() -> float | None:
    """Seconds left before the running tool call hits its deadline, or ``None``.

    ``None`` means no bounded call is running in this context; callers then keep
    their own default bound.
    """

    deadline = _TOOL_DEADLINE.get()
    if deadline is None:
        return None
    return deadline - asyncio.get_running_loop().time()


__all__ = [
    "remaining_tool_budget_s",
    "reset_tool_deadline",
    "set_tool_deadline",
]
