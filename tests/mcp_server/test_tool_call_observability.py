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

import pytest
import sentry_sdk
from fastmcp import Client, FastMCP

from app.mcp_server.tool_call_log_middleware import ToolCallLogMiddleware
from app.mcp_server.tooling.niche import NICHE_GROUPS, NicheMCP

pytestmark = [pytest.mark.unit]

PROFILE = "default"
# Audited C tool on the ``default`` profile; the live-order tool the wrapper now
# also sits in front of.
NICHE_ORDER_TOOL = "kis_live_place_order"
# Deliberately not in NICHE_GROUPS for any profile — the newly covered class.
PLAIN_TOOL = "get_quote"

# Byte-identical to app.mcp_server.main.main()'s logging.basicConfig.
DEPLOYED_LOG_FORMAT = "%(asctime)s [%(levelname)s] %(message)s"
DEPLOYED_DATE_FORMAT = "%H:%M:%S"


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
async def test_deployed_format_keeps_the_tool_name_for_niche_and_plain_tools() -> None:
    """Acceptance 1 (local half): the shipped line carries ``tool=<name>``."""
    seen: dict[str, object] = {}
    server = _build_server(seen)

    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(
        logging.Formatter(DEPLOYED_LOG_FORMAT, datefmt=DEPLOYED_DATE_FORMAT)
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
