"""End-to-end proof that a tool call leaves a name-carrying line in the deployed
log format, for every tool, without disturbing the audited ``mcp.niche`` signal.

Unlike ``tests/test_mcp_tool_call_log_middleware.py`` (which drives the middleware
directly), these tests go through a real ``FastMCP`` server, the real middleware
chain, a real in-memory ``Client`` call, and the exact root formatter configured
in ``app.mcp_server.main`` — so a line that passes here is the line Loki receives.
"""

from __future__ import annotations

import io
import logging
import re
from typing import Any

import pytest
import sentry_sdk
from fastmcp import Client, FastMCP

from app.mcp_server.tool_call_log_middleware import (
    MAX_FIELD_CHARS,
    ToolCallLogMiddleware,
)
from app.mcp_server.tooling.niche import NICHE_GROUPS, NicheMCP
from tests.test_mcp_server_main import _load_main_module

pytestmark = [pytest.mark.unit]

PROFILE = "default"
# Audited C tool on the ``default`` profile; the live-order tool the wrapper now
# also sits in front of.
NICHE_ORDER_TOOL = "kis_live_place_order"
# Deliberately not in NICHE_GROUPS for any profile — the newly covered class.
PLAIN_TOOL = "get_quote"


def _deployed_log_config(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Drive the real ``app.mcp_server.main.main()`` and capture the kwargs it
    actually passes to ``logging.basicConfig`` — the deployed log format, not a
    copy of it. ``mcp.run`` is already a MagicMock on the loaded module, so this
    exercises the real call site without starting a server.
    """
    monkeypatch.setenv("MCP_TYPE", "streamable-http")
    module, _mcp, _, _, _ = _load_main_module(monkeypatch)
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(logging, "basicConfig", lambda **kwargs: calls.append(kwargs))
    module.main()
    # The sys.modules stubs from _load_main_module (fake fastmcp etc.) must go
    # before the caller builds a real FastMCP server and in-process Client.
    monkeypatch.undo()
    # calls[0] is main()'s own basicConfig; configure_dependency_log_levels may
    # append a second empty call.
    assert calls, "main() must call logging.basicConfig"
    deployed = calls[0]
    # Pin the default level too: the mcp.tool.called line is INFO, so a deployed
    # default above INFO would silence it in the container.
    assert deployed["level"] == logging.INFO
    return deployed


def _niche_names(profile: str) -> frozenset[str]:
    return frozenset(
        name
        for profiles, names in NICHE_GROUPS
        if profile in profiles
        for name in names
    )


def test_fixture_tool_names_match_their_audited_class() -> None:
    """Guard the premise: these tests are worthless if the classes ever swap."""
    names = _niche_names(PROFILE)
    assert NICHE_ORDER_TOOL in names
    assert PLAIN_TOOL not in names


def _build_server(seen: dict[str, object]) -> FastMCP:
    server = FastMCP("observability", on_duplicate="error")
    server.add_middleware(ToolCallLogMiddleware(profile=PROFILE))
    proxy = NicheMCP(server, profile=PROFILE)

    @proxy.tool(name=NICHE_ORDER_TOOL)
    async def place_order(symbol: str = "005930", quantity: int = 7) -> dict:
        seen[NICHE_ORDER_TOOL] = sentry_sdk.get_current_scope()._tags.get("mcp.niche")
        return {"success": True, "order_id": "10001", "status": "accepted"}

    @proxy.tool(name=PLAIN_TOOL)
    async def quote(symbol: str = "005930") -> dict:
        seen[PLAIN_TOOL] = sentry_sdk.get_current_scope()._tags.get("mcp.niche")
        return {"symbol": symbol, "price": "71200.00"}

    return server


@pytest.mark.asyncio
async def test_deployed_format_keeps_the_tool_name_for_niche_and_plain_tools(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Acceptance 1 (local half): the shipped line carries ``tool=<name>``."""
    deployed = _deployed_log_config(monkeypatch)
    seen: dict[str, object] = {}
    server = _build_server(seen)

    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(
        logging.Formatter(deployed["format"], datefmt=deployed["datefmt"])
    )
    root = logging.getLogger()
    root.addHandler(handler)
    previous_level = root.level
    root.setLevel(logging.INFO)
    try:
        async with Client(server) as client:
            order = await client.call_tool(NICHE_ORDER_TOOL, {"symbol": "005930"})
            plain = await client.call_tool(PLAIN_TOOL, {"symbol": "005930"})
    finally:
        root.removeHandler(handler)
        root.setLevel(previous_level)

    assert order.data["status"] == "accepted"
    assert plain.data["symbol"] == "005930"

    rendered = stream.getvalue()
    emitted = [line for line in rendered.splitlines() if "mcp.tool.called" in line]
    pattern = re.compile(
        r"^\d{2}:\d{2}:\d{2} \[INFO\] mcp\.tool\.called tool=(\S+) profile=default$"
    )
    matched = [pattern.match(line) for line in emitted]
    assert all(matched), emitted
    # Both classes of tool are covered — the point of widening past "niche".
    assert [m.group(1) for m in matched if m] == [NICHE_ORDER_TOOL, PLAIN_TOOL]

    # The nameless line the widening was meant to replace is still nameless, which
    # is exactly why the new line had to exist.
    assert "mcp.niche_tool_called" in rendered
    assert not re.search(r"mcp\.niche_tool_called.*\bkis_live_place_order\b", rendered)

    # Arguments and results never appear in any rendered line.
    for leaked in ("71200", "order_id", "quantity", "success"):
        assert leaked not in rendered, leaked


@pytest.mark.asyncio
async def test_mcp_niche_sentry_tag_still_marks_only_the_audited_group() -> None:
    """Acceptance 3: widening the log did not widen the ``mcp.niche`` tag."""
    seen: dict[str, object] = {}
    server = _build_server(seen)

    with sentry_sdk.new_scope() as parent:
        parent.remove_tag("mcp.niche")
        parent.set_tag("caller", "observability-test")
        async with Client(server) as client:
            await client.call_tool(NICHE_ORDER_TOOL, {"symbol": "005930"})
            await client.call_tool(PLAIN_TOOL, {"symbol": "005930"})
        assert parent._tags.get("mcp.niche") is None
        assert parent._tags.get("caller") == "observability-test"

    assert seen == {NICHE_ORDER_TOOL: "true", PLAIN_TOOL: None}


@pytest.mark.asyncio
async def test_raising_logger_still_lets_a_live_order_tool_return(monkeypatch) -> None:
    """Acceptance 2 through the real chain: #2049 must not be reintroduced."""
    from app.mcp_server import tool_call_log_middleware as module

    seen: dict[str, object] = {}
    server = _build_server(seen)

    def broken_log(*args, **kwargs):
        raise RuntimeError("telemetry unavailable")

    monkeypatch.setattr(module.logger, "info", broken_log)

    async with Client(server) as client:
        order = await client.call_tool(NICHE_ORDER_TOOL, {"symbol": "005930"})

    assert order.data == {"success": True, "order_id": "10001", "status": "accepted"}
    assert seen[NICHE_ORDER_TOOL] == "true"


# ──────────────────────────────────────────────────────────────────────
# #329 log-injection vectors through the real FastMCP Client path. An
# unregistered ``name`` reaches ``_observe`` verbatim (dispatch rejects it
# afterwards with ``is_error=True``), so each case must still produce exactly
# one physical record line whose logfmt keys cannot be forged. Every check is
# an assertion on the rendered output — deleting the sanitization turns each
# case RED via ``AssertionError``, not via an exception.
# ──────────────────────────────────────────────────────────────────────

# A deployed-format record: ``HH:MM:SS [INFO] mcp.tool.called tool=<tok>
# profile=<tok>``. Anchored to line start so forged *content* inside a value
# (which keeps the literal ``mcp.tool.called`` substring after escaping) cannot
# inflate the count — only a real second line can.
RECORD_LINE = re.compile(
    r"^\d{2}:\d{2}:\d{2} \[INFO\] mcp\.tool\.called tool=\S+ profile=\S+$"
)

_INJECTION_VECTORS = [
    # (raw tool name, escaped fragment that must survive, raw chars that must
    #  never reach the rendered stream)
    pytest.param(
        "x tool=spoofed",
        "x\\x20tool\\x3dspoofed",
        (
            " tool=spoofed",
            " spoofed ",
        ),
        id="duplicate_key",
    ),
    pytest.param(
        "ignored\n08:00:00 [INFO] mcp.tool.called tool=fake",
        "ignored\\n08:00:00\\x20[INFO]\\x20mcp.tool.called\\x20tool\\x3dfake",
        (),
        id="forged_record_line",
    ),
    pytest.param(
        "tab\tfield=admin\rCR\x00NUL",
        "tab\\tfield\\x3dadmin\\rCR\\x00NUL",
        ("\t", "\r", "\x00"),
        id="control_characters",
    ),
    pytest.param(
        "unicode💥\u2028separate",
        "unicode💥\\u2028separate",
        ("\u2028", "\u2029"),
        id="unicode_line_separators",
    ),
    pytest.param(
        "percent%s%(name)s",
        "percent\\x25s\\x25(name)s",
        ("%",),
        id="printf_literals",
    ),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("raw_name", "escaped_fragment", "forbidden"), _INJECTION_VECTORS
)
async def test_untrusted_tool_name_cannot_forge_log_records(
    monkeypatch: pytest.MonkeyPatch,
    raw_name: str,
    escaped_fragment: str,
    forbidden: tuple[str, ...],
) -> None:
    deployed = _deployed_log_config(monkeypatch)
    seen: dict[str, object] = {}
    server = _build_server(seen)

    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(
        logging.Formatter(deployed["format"], datefmt=deployed["datefmt"])
    )
    root = logging.getLogger()
    root.addHandler(handler)
    previous_level = root.level
    root.setLevel(deployed["level"])
    try:
        async with Client(server) as client:
            result = await client.call_tool(raw_name, {}, raise_on_error=False)
    finally:
        root.removeHandler(handler)
        root.setLevel(previous_level)

    # The hostile name never dispatched, but the observation line was written
    # before dispatch — that is the surface under test.
    assert result.is_error

    rendered = stream.getvalue()
    records = [line for line in rendered.splitlines() if RECORD_LINE.match(line)]
    # One physical record per call: no embedded newline or Unicode separator
    # may have manufactured a second line.
    assert len(records) == 1, rendered
    (line,) = records
    # logfmt keys are unforgeable: exactly one tool= and one profile= pair.
    assert line.count("tool=") == 1
    assert line.count("profile=") == 1
    # The original value stays identifiable through its escapes.
    assert escaped_fragment in line
    for bad in forbidden:
        assert bad not in rendered, repr(bad)


@pytest.mark.asyncio
async def test_oversized_tool_name_is_bounded_and_marked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    deployed = _deployed_log_config(monkeypatch)
    seen: dict[str, object] = {}
    server = _build_server(seen)

    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(
        logging.Formatter(deployed["format"], datefmt=deployed["datefmt"])
    )
    root = logging.getLogger()
    root.addHandler(handler)
    previous_level = root.level
    root.setLevel(deployed["level"])
    try:
        async with Client(server) as client:
            result = await client.call_tool("L" * 20000, {}, raise_on_error=False)
    finally:
        root.removeHandler(handler)
        root.setLevel(previous_level)

    assert result.is_error

    rendered = stream.getvalue()
    records = [line for line in rendered.splitlines() if RECORD_LINE.match(line)]
    assert len(records) == 1, rendered
    (line,) = records
    # MAX_FIELD_CHARS source chars, escaped, plus the truncation marker —
    # the value is cut, not dropped, and the line stays bounded.
    assert len(line) < 256
    assert f"…(+{20000 - MAX_FIELD_CHARS})" in line
