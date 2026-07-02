"""ROB-469 PR2: isolated unit tests for the per-tool timeout middleware.

No FastMCP machinery / no main import — the middleware only reads context.message.name
and calls call_next, so a SimpleNamespace context + a stub call_next fully exercises it.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from fastmcp.exceptions import ToolError

from app.mcp_server.timeout_middleware import ToolTimeoutMiddleware


def _ctx(name: str) -> SimpleNamespace:
    return SimpleNamespace(message=SimpleNamespace(name=name))


@pytest.mark.unit
@pytest.mark.asyncio
async def test_slow_tool_times_out_as_toolerror() -> None:
    mw = ToolTimeoutMiddleware(default_timeout_s=0.05)

    async def call_next(ctx):
        await asyncio.sleep(1.0)
        return "done"

    with pytest.raises(ToolError, match="time budget"):
        await mw.on_call_tool(_ctx("some_slow_tool"), call_next)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_fast_tool_passes_through() -> None:
    mw = ToolTimeoutMiddleware(default_timeout_s=5.0)

    async def call_next(ctx):
        return "ok"

    assert await mw.on_call_tool(_ctx("fast"), call_next) == "ok"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_elevated_budget_not_timed_out() -> None:
    mw = ToolTimeoutMiddleware(default_timeout_s=0.05, overrides={"heavy": 1.0})

    async def call_next(ctx):
        await asyncio.sleep(0.2)  # > default 0.05 but < elevated 1.0
        return "ok"

    assert await mw.on_call_tool(_ctx("heavy"), call_next) == "ok"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_disabled_passes_through_without_timeout() -> None:
    mw = ToolTimeoutMiddleware(default_timeout_s=0.01, enabled=False)

    async def call_next(ctx):
        await asyncio.sleep(0.1)
        return "ok"

    assert await mw.on_call_tool(_ctx("slow"), call_next) == "ok"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_zero_budget_is_exempt() -> None:
    mw = ToolTimeoutMiddleware(default_timeout_s=45.0, overrides={"exempt": 0.0})

    async def call_next(ctx):
        await asyncio.sleep(0.1)
        return "ok"

    assert await mw.on_call_tool(_ctx("exempt"), call_next) == "ok"


@pytest.mark.unit
def test_budget_resolution_default_and_overrides() -> None:
    mw = ToolTimeoutMiddleware(default_timeout_s=45.0)
    assert mw._budget_for("a_tool_with_no_override") == 45.0
    assert mw._budget_for("investment_report_generate_from_bundle") == 240.0
    assert mw._budget_for("get_holdings") == 120.0


@pytest.mark.unit
def test_reconcile_tools_share_elevated_budget() -> None:
    # ROB-631: toss_reconcile_orders fans out a single-detail GET per open order
    # and was being killed by the 45s default; it must get the same elevated budget
    # as its KIS/live reconcile siblings.
    mw = ToolTimeoutMiddleware(default_timeout_s=45.0)
    assert mw._budget_for("kis_live_reconcile_orders") == 90.0
    assert mw._budget_for("live_reconcile_orders") == 90.0
    assert mw._budget_for("toss_reconcile_orders") == 90.0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_tool_own_timeouterror_propagates_unwrapped() -> None:
    # A tool that raises its OWN TimeoutError BEFORE the budget elapses must NOT be
    # reported as a budget timeout — it propagates unwrapped (not wrapped in ToolError),
    # so the user-facing message and the mcp.tool.timeout metric stay accurate.
    mw = ToolTimeoutMiddleware(default_timeout_s=5.0)

    async def call_next(ctx):
        raise TimeoutError("tool's own internal timeout")

    with pytest.raises(TimeoutError) as exc_info:
        await mw.on_call_tool(_ctx("tool"), call_next)
    assert not isinstance(exc_info.value, ToolError)
    assert "internal timeout" in str(exc_info.value)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_tool_other_exception_propagates_unwrapped() -> None:
    mw = ToolTimeoutMiddleware(default_timeout_s=5.0)

    async def call_next(ctx):
        raise ValueError("tool error")

    with pytest.raises(ValueError, match="tool error"):
        await mw.on_call_tool(_ctx("tool"), call_next)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_external_cancellation_propagates() -> None:
    # The middleware must NOT swallow CancelledError (e.g. server shutdown cancellation).
    mw = ToolTimeoutMiddleware(default_timeout_s=5.0)

    async def call_next(ctx):
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await mw.on_call_tool(_ctx("tool"), call_next)
