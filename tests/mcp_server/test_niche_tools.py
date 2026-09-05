"""Audited C registration and invocation observation, without broker calls."""

from __future__ import annotations

import asyncio
import inspect
import logging
from pathlib import Path
from typing import Any, cast

import pytest
import sentry_sdk
from fastmcp import FastMCP

from app.core.config import settings
from app.mcp_server.profiles import McpProfile
from app.mcp_server.tooling import registry
from app.mcp_server.tooling.niche import NicheMCP
from tests.mcp_server._registration_recorder import RegistrationRecorder

pytestmark = [pytest.mark.unit]
AUDIT = Path(__file__).resolve().parents[2] / "docs/mcp-tool-usage-audit-20260903.md"


def _expected_niche(profile: str) -> set[str]:
    table = (
        AUDIT.read_text()
        .split("## Complete classification\n", 1)[1]
        .split("\n## ", 1)[0]
    )
    expected = set()
    for line in table.splitlines():
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if len(cells) == 10 and cells[4] == "C" and profile in cells[1].split(", "):
            expected.add(cells[0])
    return expected


def _all_gates(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in type(settings).model_fields:
        if name.lower().endswith("enabled"):
            monkeypatch.setattr(settings, name, True)


@pytest.mark.parametrize("profile", list(McpProfile))
def test_every_actual_registration_has_only_its_audited_niche_group(
    monkeypatch, profile
):
    _all_gates(monkeypatch)
    recorder = RegistrationRecorder()
    registry.register_all_tools(cast(Any, recorder), profile=profile)
    tagged = {
        name
        for name, options in recorder.options.items()
        if "niche" in options.get("tags", set())
    }
    assert tagged == _expected_niche(profile.value), (
        "niche profile/name pairs must match Complete classification"
    )
    for name in tagged:
        assert hasattr(recorder.tools[name], "__wrapped__")
        assert inspect.iscoroutinefunction(recorder.tools[name]), (
            "current C handlers are async"
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("profile", list(McpProfile))
async def test_real_fastmcp_metadata_schema_and_account_defaults_are_preserved(
    monkeypatch, profile
):
    _all_gates(monkeypatch)
    observed = FastMCP("observed", on_duplicate="error")
    registry.register_all_tools(observed, profile=profile)
    with monkeypatch.context() as bare_patch:
        bare_patch.setattr(registry, "NicheMCP", lambda inner, **_kwargs: inner)
        bare = FastMCP("bare", on_duplicate="error")
        registry.register_all_tools(bare, profile=profile)
    actual = {tool.name: tool for tool in await observed.list_tools()}
    original = {tool.name: tool for tool in await bare.list_tools()}
    assert actual.keys() == original.keys()
    for name, tool in actual.items():
        source = original[name]
        for field in (
            "description",
            "parameters",
            "output_schema",
            "annotations",
            "meta",
            "title",
            "timeout",
            "auth",
        ):
            assert getattr(tool, field, None) == getattr(source, field, None), (
                profile,
                name,
                field,
            )
        assert inspect.signature(tool.fn) == inspect.signature(source.fn)
        assert tool.tags == source.tags | (
            {"niche"} if name in _expected_niche(profile.value) else set()
        )


def _warnings(caplog):
    return [
        (record.levelno, record.getMessage(), record.tool)
        for record in caplog.records
        if record.getMessage() == "mcp.niche_tool_called"
    ]


@pytest.mark.asyncio
async def test_actual_fanout_preserves_multi_source_result_and_emits_one_warning(
    monkeypatch, caplog
):
    from app.mcp_server.tooling import buy_candidate_fanout_registration as fanout

    result = {
        "market": "kr",
        "sources": [
            {
                "source": "rsi",
                "candidates": [
                    {"symbol": "005930", "price": "71200.00"},
                    {"symbol": "000660", "price": "185400.00"},
                ],
            },
            {
                "source": "pullback",
                "candidates": [
                    {"symbol": "035420", "price": "241000.00"},
                    {"symbol": "005930", "price": "71200.00"},
                ],
            },
        ],
        "candidates": [
            {
                "symbol": s,
                "current_price": p,
                "data_state": "fresh",
                "as_of": "2026-09-03T07:00:00Z",
            }
            for s, p in [
                ("005930", "71200.00"),
                ("000660", "185400.00"),
                ("035420", "241000.00"),
            ]
        ],
        "observation_only": True,
    }
    calls = []

    async def discover():
        calls.append("discover")
        assert sentry_sdk.get_current_scope()._tags.get("mcp.niche") == "true"
        assert sentry_sdk.get_current_span()._tags["mcp.niche"] == "true"
        return result

    async def record(payload):
        assert payload is result
        calls.append("record")

    monkeypatch.setattr(fanout, "discover_buy_candidates_fanout_impl", discover)
    monkeypatch.setattr(fanout, "maybe_record_fanout_picks", record)
    recorder = RegistrationRecorder()
    registry.register_all_tools(
        cast(Any, recorder), profile=McpProfile.ANALYSIS_READONLY
    )
    caplog.set_level(logging.WARNING)
    with (
        sentry_sdk.new_scope() as parent,
        sentry_sdk.start_span(
            op="mcp.server", name="tools/call discover_buy_candidates_fanout"
        ),
    ):
        parent.set_tag("caller", "fanout-test")
        before = parent._tags.get("mcp.niche")
        assert await recorder.tools["discover_buy_candidates_fanout"]() is result
        assert parent._tags.get("mcp.niche") == before
        assert parent._tags.get("caller") == "fanout-test"
    assert calls == ["discover", "record"]
    assert _warnings(caplog) == [
        (logging.WARNING, "mcp.niche_tool_called", "discover_buy_candidates_fanout")
    ], "niche invocation must emit exactly one warning with the public tool name"
    assert "71200" not in caplog.text and "005930" not in caplog.text


def test_non_c_registration_preserves_callable_identity_and_options():
    inner = RegistrationRecorder()
    proxy = NicheMCP(inner, profile="analysis_readonly")

    def quote(symbol: str = "005930"):
        return {"symbol": symbol}

    assert (
        proxy.tool(name="get_quote", description="quote", tags={"read"})(quote) is quote
    )
    assert inner.tools["get_quote"] is quote
    assert inner.options["get_quote"]["tags"] == {"read"}
    # Same C name on a profile the audit did not assign is untouched.
    assert (
        NicheMCP(inner, profile="account_read").tool(
            name="discover_buy_candidates_fanout"
        )(quote)
        is quote
    )


@pytest.mark.asyncio
async def test_concurrent_scope_exception_cancellation_and_parent_restoration(caplog):
    inner = RegistrationRecorder()
    proxy = NicheMCP(inner, profile="analysis_readonly")
    entered, release = asyncio.Event(), asyncio.Event()
    sentinel = RuntimeError("sentinel")

    @proxy.tool(name="discover_buy_candidates_fanout")
    async def niche(failure=None):
        assert sentry_sdk.get_current_scope()._tags.get("mcp.niche") == "true"
        entered.set()
        await release.wait()
        if failure is not None:
            raise failure
        return ["005930", "000660"]

    @proxy.tool(name="get_quote")
    async def ordinary():
        await entered.wait()
        assert sentry_sdk.get_current_scope()._tags.get("mcp.niche") is None
        release.set()
        return {"symbol": "035420"}

    caplog.set_level(logging.WARNING)
    with sentry_sdk.new_scope() as parent:
        parent.remove_tag("mcp.niche")
        parent.set_tag("caller", "parent")
        values = await asyncio.gather(niche(), ordinary())
        assert values == [["005930", "000660"], {"symbol": "035420"}]
        for failure in (sentinel, asyncio.CancelledError()):
            with pytest.raises(type(failure)) as raised:
                await niche(failure)
            assert raised.value is failure
            assert parent._tags.get("mcp.niche") is None
            assert parent._tags.get("caller") == "parent"
    assert len(_warnings(caplog)) == 3


@pytest.mark.asyncio
async def test_sync_and_sync_awaitable_keep_results_and_warn_once(caplog, monkeypatch):
    inner = RegistrationRecorder()
    proxy = NicheMCP(inner, profile="account_read")
    result = object()

    @proxy.tool("get_order_history")
    def sync(limit: int = 2):
        assert limit == 2
        assert sentry_sdk.get_current_scope()._tags.get("mcp.niche") == "true"
        return result

    caplog.set_level(logging.WARNING)
    assert not inspect.iscoroutinefunction(sync)
    assert sync() is result
    assert inspect.signature(sync).parameters["limit"].default == 2
    second = RegistrationRecorder()

    def returns_awaitable():
        async def finish():
            assert sentry_sdk.get_current_scope()._tags.get("mcp.niche") == "true"
            return result

        return finish()

    registered = NicheMCP(second, profile="account_read").tool(
        returns_awaitable, name="get_order_history"
    )
    assert await registered() is result
    assert len(_warnings(caplog)) == 2
    from app.mcp_server.tooling import niche as module

    def broken_log(*args, **kwargs):
        raise RuntimeError("telemetry unavailable")

    monkeypatch.setattr(module.logger, "warning", broken_log)
    assert sync() is result


@pytest.mark.asyncio
async def test_actual_clean_account_pin_still_rejects_foreign_account(
    monkeypatch, caplog
):
    from app.mcp_server.tooling import alpaca_paper

    _all_gates(monkeypatch)
    recorder = RegistrationRecorder()
    registry.register_all_tools(
        cast(Any, recorder), profile=McpProfile.ALPACA_PAPER_CLEAN
    )
    calls = []

    async def underlying(order_id, account_mode="alpaca_paper"):
        calls.append((order_id, account_mode))
        return {
            "orders": [
                {"id": order_id, "symbol": "BTC/USD"},
                {"id": "second", "symbol": "ETH/USD"},
            ]
        }

    # Patch at registration time, then exercise the actual existing pin wrapper.
    monkeypatch.setattr(alpaca_paper, "alpaca_paper_get_order", underlying)
    recorder = RegistrationRecorder()
    registry.register_all_tools(
        cast(Any, recorder), profile=McpProfile.ALPACA_PAPER_CLEAN
    )
    tool = recorder.tools["alpaca_paper_get_order"]
    assert (
        inspect.signature(tool).parameters["account_mode"].default
        == "alpaca_paper_crypto"
    )
    with pytest.raises(ValueError, match="pinned"):
        await tool("order-1", account_mode="alpaca_paper")
    assert calls == []
    result = await tool("order-1")
    assert len(result["orders"]) == 2
    assert calls == [("order-1", "alpaca_paper_crypto")]


@pytest.mark.asyncio
async def test_actual_account_history_preserves_routing_and_multi_order_envelope(
    monkeypatch, caplog
):
    from app.mcp_server.tooling import orders_history

    result = {
        "success": True,
        "orders": [
            {
                "order_id": "10001",
                "symbol": "005930",
                "side": "buy",
                "quantity": 2,
                "status": "filled",
            },
            {
                "order_id": "10002",
                "symbol": "000660",
                "side": "sell",
                "quantity": 1,
                "status": "cancelled",
            },
        ],
    }
    calls = []

    async def history(**kwargs):
        calls.append(kwargs)
        assert sentry_sdk.get_current_scope()._tags.get("mcp.niche") == "true"
        return result

    monkeypatch.setattr(orders_history, "get_order_history_impl", history)
    recorder = RegistrationRecorder()
    registry.register_all_tools(cast(Any, recorder), profile=McpProfile.ACCOUNT_READ)
    caplog.set_level(logging.WARNING)
    response = await recorder.tools["get_order_history"](
        market="kr", days=3, limit=2, account_mode="kis_live"
    )
    assert response["orders"] == result["orders"]
    assert calls == [
        {
            "symbol": None,
            "status": "all",
            "order_id": None,
            "market": "kr",
            "side": None,
            "days": 3,
            "limit": 2,
            "is_mock": False,
        }
    ]
    assert _warnings(caplog) == [
        (logging.WARNING, "mcp.niche_tool_called", "get_order_history")
    ]
