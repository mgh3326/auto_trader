"""#1257 (#1245): MCP_PROFILE=h3-us-paper contract.

The H3-US paper pilot session (auto_trader-operator
``runners/h3_pilot_runner.py --market us``) is served by a closed-world
profile, the US twin of #1171 h3-crypto-paper. These tests pin:

* the registered set EQUALS the reviewed 20-name allowlist (not a subset), with
  every feature gate on and off;
* every live order / live account tool is absent, by an explicit name list
  that is itself checked to be real (each name registers on another profile);
* the allowlist equals the operator runner's ``registered_tools("us")``
  literal, copied below as ``RUNNER_REGISTERED_TOOLS_US``;
* every listed tool is pinned to market us / account_mode alpaca_paper /
  asset_class us_equity / briefing account_scope db_simulated, refusing any
  other value before its body runs;
* every listed tool body runs inside the #1257 credential firewall: a KIS,
  Toss or authenticated Upbit client reached from it refuses before any
  token lookup, breaker lease or send;
* the boot fails when the registered set drifts from the allowlist;
* the deploy script runs the unit with exactly this profile, port 8777 and
  token env name, behind a tailnet-only HAProxy frontend.
"""

from __future__ import annotations

import inspect
import re
from pathlib import Path
from typing import Any, cast

import pytest

from app.mcp_server.profiles import McpProfile, resolve_mcp_profile
from app.mcp_server.tooling import h3_us_paper_registration as h3
from app.mcp_server.tooling import register_all_tools
from app.mcp_server.tooling.route_request_lanes import (
    DIRECT_BROKER_MUTATION_TOOLS,
    MUTATION_TOOLS,
)
from app.services.brokers import credential_firewall
from tests.mcp_server._registration_recorder import (
    RegistrationRecorder,
    collect_profile_tools,
)

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
PROFILE = McpProfile.H3_US_PAPER

# auto_trader-operator runners/h3_pilot_runner.py registered_tools("us")
# = RESEARCH_TOOLS + ACCOUNT_READ_TOOLS["us"] + MUTATION_MCP_TOOLS["us"]
# + RECORD_MCP_TOOLS (operator main d3be3e6).
RUNNER_REGISTERED_TOOLS_US = frozenset(
    {
        "get_operating_briefing",
        "route_request",
        "get_trading_policy",
        "get_quote",
        "get_ohlcv",
        "get_top_stocks",
        "screen_stocks",
        "screen_stocks_snapshot",
        "discover_buy_candidates_fanout",
        "analyze_stock",
        "analyze_stock_batch",
        "session_context_get_recent",
        "alpaca_paper_list_orders",
        "alpaca_paper_get_order",
        "alpaca_paper_list_positions",
        "market_quote_snapshot_ensure",
        "alpaca_paper_submit_order",
        "alpaca_paper_cancel_order",
        "analysis_artifact_save",
        "session_context_append",
    }
)

# Every tool that places, changes, cancels or settles a live (or non-H3
# broker mock / other paper) order, or reads a live broker account through
# server credentials, or mutates proposals/watches/policy/settings. None may
# appear. The runner's FOREIGN_PROBES["us"] are included verbatim.
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
    # other broker order namespaces (not the H3-US surface)
    "kis_mock_place_order",
    "kis_mock_cancel_order",
    "kis_mock_modify_order",
    "kiwoom_mock_place_order",
    "kiwoom_mock_cancel_order",
    "kiwoom_mock_modify_order",
    "kiwoom_mock_us_place_order",
    "kiwoom_mock_us_cancel_order",
    "kiwoom_mock_us_modify_order",
    "paper_place_limit_order",
    "paper_cancel_pending_order",
    # other Alpaca paper surfaces (automated path, preview, reconcile writer,
    # other-account reads) — the runner probes the first two as foreign
    "alpaca_paper_automated_submit_order",
    "alpaca_paper_automated_preview_order",
    "alpaca_paper_preview_order",
    "alpaca_paper_reconcile_orders",
    "alpaca_paper_get_account",
    "alpaca_paper_get_cash",
    "us_dual_paper_preview",
    # live account reads
    "get_holdings",
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
    # paper account lifecycle
    "create_paper_account",
    "reset_paper_account",
    "delete_paper_account",
    # broad-surface extras
    "session_bootstrap_pack",
    "suggest_order_account",
    "screen_stocks_enrich",
)

ALPACA_MUTATIONS = frozenset({"alpaca_paper_submit_order", "alpaca_paper_cancel_order"})
RECORD_WRITES = frozenset(
    {"analysis_artifact_save", "session_context_append", "market_quote_snapshot_ensure"}
)


def _register(profile: McpProfile) -> RegistrationRecorder:
    recorder = RegistrationRecorder()
    register_all_tools(cast(Any, recorder), profile=profile)
    return recorder


def test_profile_resolves_from_env_value() -> None:
    assert resolve_mcp_profile("h3-us-paper") is PROFILE


def test_allowlist_literal_equals_operator_runner_list() -> None:
    assert h3.H3_US_PAPER_TOOL_NAMES == RUNNER_REGISTERED_TOOLS_US
    assert len(RUNNER_REGISTERED_TOOLS_US) == 20


@pytest.mark.parametrize("gates_enabled", [True, False], ids=["gates-on", "gates-off"])
def test_registered_tool_set_equals_allowlist(
    monkeypatch: pytest.MonkeyPatch, gates_enabled: bool
) -> None:
    actual = collect_profile_tools(monkeypatch, gates_enabled=gates_enabled)
    assert set(actual[PROFILE.value]) == RUNNER_REGISTERED_TOOLS_US


@pytest.mark.parametrize("gates_enabled", [True, False], ids=["gates-on", "gates-off"])
def test_every_live_order_and_account_tool_is_absent(
    monkeypatch: pytest.MonkeyPatch, gates_enabled: bool
) -> None:
    actual = collect_profile_tools(monkeypatch, gates_enabled=gates_enabled)
    registered = set(actual[PROFILE.value])
    present = sorted(set(LIVE_ORDER_AND_ACCOUNT_TOOLS) & registered)
    assert present == [], f"h3-us-paper exposes forbidden tools: {present}"


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


def test_profile_is_strict_subset_of_us_paper(monkeypatch: pytest.MonkeyPatch) -> None:
    # Nothing on this profile is new: it is a narrowing of the existing
    # Alpaca paper profile, which already serves the same paper account.
    tools = collect_profile_tools(monkeypatch, gates_enabled=True)
    assert set(tools[PROFILE.value]) < set(tools[McpProfile.US_PAPER.value])


def test_only_alpaca_paper_orders_and_record_writes_are_mutations() -> None:
    registered = set(_register(PROFILE).tools)
    assert registered & DIRECT_BROKER_MUTATION_TOOLS <= ALPACA_MUTATIONS
    assert registered & MUTATION_TOOLS <= ALPACA_MUTATIONS | RECORD_WRITES


def test_registration_drift_fails_the_boot(monkeypatch: pytest.MonkeyPatch) -> None:
    # A listed name that no registrar produces must fail, never shrink silently.
    monkeypatch.setattr(
        h3,
        "H3_US_PAPER_TOOL_NAMES",
        h3.H3_US_PAPER_TOOL_NAMES | {"not_a_registered_tool"},
    )
    with pytest.raises(h3.H3UsPaperProfileError, match="not_a_registered_tool"):
        _register(PROFILE)


def test_foreign_registration_is_dropped() -> None:
    recorder = RegistrationRecorder()
    proxy = h3._H3UsPaperMCP(recorder)

    @proxy.tool(name="place_order")
    async def place_order() -> None:  # pragma: no cover - never registered
        return None

    async def alpaca_paper_automated_submit_order() -> None:  # pragma: no cover
        return None

    proxy.tool(alpaca_paper_automated_submit_order)
    assert recorder.tools == {}
    assert proxy.registered == set()


@pytest.mark.asyncio
async def test_other_profiles_do_not_change_when_this_one_registers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The pins and the firewall wrap only this profile's registrations: the
    # same tools on us-paper keep their defaults, accept the lab account and
    # run outside the firewall.
    from app.mcp_server.tooling import alpaca_paper

    _register(PROFILE)
    other = _register(McpProfile.US_PAPER).tools
    assert inspect.signature(other["get_quote"]).parameters["market"].default is None
    seen: list[Any] = []

    class _FakeService:
        async def list_positions(self) -> list[Any]:
            seen.append(credential_firewall.broker_credentials_blocked_by())
            return []

    monkeypatch.setattr(alpaca_paper, "_service_factory", _FakeService)
    result = await other["alpaca_paper_list_positions"](account_mode="alpaca_paper_lab")
    assert result["account_mode"] == "alpaca_paper_lab"
    assert seen == [None]


# ---------------------------------------------------------------------------
# Argument pins: market us, account_mode alpaca_paper, asset_class us_equity,
# briefing account_scope db_simulated.
# ---------------------------------------------------------------------------


class _BrokerTrap:
    """Records any construction/use of a credential-backed broker client.

    Each trap sits where a credential is used (KIS client construction and
    token lookup, Toss client construction and token, Upbit auth, the
    live/manual holdings collectors, the broker pending-order collector), so
    the trap fires only if the credential firewall and the pins both failed.
    """

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from app.mcp_server.tooling import portfolio_holdings
        from app.services.brokers.kis import base as kis_base
        from app.services.brokers.toss import auth as toss_auth
        from app.services.brokers.upbit import client as upbit_client

        self.hits: list[str] = []

        def async_trap(label: str) -> Any:
            async def raising(*_args: Any, **_kwargs: Any) -> Any:
                self.hits.append(label)
                raise AssertionError(f"credential-backed broker read: {label}")

            return raising

        def trap(label: str) -> Any:
            def raising(*_args: Any, **_kwargs: Any) -> Any:
                self.hits.append(label)
                raise AssertionError(f"credential-backed broker read: {label}")

            return raising

        monkeypatch.setattr(
            kis_base.redis_token_manager, "get_token", async_trap("kis_token")
        )
        monkeypatch.setattr(
            kis_base.BaseKISClient, "_fetch_token", async_trap("kis_token_fetch")
        )
        monkeypatch.setattr(
            kis_base, "get_kis_circuit_breaker", trap("kis_request_dispatch")
        )
        monkeypatch.setattr(
            toss_auth.TossOAuthTokenManager,
            "get_access_token",
            async_trap("toss_token"),
        )
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


class _FirewallSpy:
    """Records every refusal the credential firewall raises."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.refusals: list[str] = []
        original = credential_firewall.BrokerCredentialsBlocked.__init__
        spy = self

        def recording(self: Any, *args: Any, **kwargs: Any) -> None:
            spy.refusals.append(str(args[0]) if args else "")
            original(self, *args, **kwargs)

        monkeypatch.setattr(
            credential_firewall.BrokerCredentialsBlocked, "__init__", recording
        )


def test_every_market_taking_tool_is_pinned_or_reviewed_db_only() -> None:
    tools = _register(PROFILE).tools
    takes_market = {
        name
        for name, function in tools.items()
        if "market" in inspect.signature(function).parameters
    }
    pinned = h3.H3_US_PAPER_MARKET_PINNED_TOOLS
    unpinned = h3.H3_US_PAPER_MARKET_UNPINNED_DB_ONLY
    assert pinned.isdisjoint(unpinned)
    assert takes_market == pinned | unpinned


def test_every_account_mode_taking_tool_is_pinned() -> None:
    tools = _register(PROFILE).tools
    takes_account_mode = {
        name
        for name, function in tools.items()
        if "account_mode" in inspect.signature(function).parameters
    }
    assert takes_account_mode == h3.H3_US_PAPER_ACCOUNT_PINNED_TOOLS
    for name in takes_account_mode:
        parameter = inspect.signature(tools[name]).parameters["account_mode"]
        assert parameter.default == "alpaca_paper", name


def test_pinned_schema_defaults_show_the_pins() -> None:
    tools = _register(PROFILE).tools
    for name in h3.H3_US_PAPER_MARKET_PINNED_TOOLS - {"market_quote_snapshot_ensure"}:
        assert inspect.signature(tools[name]).parameters["market"].default == "us"
    # market_quote_snapshot_ensure(market, symbol): market stays required in
    # the schema (a default cannot precede the required symbol) and is still
    # pinned at call time.
    snapshot = inspect.signature(tools["market_quote_snapshot_ensure"])
    assert snapshot.parameters["market"].default is inspect.Parameter.empty
    submit = inspect.signature(tools["alpaca_paper_submit_order"])
    assert submit.parameters["asset_class"].default == "us_equity"
    briefing = inspect.signature(tools["get_operating_briefing"])
    assert briefing.parameters["account_scope"].default == "db_simulated"


def _required_kwargs(function: Any) -> dict[str, Any]:
    kwargs: dict[str, Any] = {}
    for name, parameter in inspect.signature(function).parameters.items():
        if parameter.default is not inspect.Parameter.empty:
            continue
        if name == "symbols":
            kwargs[name] = ["AAPL"]
        elif name in {"symbol", "order_id", "side", "type"}:
            kwargs[name] = {"side": "buy", "type": "limit"}.get(name, "AAPL")
        elif name != "market":
            kwargs[name] = "x"
    return kwargs


async def _refusal(call: Any) -> str:
    """The pin refusal message; anything else (a body run) is an assertion."""
    try:
        await call
    except ValueError as exc:
        return str(exc)
    except Exception as exc:  # noqa: BLE001 - the body ran: the pin failed
        raise AssertionError(f"the tool body ran instead of the pin: {exc!r}") from exc
    raise AssertionError("the call was not refused by the pin")


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", sorted(h3.H3_US_PAPER_MARKET_PINNED_TOOLS))
@pytest.mark.parametrize("market", ["kr", "crypto", "US", " us", "nyse", "upbit"])
async def test_pinned_tool_refuses_a_non_us_market_before_its_body(
    monkeypatch: pytest.MonkeyPatch, tool: str, market: str
) -> None:
    trap = _BrokerTrap(monkeypatch)
    function = _register(PROFILE).tools[tool]
    kwargs = _required_kwargs(function)
    kwargs["market"] = market
    assert "pinned to market='us'" in await _refusal(function(**kwargs))
    assert trap.hits == []


@pytest.mark.parametrize("tool", sorted(h3.H3_US_PAPER_MARKET_PINNED_TOOLS))
def test_pinned_tool_passes_us_when_market_is_omitted(tool: str) -> None:
    import asyncio

    real = _register(McpProfile.US_PAPER).tools[tool]
    seen: dict[str, Any] = {}

    async def body(*args: Any, **kwargs: Any) -> None:
        seen.update(inspect.signature(real).bind(*args, **kwargs).arguments)
        seen["firewall"] = credential_firewall.broker_credentials_blocked_by()

    body.__signature__ = inspect.signature(real)  # type: ignore[attr-defined]
    wrapped = h3._pinned_tool(tool, body)
    asyncio.run(wrapped(**_required_kwargs(real)))
    assert seen["market"] == "us"
    assert seen["firewall"] == "h3-us-paper"
    if tool == "get_operating_briefing":
        assert seen["account_scope"] == "db_simulated"


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", sorted(h3.H3_US_PAPER_ACCOUNT_PINNED_TOOLS))
@pytest.mark.parametrize(
    "account_mode", ["alpaca_paper_lab", "alpaca_paper_crypto", "ALPACA_PAPER", "x"]
)
async def test_alpaca_tool_refuses_another_account_before_its_body(
    monkeypatch: pytest.MonkeyPatch, tool: str, account_mode: str
) -> None:
    from app.mcp_server.tooling import alpaca_paper, alpaca_paper_orders

    def no_service() -> Any:
        raise AssertionError("alpaca paper service built for a refused account")

    monkeypatch.setattr(alpaca_paper, "_service_factory", no_service)
    monkeypatch.setattr(alpaca_paper_orders, "_service_factory", no_service)
    function = _register(PROFILE).tools[tool]
    kwargs = _required_kwargs(function)
    kwargs["account_mode"] = account_mode
    message = await _refusal(function(**kwargs))
    assert "pinned to account_mode='alpaca_paper'" in message


@pytest.mark.asyncio
async def test_alpaca_read_with_omitted_account_mode_reads_alpaca_paper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.mcp_server.tooling import alpaca_paper

    calls: list[Any] = []

    class _FakeService:
        async def list_orders(self, **kwargs: Any) -> list[Any]:
            calls.append((kwargs, credential_firewall.broker_credentials_blocked_by()))
            return []

    monkeypatch.setattr(alpaca_paper, "_service_factory", _FakeService)
    result = await _register(PROFILE).tools["alpaca_paper_list_orders"](
        status="open", limit=500
    )
    assert result["account_mode"] == "alpaca_paper"
    # Alpaca paper is the H3-US account itself: it runs inside the firewall
    # (which never blocks it) and reaches only the paper service.
    assert calls == [({"status": "open", "limit": 500}, "h3-us-paper")]


@pytest.mark.asyncio
@pytest.mark.parametrize("asset_class", ["crypto", "us_option", "US_EQUITY"])
async def test_submit_refuses_a_non_equity_asset_class(
    monkeypatch: pytest.MonkeyPatch, asset_class: str
) -> None:
    from app.mcp_server.tooling import alpaca_paper_orders

    def no_service() -> Any:
        raise AssertionError("alpaca paper service built for a refused order")

    monkeypatch.setattr(alpaca_paper_orders, "_service_factory", no_service)
    with pytest.raises(ValueError, match="pinned to asset_class='us_equity'"):
        await _register(PROFILE).tools["alpaca_paper_submit_order"](
            symbol="AAPL",
            side="buy",
            type="limit",
            qty=1,
            limit_price=1,
            time_in_force="day",
            asset_class=asset_class,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "scope", ["kis_live", "toss", "kis_mock", "alpaca_paper", "upbit_live"]
)
async def test_briefing_refuses_a_live_account_scope(
    monkeypatch: pytest.MonkeyPatch, scope: str
) -> None:
    trap = _BrokerTrap(monkeypatch)
    function = _register(PROFILE).tools["get_operating_briefing"]
    message = await _refusal(function(market="us", account_scope=scope))
    assert "account_scope='db_simulated'" in message
    assert trap.hits == []


@pytest.mark.asyncio
async def test_required_us_briefing_reads_no_broker_account(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The bootstrap call get_operating_briefing(market="us") would default to
    # account_scope kis_live (KIS live holdings + KIS overseas pending orders).
    # Every broker read is trapped; DB sections fail open against a dead
    # session; the paper collector is faked.
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

    result = await _register(PROFILE).tools["get_operating_briefing"](market="us")

    assert trap.hits == []
    assert len(paper_calls) == 1
    assert result["success"] is True
    assert result["account_scope"] == "db_simulated"
    assert result["pending_orders"]["unavailable_reason"] == (
        "db_simulated_scope_uses_paper_list_pending_orders"
    )


# ---------------------------------------------------------------------------
# Credential firewall: the US market-data bodies would use the live KIS app
# key (quotes, US daily candle fill) and Toss (daily candle fallback, USD/KRW).
# On this profile they refuse before any credential use.
# ---------------------------------------------------------------------------


def test_every_listed_tool_runs_inside_the_firewall() -> None:
    tools = _register(PROFILE).tools
    assert set(tools) == RUNNER_REGISTERED_TOOLS_US
    for name, function in tools.items():
        assert hasattr(function, "__wrapped__"), name


@pytest.fixture
def us_exchange(monkeypatch: pytest.MonkeyPatch) -> None:
    # Resolve the exchange without the DB universe so the KIS leg is entered.
    from app.mcp_server.tooling import market_data_quotes

    async def nasdaq(_symbol: str) -> str:
        return "NAS"

    monkeypatch.setattr(market_data_quotes, "get_us_exchange_by_symbol", nasdaq)


@pytest.mark.asyncio
async def test_us_quote_refuses_kis_and_falls_back(
    monkeypatch: pytest.MonkeyPatch, us_exchange: None
) -> None:
    from app.core.config import settings

    monkeypatch.setattr(settings, "us_quote_kis_primary", True)
    trap = _BrokerTrap(monkeypatch)
    spy = _FirewallSpy(monkeypatch)
    try:
        await _register(PROFILE).tools["get_quote"](symbol="SPY")
    except Exception:  # noqa: BLE001 - Yahoo is blocked by the socket guard
        pass
    assert trap.hits == []
    assert any("KIS client" in refusal for refusal in spy.refusals), spy.refusals


@pytest.mark.asyncio
async def test_trap_is_live_for_the_same_quote_on_us_paper(
    monkeypatch: pytest.MonkeyPatch, us_exchange: None
) -> None:
    # Non-vacuity: the identical call on us-paper (no firewall) reaches the
    # KIS token lookup.
    from app.core.config import settings

    monkeypatch.setattr(settings, "us_quote_kis_primary", True)
    trap = _BrokerTrap(monkeypatch)
    try:
        await _register(McpProfile.US_PAPER).tools["get_quote"](
            symbol="SPY", market="us"
        )
    except Exception:  # noqa: BLE001
        pass
    assert "kis_token" in trap.hits


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool", "kwargs"),
    [
        ("get_quote", {"symbol": "SPY"}),
        ("get_quote", {"symbol": "AAPL", "market": "us"}),
        ("get_ohlcv", {"symbol": "SPY", "period": "day", "count": 30}),
        ("get_top_stocks", {}),
        ("screen_stocks", {}),
        ("screen_stocks_snapshot", {}),
        ("analyze_stock", {"symbol": "AAPL"}),
        ("analyze_stock_batch", {"symbols": ["AAPL", "SPY"], "quick": False}),
        ("analyze_stock_batch", {"symbols": ["AAPL"]}),
        ("discover_buy_candidates_fanout", {}),
        ("market_quote_snapshot_ensure", {"market": "us", "symbol": "SPY"}),
    ],
)
async def test_us_bodies_never_use_a_broker_credential(
    monkeypatch: pytest.MonkeyPatch,
    us_exchange: None,
    tool: str,
    kwargs: dict[str, Any],
) -> None:
    # Whatever the body returns or raises (public data is blocked by the
    # suite's socket guard), no credential-backed broker path is reached.
    from app.core.config import settings

    monkeypatch.setattr(settings, "us_quote_kis_primary", True)
    _enable_fake_toss(monkeypatch)
    trap = _BrokerTrap(monkeypatch)
    try:
        await _register(PROFILE).tools[tool](**kwargs)
    except Exception:  # noqa: BLE001 - public-data failure is fine; hits decide
        pass
    assert trap.hits == []


def _enable_fake_toss(monkeypatch: pytest.MonkeyPatch) -> None:
    # Toss enabled with fake credentials, so the Toss leg is really entered.
    from app.core.config import settings

    monkeypatch.setattr(settings, "toss_api_enabled", True, raising=False)
    monkeypatch.setattr(settings, "toss_api_client_id", "fake-id", raising=False)
    monkeypatch.setattr(
        settings, "toss_api_client_secret", "fake-secret", raising=False
    )


@pytest.mark.asyncio
async def test_us_daily_candles_refuse_the_toss_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # get_ohlcv(day) reads Yahoo and falls back to Toss on failure; on this
    # profile the Toss client refuses, so the tool returns no Toss candles.
    from app.mcp_server.tooling import market_data_quotes

    async def yahoo_down(**_kwargs: Any) -> Any:
        raise RuntimeError("yahoo unavailable")

    monkeypatch.setattr(market_data_quotes.yahoo_service, "fetch_ohlcv", yahoo_down)
    _enable_fake_toss(monkeypatch)
    trap = _BrokerTrap(monkeypatch)
    spy = _FirewallSpy(monkeypatch)
    try:
        result: Any = await _register(PROFILE).tools["get_ohlcv"](
            symbol="SPY", period="day", count=30
        )
    except Exception as exc:  # noqa: BLE001 - the refusal may propagate
        result = exc
    assert not (isinstance(result, dict) and result.get("source") == "toss")
    assert trap.hits == []
    assert any("Toss client" in refusal for refusal in spy.refusals), spy.refusals


@pytest.mark.asyncio
async def test_toss_fallback_never_unwraps_the_toss_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # #1257 r1 B1 (tester reproduction): Yahoo down -> Toss daily fallback.
    # The Toss client secret must not even be unwrapped on this profile.
    from pydantic import SecretStr

    from app.core.config import settings
    from app.mcp_server.tooling import market_data_quotes

    unwrapped: list[str] = []

    class _SpySecret(SecretStr):
        def get_secret_value(self) -> str:
            unwrapped.append("toss_secret")
            return super().get_secret_value()

    async def yahoo_down(**_kwargs: Any) -> Any:
        raise RuntimeError("yahoo unavailable")

    monkeypatch.setattr(market_data_quotes.yahoo_service, "fetch_ohlcv", yahoo_down)
    _enable_fake_toss(monkeypatch)
    monkeypatch.setattr(
        settings, "toss_api_client_secret", _SpySecret("fake-secret"), raising=False
    )
    trap = _BrokerTrap(monkeypatch)
    try:
        await _register(PROFILE).tools["get_ohlcv"](symbol="SPY", period="day", count=3)
    except Exception:  # noqa: BLE001 - the refusal may propagate
        pass
    assert unwrapped == []
    assert trap.hits == []


# ---------------------------------------------------------------------------
# Real FastMCP server (in-memory client, no network): what a session sees.
# ---------------------------------------------------------------------------


def _real_server() -> Any:
    from fastmcp import FastMCP

    server = FastMCP(name="h3-us-paper-test", on_duplicate="error")
    register_all_tools(server, profile=PROFILE)
    return server


@pytest.mark.asyncio
async def test_real_server_offers_exactly_the_allowlist() -> None:
    from fastmcp import Client

    async with Client(_real_server()) as client:
        tools = {tool.name: tool for tool in await client.list_tools()}
    assert set(tools) == RUNNER_REGISTERED_TOOLS_US
    schema = tools["alpaca_paper_submit_order"].inputSchema["properties"]
    assert schema["account_mode"].get("default") == "alpaca_paper"
    assert schema["asset_class"].get("default") == "us_equity"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "denied",
    [
        "place_order",
        "kis_live_place_order",
        "toss_place_order",
        "alpaca_paper_automated_submit_order",
        "alpaca_paper_preview_order",
        "kis_mock_place_order",
        "kiwoom_mock_us_place_order",
    ],
)
async def test_real_server_refuses_a_foreign_order_call(denied: str) -> None:
    from fastmcp import Client

    async with Client(_real_server()) as client:
        result = await client.call_tool_mcp(denied, {})
    assert result.isError is True


@pytest.mark.asyncio
async def test_real_server_refuses_the_lab_account() -> None:
    from fastmcp import Client

    async with Client(_real_server()) as client:
        result = await client.call_tool_mcp(
            "alpaca_paper_list_positions", {"account_mode": "alpaca_paper_lab"}
        )
    assert result.isError is True
    text = " ".join(getattr(part, "text", "") for part in result.content)
    assert "pinned to account_mode='alpaca_paper'" in text


# ---------------------------------------------------------------------------
# Deployment through the deploy script (#1189 wiring, one port up).
# ---------------------------------------------------------------------------

DEPLOY_SCRIPT = REPO_ROOT / "scripts" / "deploy-ncp-pull.sh"
HAPROXY_TEMPLATE = REPO_ROOT / "ops" / "ncp" / "haproxy" / "haproxy.cfg.tmpl"


def _deploy_array(name: str) -> list[str]:
    match = re.search(
        rf"^declare -a {name}=\((.*)\)$",
        DEPLOY_SCRIPT.read_text(encoding="utf-8"),
        re.MULTILINE,
    )
    assert match, name
    return match.group(1).split()


def test_deploy_script_runs_the_profile_with_exact_port_and_token() -> None:
    names = _deploy_array("MCP_NAMES")
    profiles = _deploy_array("MCP_PROFILES")
    ports = _deploy_array("MCP_PORTS")
    tokens = _deploy_array("MCP_TOKENS")
    assert len(names) == len(profiles) == len(ports) == len(tokens)
    assert names.count("h3-us-paper") == 1
    i = names.index("h3-us-paper")
    assert profiles[i] == PROFILE.value == "h3-us-paper"
    assert ports[i] == "8777"
    assert tokens[i] == "MCP_H3_US_PAPER_AUTH_TOKEN"
    assert profiles.count("h3-us-paper") == 1
    assert ports.count("8777") == 1
    assert tokens.count("MCP_H3_US_PAPER_AUTH_TOKEN") == 1
    assert "at-mcp-h3-us-paper" in _deploy_array("APP_CONTAINERS")
    assert "h3-us-paper" in _deploy_array("MCP_LIVE_ROUTE_NAMES")


def test_haproxy_exposes_the_unit_on_the_tailnet_only() -> None:
    text = HAPROXY_TEMPLATE.read_text(encoding="utf-8")
    binds = re.findall(r"^\s*bind (\S+)\s*$", text, re.MULTILINE)
    assert [b for b in binds if b.endswith(":8777")] == ["100.122.100.56:8777"]
    assert "server mcp_h3_us_paper 127.0.0.1:8777 check" in text


def test_runbook_declares_the_unit() -> None:
    runbook = (REPO_ROOT / "docs" / "runbooks" / "h3-us-paper-mcp.md").read_text(
        encoding="utf-8"
    )
    for fact in (
        "MCP_PROFILE=h3-us-paper",
        "at-mcp-h3-us-paper",
        "MCP_H3_US_PAPER_AUTH_TOKEN",
        "http://100.122.100.56:8777/mcp",
    ):
        assert fact in runbook
