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
    MAX_FIELD_CHARS,
    TOOL_CALL_MESSAGE,
    UNKNOWN_TOOL_NAME,
    ToolCallLogMiddleware,
    sanitize_log_field,
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


# ──────────────────────────────────────────────────────────────────────
# Log-injection vectors (#329). An unregistered name reaches ``_observe``
# verbatim before dispatch rejects the call, so each case asserts the *exact*
# rendered message: removing the sanitization turns these RED via assertion,
# not via an exception.
# ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("raw_name", "expected_field"),
    [
        # Duplicate key: a space + ``key=`` would forge a second logfmt pair.
        ("x tool=spoofed", "x\\x20tool\\x3dspoofed"),
        ("known_tool profile=admin", "known_tool\\x20profile\\x3dadmin"),
        # Forged record: an embedded newline would print a second fake line.
        (
            "ignored\n08:00:00 [INFO] mcp.tool.called tool=fake",
            "ignored\\n08:00:00\\x20[INFO]\\x20mcp.tool.called\\x20tool\\x3dfake",
        ),
        # Control characters: tab, CR, NUL.
        ("tab\tfield=admin\rCR\x00NUL", "tab\\tfield\\x3dadmin\\rCR\\x00NUL"),
        # Unicode line separator (str.splitlines breaks on it too).
        ("unicode💥\u2028separate", "unicode💥\\u2028separate"),
        # printf-style literals must stay inert text.
        ("percent%s%(name)s", "percent\\x25s\\x25(name)s"),
    ],
)
async def test_untrusted_tool_name_is_escaped(
    caplog, raw_name: str, expected_field: str
) -> None:
    middleware = ToolCallLogMiddleware(profile="default")

    async def call_next(ctx):
        return "ok"

    caplog.set_level(logging.INFO)
    assert await middleware.on_call_tool(_ctx(raw_name), call_next) == "ok"
    # Exactly one record, and the escaped original is still identifiable.
    assert _lines(caplog) == [
        (logging.INFO, f"mcp.tool.called tool={expected_field} profile=default")
    ]


@pytest.mark.asyncio
async def test_tool_name_longer_than_the_cap_is_cut_and_marked(caplog) -> None:
    middleware = ToolCallLogMiddleware(profile="default")
    raw_name = "L" * (MAX_FIELD_CHARS * 3)

    async def call_next(ctx):
        return "ok"

    caplog.set_level(logging.INFO)
    await middleware.on_call_tool(_ctx(raw_name), call_next)
    [(levelno, message)] = _lines(caplog)
    assert levelno == logging.INFO
    assert message == (
        "mcp.tool.called tool="
        + "L" * MAX_FIELD_CHARS
        + f"…(+{MAX_FIELD_CHARS * 2}) profile=default"
    )


@pytest.mark.asyncio
async def test_untrusted_profile_value_is_escaped_too(caplog) -> None:
    """``profile`` is a public-constructor ``Any`` — same injection surface."""
    middleware = ToolCallLogMiddleware(profile="blue\nprofile=admin")

    async def call_next(ctx):
        return "ok"

    caplog.set_level(logging.INFO)
    await middleware.on_call_tool(_ctx("get_quote"), call_next)
    assert _lines(caplog) == [
        (
            logging.INFO,
            "mcp.tool.called tool=get_quote profile=blue\\nprofile\\x3dadmin",
        )
    ]


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("plain_name-1.2", "plain_name-1.2"),
        ("", ""),
        ("a b", "a\\x20b"),
        ("a=b", "a\\x3db"),
        ('a"b', "a\\x22b"),
        ("a\\b", "a\\\\b"),
        ("a\vb\fc\rd", "a\\x0bb\\x0cc\\rd"),
        ("a\u2028b\u2029c", "a\\u2028b\\u2029c"),
        ("a\u0085b\x1b[31mc", "a\\x85b\\x1b[31mc"),
        ("a\u00a0b\u2003c", "a\\xa0b\\u2003c"),
        ("한글 도구", "한글\\x20도구"),
        (None, "None"),
        (123, "123"),
    ],
)
def test_sanitize_log_field_escapes_structural_characters(
    raw: object, expected: str
) -> None:
    assert sanitize_log_field(raw) == expected


def test_sanitize_log_field_never_raises_for_unstringable_value() -> None:
    class Exploding:
        def __str__(self) -> str:
            raise RuntimeError("no repr")

    assert sanitize_log_field(Exploding()) == "unreadable"
