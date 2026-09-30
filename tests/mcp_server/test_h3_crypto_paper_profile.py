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
