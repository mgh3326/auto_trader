"""Isolated unit tests for the all-tools invocation log middleware.

The middleware only reads ``context.message.name`` and calls ``call_next``, so a
``SimpleNamespace`` context plus a stub ``call_next`` exercises it fully (same
shape as ``tests/test_mcp_timeout_middleware.py``). The end-to-end proof that the
line survives the real FastMCP middleware chain and the deployed log format lives
in ``tests/mcp_server/test_tool_call_observability.py``.
"""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace

import pytest

from app.mcp_server.tool_call_log_middleware import (
    TOOL_CALL_MESSAGE,
    UNKNOWN_TOOL_NAME,
    ToolCallLogMiddleware,
)

pytestmark = [pytest.mark.unit]

# A live-order tool: the acceptance case where a raising telemetry call must not
# turn an accepted order into an exception (the #2049 failure mode).
ORDER_TOOL = "kis_live_place_order"


def _ctx(name: str) -> SimpleNamespace:
    return SimpleNamespace(message=SimpleNamespace(name=name))


def _lines(caplog: pytest.LogCaptureFixture) -> list[tuple[int, str]]:
    return [
        (record.levelno, record.getMessage())
        for record in caplog.records
        if record.getMessage().startswith("mcp.tool.called")
    ]


@pytest.mark.asyncio
async def test_logs_tool_name_and_profile_in_the_message_body(caplog) -> None:
    middleware = ToolCallLogMiddleware(profile="analysis_readonly")

    async def call_next(ctx):
        return "ok"

    caplog.set_level(logging.INFO)
    assert await middleware.on_call_tool(_ctx("get_quote"), call_next) == "ok"
    assert _lines(caplog) == [
        (logging.INFO, "mcp.tool.called tool=get_quote profile=analysis_readonly")
    ]


@pytest.mark.asyncio
async def test_non_niche_tool_is_observed_too(caplog) -> None:
    """The scope is every tool, not only the audited C ("niche") group."""
    middleware = ToolCallLogMiddleware(profile="default")

    async def call_next(ctx):
        return "ok"

    caplog.set_level(logging.INFO)
    # get_quote is deliberately NOT in NICHE_GROUPS for any profile.
    from app.mcp_server.tooling.niche import NICHE_GROUPS

    assert not any("get_quote" in names for _profiles, names in NICHE_GROUPS)
    await middleware.on_call_tool(_ctx("get_quote"), call_next)
    assert _lines(caplog) == [
        (logging.INFO, "mcp.tool.called tool=get_quote profile=default")
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("tool_name", [ORDER_TOOL, "toss_place_order", "get_quote"])
async def test_raising_logger_does_not_break_the_tool_call(
    monkeypatch, tool_name
) -> None:
    """A telemetry failure must not change the tool's outcome (#2049)."""
    from app.mcp_server import tool_call_log_middleware as module

    def broken_log(*args, **kwargs):
        raise RuntimeError("telemetry unavailable")

    monkeypatch.setattr(module.logger, "info", broken_log)
    middleware = ToolCallLogMiddleware(profile="default")
    accepted = {"success": True, "order_id": "10001", "status": "accepted"}
    calls = []

    async def call_next(ctx):
        calls.append(ctx.message.name)
        return accepted

    assert await middleware.on_call_tool(_ctx(tool_name), call_next) is accepted
    assert calls == [tool_name]


@pytest.mark.asyncio
async def test_unreadable_request_shape_does_not_break_the_tool_call() -> None:
    middleware = ToolCallLogMiddleware(profile="default")

    class Exploding:
        @property
        def message(self):
            raise RuntimeError("request shape changed")

    async def call_next(ctx):
        return "ok"

    assert await middleware.on_call_tool(Exploding(), call_next) == "ok"


@pytest.mark.asyncio
async def test_missing_tool_name_still_emits_a_line(caplog) -> None:
    middleware = ToolCallLogMiddleware(profile="default")

    async def call_next(ctx):
        return "ok"

    caplog.set_level(logging.INFO)
    await middleware.on_call_tool(SimpleNamespace(message=None), call_next)
    assert _lines(caplog) == [
        (logging.INFO, f"mcp.tool.called tool={UNKNOWN_TOOL_NAME} profile=default")
    ]


@pytest.mark.asyncio
async def test_cancellation_is_not_swallowed_by_the_observation() -> None:
    """``except Exception`` must not absorb cancellation or interpreter exit."""
    from app.mcp_server import tool_call_log_middleware as module

    middleware = ToolCallLogMiddleware(profile="default")

    async def call_next(ctx):
        raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        await middleware.on_call_tool(_ctx(ORDER_TOOL), call_next)

    # And a BaseException raised by the logger itself propagates rather than
    # being quietly dropped alongside ordinary telemetry failures.
    def exiting_log(*args, **kwargs):
        raise KeyboardInterrupt()

    original = module.logger.info
    module.logger.info = exiting_log  # type: ignore[method-assign]
    try:
        with pytest.raises(KeyboardInterrupt):
            await middleware.on_call_tool(_ctx(ORDER_TOOL), call_next)
    finally:
        module.logger.info = original  # type: ignore[method-assign]


@pytest.mark.asyncio
async def test_arguments_and_results_never_reach_the_log(caplog) -> None:
    middleware = ToolCallLogMiddleware(profile="default")
    context = SimpleNamespace(
        message=SimpleNamespace(
            name=ORDER_TOOL,
            arguments={
                "symbol": "005930",
                "quantity": 7,
                "price": "71200.00",
                "account_mode": "kis_live",
            },
        )
    )

    async def call_next(ctx):
        return {"order_id": "10001", "symbol": "005930", "filled_price": "71200.00"}

    caplog.set_level(logging.DEBUG)
    await middleware.on_call_tool(context, call_next)
    # The emitted line carries the name and the profile and nothing else.
    assert _lines(caplog) == [
        (logging.INFO, f"mcp.tool.called tool={ORDER_TOOL} profile=default")
    ]
    for leaked in ("005930", "71200", "order_id", "quantity", "filled_price"):
        assert leaked not in caplog.text, leaked
    assert TOOL_CALL_MESSAGE.split(" ", 1)[0] in caplog.text
