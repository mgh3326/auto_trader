"""#1171 (operator hk 1135 = A): MCP_PROFILE=h3-crypto-paper contract.

The H3-CRYPTO paper pilot session (auto_trader-operator
``runners/h3_pilot_runner.py --market crypto``) is served by a closed-world
profile. These tests pin:

* the registered set EQUALS the reviewed 20-name allowlist (not a subset), with
  every feature gate on and off;
* every live order / live account tool is absent, by an explicit name list
  that is itself checked to be real (each name registers on another profile);
* the allowlist equals the operator runner's ``registered_tools("crypto")``
  literal, copied below as ``RUNNER_REGISTERED_TOOLS_CRYPTO``;
* ``get_holdings`` on this profile cannot select a live account;
* the boot fails when the registered set drifts from the allowlist;
* deployment is declared only (no deploy script unit).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest

from app.mcp_server.profiles import McpProfile, resolve_mcp_profile
from app.mcp_server.tooling import h3_crypto_paper_registration as h3
from app.mcp_server.tooling import register_all_tools
from app.mcp_server.tooling.route_request_lanes import (
    DIRECT_BROKER_MUTATION_TOOLS,
    MUTATION_TOOLS,
)
from tests.mcp_server._registration_recorder import (
    RegistrationRecorder,
    collect_profile_tools,
)

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
PROFILE = McpProfile.H3_CRYPTO_PAPER

# auto_trader-operator runners/h3_pilot_runner.py registered_tools("crypto")
# = CRYPTO_RESEARCH_TOOLS + ACCOUNT_READ_TOOLS["crypto"]
# + MUTATION_MCP_TOOLS["crypto"] + RECORD_MCP_TOOLS, which the runner
# preflight also requires to equal the operator_contract.yaml registration
# allowed_tools for prompts/h3-managed-envelope-pilot-crypto.md.
RUNNER_REGISTERED_TOOLS_CRYPTO = frozenset(
    {
        "get_operating_briefing",
        "route_request",
        "get_trading_policy",
        "get_quote",
        "get_ohlcv",
        "get_support_resistance",
        "get_indicators",
        "get_momentum_candidates",
        "screen_stocks",
        "screen_stocks_snapshot",
        "analyze_stock",
        "analyze_stock_batch",
        "session_context_get_recent",
        "paper_reconcile_orders",
        "paper_list_pending_orders",
        "get_holdings",
        "paper_place_limit_order",
        "paper_cancel_pending_order",
        "analysis_artifact_save",
        "session_context_append",
    }
)

# Every tool that places, changes, cancels or settles a live (or non-H3
# broker mock) order, or reads a live broker account through server
# credentials, or mutates proposals/watches/policy/settings. None may appear.
LIVE_ORDER_AND_ACCOUNT_TOOLS = (
    # generic account_mode order tools (Upbit/KIS live entry point)
    "place_order",
    "cancel_order",
    "modify_order",
    "get_order_history",
    "live_reconcile_orders",
    # KIS live
    "kis_live_place_order",
    "kis_live_cancel_order",
    "kis_live_modify_order",
    "kis_live_get_order_history",
    "kis_live_reconcile_orders",
    # Toss live
    "toss_preview_order",
    "toss_place_order",
    "toss_cancel_order",
    "toss_modify_order",
    "toss_reconcile_orders",
    "toss_get_order_history",
    "toss_get_positions",
    "toss_get_orderable_cash",
    # other broker order namespaces (not the H3 surface)
    "kis_mock_place_order",
    "kis_mock_cancel_order",
    "kis_mock_modify_order",
    "kiwoom_mock_place_order",
    "kiwoom_mock_cancel_order",
    "kiwoom_mock_modify_order",
    "alpaca_paper_submit_order",
    "alpaca_paper_cancel_order",
    # live account reads
    "get_cash_balance",
    "get_available_capital",
    "get_position",
    # proposal / watch / policy / settings / holdings mutation
    "order_proposal_create",
    "investment_watch_create",
    "investment_watch_void",
    "set_user_setting",
    "update_manual_holdings",
    "decision_table_apply",
    # paper account lifecycle (prompt: never create/reset/delete/kill-switch)
    "create_paper_account",
    "reset_paper_account",
    "delete_paper_account",
    # broad-surface extras
    "session_bootstrap_pack",
    "suggest_order_account",
)

PAPER_MUTATIONS = frozenset(
    {
        "paper_place_limit_order",
        "paper_cancel_pending_order",
        "paper_reconcile_orders",
    }
)
RECORD_WRITES = frozenset({"analysis_artifact_save", "session_context_append"})


def _register(profile: McpProfile) -> RegistrationRecorder:
    recorder = RegistrationRecorder()
    register_all_tools(cast(Any, recorder), profile=profile)
    return recorder


def test_profile_resolves_from_env_value() -> None:
    assert resolve_mcp_profile("h3-crypto-paper") is PROFILE


def test_allowlist_literal_equals_operator_runner_list() -> None:
    assert h3.H3_CRYPTO_PAPER_TOOL_NAMES == RUNNER_REGISTERED_TOOLS_CRYPTO
    assert len(RUNNER_REGISTERED_TOOLS_CRYPTO) == 20


@pytest.mark.parametrize("gates_enabled", [True, False], ids=["gates-on", "gates-off"])
def test_registered_tool_set_equals_allowlist(
    monkeypatch: pytest.MonkeyPatch, gates_enabled: bool
) -> None:
    actual = collect_profile_tools(monkeypatch, gates_enabled=gates_enabled)
    assert set(actual[PROFILE.value]) == RUNNER_REGISTERED_TOOLS_CRYPTO


@pytest.mark.parametrize("gates_enabled", [True, False], ids=["gates-on", "gates-off"])
def test_every_live_order_and_account_tool_is_absent(
    monkeypatch: pytest.MonkeyPatch, gates_enabled: bool
) -> None:
    actual = collect_profile_tools(monkeypatch, gates_enabled=gates_enabled)
    registered = set(actual[PROFILE.value])
    present = sorted(set(LIVE_ORDER_AND_ACCOUNT_TOOLS) & registered)
    assert present == [], f"h3-crypto-paper exposes forbidden tools: {present}"


def test_forbidden_list_names_real_registered_tools(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Non-vacuity: every forbidden name is a real tool some other profile
    # registers (all gates on), so the absence test is not checking typos.
    tools = collect_profile_tools(monkeypatch, gates_enabled=True)
    elsewhere = set().union(
        *(names for profile, names in tools.items() if profile != PROFILE.value)
    )
    unknown = sorted(set(LIVE_ORDER_AND_ACCOUNT_TOOLS) - elsewhere)
    assert unknown == []


def test_profile_is_strict_subset_of_default(monkeypatch: pytest.MonkeyPatch) -> None:
    tools = collect_profile_tools(monkeypatch, gates_enabled=True)
    assert set(tools[PROFILE.value]) < set(tools["default"])


def test_only_paper_simulator_and_record_writes_are_mutations() -> None:
    registered = set(_register(PROFILE).tools)
    assert registered & DIRECT_BROKER_MUTATION_TOOLS <= PAPER_MUTATIONS
    assert registered & MUTATION_TOOLS <= PAPER_MUTATIONS | RECORD_WRITES


def test_registration_drift_fails_the_boot(monkeypatch: pytest.MonkeyPatch) -> None:
    # A listed name that no registrar produces must fail, never shrink silently.
    monkeypatch.setattr(
        h3,
        "H3_CRYPTO_PAPER_TOOL_NAMES",
        h3.H3_CRYPTO_PAPER_TOOL_NAMES | {"not_a_registered_tool"},
    )
    with pytest.raises(h3.H3CryptoPaperProfileError, match="not_a_registered_tool"):
        _register(PROFILE)


def test_foreign_registration_is_dropped() -> None:
    recorder = RegistrationRecorder()
    proxy = h3._H3CryptoPaperMCP(recorder)

    @proxy.tool(name="place_order")
    async def place_order() -> None:  # pragma: no cover - never registered
        return None

    async def kis_live_place_order() -> None:  # pragma: no cover
        return None

    proxy.tool(kis_live_place_order)
    assert recorder.tools == {}
    assert proxy.registered == set()


# ---------------------------------------------------------------------------
# get_holdings is pinned to DB paper accounts on this profile.
# ---------------------------------------------------------------------------


@pytest.fixture
def pinned_get_holdings(monkeypatch: pytest.MonkeyPatch) -> Any:
    from app.mcp_server.tooling import paper_portfolio_handler, portfolio_holdings

    paper_calls: list[Any] = []

    async def fake_collect_paper_positions(**kwargs: Any) -> tuple[list, list]:
        paper_calls.append(kwargs)
        return [], []

    async def broker_trap(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("live broker collector reached from h3-crypto-paper")

    monkeypatch.setattr(
        paper_portfolio_handler, "collect_paper_positions", fake_collect_paper_positions
    )
    for name in (
        "_collect_kis_positions",
        "_collect_upbit_positions",
        "_collect_manual_positions",
        "_collect_toss_api_positions",
        "_collect_whole_portfolio_positions",
    ):
        monkeypatch.setattr(portfolio_holdings, name, broker_trap)
    tool = _register(PROFILE).tools["get_holdings"]
    tool.paper_calls = paper_calls  # type: ignore[attr-defined]
    return tool


@pytest.mark.asyncio
async def test_get_holdings_runner_call_reads_paper_only(
    pinned_get_holdings: Any,
) -> None:
    result = await pinned_get_holdings(
        account="paper:h3-crypto", market="crypto", account_mode="db_simulated"
    )
    assert result["account_mode"] == "db_simulated"
    assert len(pinned_get_holdings.paper_calls) == 1


@pytest.mark.asyncio
async def test_get_holdings_defaults_to_db_simulated(pinned_get_holdings: Any) -> None:
    result = await pinned_get_holdings(account="paper:h3-crypto", market="crypto")
    assert result["account_mode"] == "db_simulated"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kwargs",
    [
        {},
        {"market": "crypto"},
        {"account": "upbit", "market": "crypto"},
        {"account": "kis"},
        {"account": "toss"},
        {"account": "paper:h3-crypto", "account_mode": "kis_live"},
        {"account": "paper:h3-crypto", "account_mode": "kis_mock"},
        {"account": "paper:h3-crypto", "account_type": "paper"},
        {"account": "paper:h3-crypto", "include_ledger_lots": True},
        {"account": "paper:h3-crypto", "fresh_sellable": True},
    ],
)
async def test_get_holdings_refuses_live_selectors(
    pinned_get_holdings: Any, kwargs: dict[str, Any]
) -> None:
    with pytest.raises(ValueError, match="h3-crypto-paper get_holdings"):
        await pinned_get_holdings(**kwargs)
    assert pinned_get_holdings.paper_calls == []


def test_pinned_get_holdings_schema_defaults_account_mode() -> None:
    import inspect

    tool = _register(PROFILE).tools["get_holdings"]
    parameter = inspect.signature(tool).parameters["account_mode"]
    assert parameter.default == "db_simulated"


def test_default_get_holdings_is_not_pinned() -> None:
    import inspect

    tool = _register(McpProfile.DEFAULT).tools["get_holdings"]
    assert inspect.signature(tool).parameters["account_mode"].default is None


# ---------------------------------------------------------------------------
# Deployment is declared, not enabled.
# ---------------------------------------------------------------------------


def test_deploy_script_does_not_run_the_profile() -> None:
    script = (REPO_ROOT / "scripts" / "deploy-ncp-pull.sh").read_text(encoding="utf-8")
    assert "h3-crypto-paper" not in script
    assert "MCP_H3_CRYPTO_PAPER_AUTH_TOKEN" not in script


def test_runbook_declares_the_unit() -> None:
    runbook = (REPO_ROOT / "docs" / "runbooks" / "h3-crypto-paper-mcp.md").read_text(
        encoding="utf-8"
    )
    for fact in (
        "MCP_PROFILE=h3-crypto-paper",
        "at-mcp-h3-crypto-paper",
        "MCP_H3_CRYPTO_PAPER_AUTH_TOKEN",
        "http://100.122.100.56:8776/mcp",
    ):
        assert fact in runbook


# ---------------------------------------------------------------------------
# Real FastMCP server (in-memory client, no network): what a session sees.
# ---------------------------------------------------------------------------


def _real_server() -> Any:
    from fastmcp import FastMCP

    server = FastMCP(name="h3-crypto-paper-test", on_duplicate="error")
    register_all_tools(server, profile=PROFILE)
    return server


@pytest.mark.asyncio
async def test_real_server_offers_exactly_the_allowlist() -> None:
    from fastmcp import Client

    async with Client(_real_server()) as client:
        tools = {tool.name: tool for tool in await client.list_tools()}
    assert set(tools) == RUNNER_REGISTERED_TOOLS_CRYPTO
    schema = tools["get_holdings"].inputSchema["properties"]["account_mode"]
    assert schema.get("default") == "db_simulated"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "denied", ["place_order", "kis_live_place_order", "toss_place_order"]
)
async def test_real_server_refuses_a_raw_live_order_call(denied: str) -> None:
    from fastmcp import Client

    async with Client(_real_server()) as client:
        result = await client.call_tool_mcp(denied, {})
    assert result.isError is True


@pytest.mark.asyncio
async def test_real_server_get_holdings_refuses_a_live_account() -> None:
    from fastmcp import Client

    async with Client(_real_server()) as client:
        result = await client.call_tool_mcp(
            "get_holdings", {"account": "upbit", "market": "crypto"}
        )
    assert result.isError is True
    text = " ".join(getattr(part, "text", "") for part in result.content)
    assert "pinned to DB paper accounts" in text


# ---------------------------------------------------------------------------
# Round 2 (#1171 tester B1/B2): argument pins keep every listed tool off live
# broker credentials — market pinned to crypto, briefing pinned to DB paper.
# ---------------------------------------------------------------------------


class _BrokerTrap:
    """Records any construction/call of a credential-backed broker client."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from app.mcp_server.tooling import portfolio_holdings
        from app.services.brokers.kis import client as kis_client
        from app.services.brokers.upbit import client as upbit_client

        self.hits: list[str] = []

        def trap(label: str) -> Any:
            def raising(*_args: Any, **_kwargs: Any) -> Any:
                self.hits.append(label)
                raise AssertionError(f"credential-backed broker read: {label}")

            return raising

        def async_trap(label: str) -> Any:
            async def raising(*_args: Any, **_kwargs: Any) -> Any:
                self.hits.append(label)
                raise AssertionError(f"credential-backed broker read: {label}")

            return raising

        monkeypatch.setattr(kis_client.KISClient, "__init__", trap("KISClient"))
        monkeypatch.setattr(
            upbit_client, "_request_with_auth", async_trap("upbit_auth")
        )
        monkeypatch.setattr(upbit_client, "fetch_my_coins", async_trap("upbit_coins"))
        for name in (
            "_collect_kis_positions",
            "_collect_upbit_positions",
            "_collect_manual_positions",
            "_collect_toss_api_positions",
            "_collect_whole_portfolio_positions",
        ):
            monkeypatch.setattr(portfolio_holdings, name, async_trap(name))
        from app.services.action_report.snapshot_backed.collectors import registry

        monkeypatch.setattr(
            registry,
            "production_collector_registry",
            trap("pending_orders_collector_registry"),
        )


def test_every_market_taking_tool_is_pinned_or_reviewed_db_only() -> None:
    import inspect

    tools = _register(PROFILE).tools
    takes_market = {
        name
        for name, function in tools.items()
        if "market" in inspect.signature(function).parameters
    }
    pinned = h3.H3_CRYPTO_PAPER_MARKET_PINNED_TOOLS
    unpinned = h3.H3_CRYPTO_PAPER_MARKET_UNPINNED_DB_ONLY
    assert pinned.isdisjoint(unpinned)
    assert takes_market == pinned | unpinned
    for name in pinned:
        assert inspect.signature(tools[name]).parameters["market"].default == "crypto"


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", sorted(h3.H3_CRYPTO_PAPER_MARKET_PINNED_TOOLS))
@pytest.mark.parametrize("market", ["kr", "us", "KR", "Crypto", " crypto", "upbit"])
async def test_pinned_tool_refuses_a_non_crypto_market_before_its_body(
    monkeypatch: pytest.MonkeyPatch, tool: str, market: str
) -> None:
    import inspect

    trap = _BrokerTrap(monkeypatch)
    function = _register(PROFILE).tools[tool]
    kwargs: dict[str, Any] = {"market": market}
    if "symbol" in inspect.signature(function).parameters:
        kwargs["symbol"] = "005930"
    if tool == "analyze_stock_batch":
        kwargs["symbols"] = ["005930", "AAPL"]
    if tool == "get_holdings":
        kwargs["account"] = "paper"
    with pytest.raises(ValueError, match="pinned to market='crypto'"):
        await function(**kwargs)
    assert trap.hits == []


@pytest.mark.parametrize("tool", sorted(h3.H3_CRYPTO_PAPER_MARKET_PINNED_TOOLS))
def test_pinned_tool_passes_crypto_when_market_is_omitted(tool: str) -> None:
    import asyncio
    import inspect

    real = _register(McpProfile.DEFAULT).tools[tool]
    seen: dict[str, Any] = {}

    async def body(*args: Any, **kwargs: Any) -> None:
        seen.update(inspect.signature(real).bind(*args, **kwargs).arguments)

    body.__signature__ = inspect.signature(real)  # type: ignore[attr-defined]
    wrapped = h3._pinned_tool(tool, body)
    kwargs: dict[str, Any] = {}
    for name, parameter in inspect.signature(real).parameters.items():
        if parameter.default is inspect.Parameter.empty and name != "market":
            kwargs[name] = ["KRW-BTC"] if name == "symbols" else "KRW-BTC"
    if tool == "get_holdings":
        kwargs["account"] = "paper:h3-crypto"
    asyncio.run(wrapped(**kwargs))
    assert seen["market"] == "crypto"
    if tool == "get_operating_briefing":
        assert seen["account_scope"] == "db_simulated"


@pytest.mark.asyncio
@pytest.mark.parametrize("scope", ["upbit_live", "kis_live", "kis_mock", "toss"])
async def test_briefing_refuses_a_live_account_scope(
    monkeypatch: pytest.MonkeyPatch, scope: str
) -> None:
    trap = _BrokerTrap(monkeypatch)
    function = _register(PROFILE).tools["get_operating_briefing"]
    with pytest.raises(ValueError, match="account_scope='db_simulated'"):
        await function(market="crypto", account_scope=scope)
    assert trap.hits == []


@pytest.mark.asyncio
async def test_required_crypto_briefing_reads_no_broker_account(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # B1 counterexample: get_operating_briefing(market="crypto") — the exact
    # bootstrap call — reached authenticated Upbit GET /v1/accounts. Every
    # broker read is trapped; DB sections fail open against a dead session.
    from app.mcp_server.tooling import operating_briefing, paper_portfolio_handler

    trap = _BrokerTrap(monkeypatch)
    paper_calls: list[Any] = []

    async def fake_paper_positions(**kwargs: Any) -> tuple[list, list]:
        paper_calls.append(kwargs)
        return [], []

    class _DeadSession:
        async def __aenter__(self) -> Any:
            return self

        async def __aexit__(self, *_exc: Any) -> None:
            return None

        def __getattr__(self, name: str) -> Any:
            raise RuntimeError("db unavailable in this test")

    monkeypatch.setattr(
        paper_portfolio_handler, "collect_paper_positions", fake_paper_positions
    )
    monkeypatch.setattr(operating_briefing, "AsyncSessionLocal", _DeadSession)

    result = await _register(PROFILE).tools["get_operating_briefing"](market="crypto")

    assert trap.hits == []
    assert len(paper_calls) == 1
    assert result["success"] is True
    assert result["account_scope"] == "db_simulated"
    assert result["pending_orders"]["unavailable_reason"] == (
        "db_simulated_scope_uses_paper_list_pending_orders"
    )


@pytest.mark.asyncio
async def test_default_briefing_scope_is_unchanged() -> None:
    from app.mcp_server.tooling.operating_briefing import (
        _default_account_scope,
        _holdings_kwargs,
    )

    assert _default_account_scope("crypto", None) == "upbit_live"
    assert _holdings_kwargs("crypto", "upbit_live", False)["account"] == "upbit"
    assert "account" not in _holdings_kwargs("kr", "kis_live", False)
    assert _holdings_kwargs("crypto", "db_simulated", False)["account"] == "paper"


@pytest.mark.asyncio
async def test_kr_quote_counterexample_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # B2 counterexample: get_quote(symbol="005930", market="kr") built a
    # KISClient. It is refused before the body on this profile.
    trap = _BrokerTrap(monkeypatch)
    with pytest.raises(ValueError, match="pinned to market='crypto'"):
        await _register(PROFILE).tools["get_quote"](symbol="005930", market="kr")
    assert trap.hits == []


@pytest.mark.asyncio
async def test_paper_holdings_valuation_is_crypto_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # B2 counterexample: get_holdings(account="paper", market="kr") valued a
    # KR paper position through KIS. market=kr is refused; an omitted market
    # becomes crypto, so the paper collector is asked for crypto only.
    from app.mcp_server.tooling import paper_portfolio_handler

    trap = _BrokerTrap(monkeypatch)
    calls: list[Any] = []

    async def fake_paper_positions(**kwargs: Any) -> tuple[list, list]:
        calls.append(kwargs)
        return [], []

    monkeypatch.setattr(
        paper_portfolio_handler, "collect_paper_positions", fake_paper_positions
    )
    tool = _register(PROFILE).tools["get_holdings"]
    with pytest.raises(ValueError, match="pinned to market='crypto'"):
        await tool(account="paper", market="kr", include_current_price=True)
    await tool(account="paper", include_current_price=True, minimum_value=0)
    assert trap.hits == []
    assert len(calls) == 1
    assert "crypto" in repr(calls[0])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool", "kwargs"),
    [
        ("get_quote", {"symbol": "005930"}),
        ("get_quote", {"symbol": "AAPL"}),
        ("get_ohlcv", {"symbol": "005930"}),
        ("get_indicators", {"symbol": "AAPL", "indicators": ["rsi"]}),
        ("get_support_resistance", {"symbol": "005930"}),
        ("analyze_stock", {"symbol": "005930"}),
        ("analyze_stock_batch", {"symbols": ["005930", "AAPL"], "quick": False}),
        ("screen_stocks", {}),
        ("get_momentum_candidates", {}),
    ],
)
async def test_crypto_pinned_bodies_never_build_a_kis_client(
    monkeypatch: pytest.MonkeyPatch, tool: str, kwargs: dict[str, Any]
) -> None:
    # Equity symbols with the market omitted run the crypto path (public
    # Upbit / DB; the suite's socket guard blocks the network). Whatever the
    # body returns or raises, no credential-backed broker client is reached.
    trap = _BrokerTrap(monkeypatch)
    try:
        await _register(PROFILE).tools[tool](**kwargs)
    except Exception:  # noqa: BLE001 - public-data failure is fine; hits decide
        pass
    assert trap.hits == []


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", ["get_quote", "get_ohlcv"])
async def test_trap_is_live_on_the_unpinned_default_surface(
    monkeypatch: pytest.MonkeyPatch, tool: str
) -> None:
    # Non-vacuity for the probe above: the same call on DEFAULT reaches KIS.
    trap = _BrokerTrap(monkeypatch)
    try:
        await _register(McpProfile.DEFAULT).tools[tool](symbol="005930")
    except Exception:  # noqa: BLE001 - the tool may or may not surface the trap
        pass
    assert "KISClient" in trap.hits
