# ROB-649 `route_request` advisory lane router — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a read-only advisory MCP tool `route_request(intent, market)` that deterministically maps an intent enum to the standard tool sequence, advisory allow/block lists, policy thresholds + version stamp, and hard constraints for that lane.

**Architecture:** One pure data/logic module (`route_request_lanes.py`, no MCP dependency, fully unit-testable) + one thin MCP glue module (`route_request_registration.py`). Lane definitions are a static dict ported from the ROB-643 playbook and kept in sync by a drift test. A set-equality registry-diff test forces every DEFAULT-profile tool into exactly one of two disjoint buckets (read-only vs mutation). Registered on every profile (advisory, no order surface).

**Tech Stack:** Python 3.13, FastMCP 3.2.0 (`mcp.list_tools()` returns objects with `.name`), pytest. Reuses `get_policy_for` / `policy_version_stamp` (ROB-646) and the exported order-tool-name sets.

## Global Constraints

- migration 0 — no DB models, no alembic revision.
- `route_request` is **advisory only** — no enforcement/blocking. Enforcement middleware is an explicit out-of-scope follow-up.
- Registered on **every** profile (always-registered block in `registry.py`).
- `intent ∈ {buy_analysis, profit_taking, discovery, market_brief}`; `market ∈ {kr, us, crypto}` (required).
- Deterministic: output is a pure function of `(intent, market)` + current `config/trading_policy.yaml` + registered tool surface. No clocks, no randomness. All list outputs sorted.
- No magic numbers in `hard_constraints` — reference policy keys by name (e.g. `sell.loss_guard_min_multiple`), never the value.
- Do not add new readers of the raw playbook policy numbers; thresholds come only via `get_policy_for` (ROB-646).
- Follow existing tooling patterns: module exports a `*_TOOL_NAMES` set + `register_*` function; tests use `tests._mcp_tooling_support.DummyMCP`.

## File Structure

- Create `app/mcp_server/tooling/route_request_lanes.py` — `INTENT_TO_LANE`, `LANE_SEQUENCES`, `LANE_TO_POLICY_LANE`, `HARD_CONSTRAINTS`, `VALID_MARKETS`, `READ_ONLY_ADVISORY_TOOLS`, `MUTATION_TOOLS`, `ALL_KNOWN_TOOLS`, `lane_tool_names()`, `build_route_plan()`.
- Create `app/mcp_server/tooling/route_request_registration.py` — `ROUTE_REQUEST_TOOL_NAMES`, `route_request()` async fn, `register_route_request_tools()`.
- Modify `app/mcp_server/tooling/registry.py` — import + call `register_route_request_tools(mcp)` in the always-registered block.
- Modify `tests/_mcp_tooling_support.py` — add `DummyMCP.list_tools()` so the profile-intersection path is testable.
- Create `tests/test_route_request_lanes.py` — pure builder unit tests.
- Create `tests/test_route_request.py` — registration + async tool tests (policy echo, errors, determinism, profile intersection).
- Create `tests/test_route_request_registry_diff.py` — set-equality partition + lane-vs-playbook drift.
- Modify `app/mcp_server/README.md` — divergence note.

---

### Task 1: Pure lane data + `build_route_plan`

**Files:**
- Create: `app/mcp_server/tooling/route_request_lanes.py`
- Test: `tests/test_route_request_lanes.py`

**Interfaces:**
- Consumes: exported order-tool-name sets from `orders_registration`, `orders_kis_variants`, `orders_toss_variants`, `orders_kiwoom_variants`.
- Produces:
  - `INTENT_TO_LANE: dict[str, str]`
  - `VALID_MARKETS: frozenset[str]`
  - `LANE_SEQUENCES: dict[str, list[dict]]` (each item `{"step": int, "tool": str, "purpose": str}`)
  - `LANE_TO_POLICY_LANE: dict[str, str | None]`
  - `HARD_CONSTRAINTS: dict[str, list[str]]`
  - `READ_ONLY_ADVISORY_TOOLS: frozenset[str]`, `MUTATION_TOOLS: frozenset[str]`, `ALL_KNOWN_TOOLS: frozenset[str]`
  - `lane_tool_names(lane: str) -> set[str]`
  - `build_route_plan(intent, market, *, registered_tools, verdict_thresholds, policy_version) -> dict[str, Any]`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_route_request_lanes.py
from __future__ import annotations

from app.mcp_server.tooling import route_request_lanes as L


def _fake_thresholds(market: str, lane: str, *, empty: bool = False) -> dict:
    return {
        "market": market,
        "lane": lane,
        "version": "V",
        "content_hash": "H",
        "thresholds": {} if empty else {"screen.rsi_max": {"value": 45}},
    }


_VERSION = {"version": "V", "content_hash": "H"}
_ALL = set(L.ALL_KNOWN_TOOLS)


def test_intent_to_lane_covers_all_four_intents():
    assert set(L.INTENT_TO_LANE) == {
        "buy_analysis",
        "profit_taking",
        "discovery",
        "market_brief",
    }
    assert L.INTENT_TO_LANE["market_brief"] == "bootstrap"


def test_buckets_partition_and_are_disjoint():
    assert L.READ_ONLY_ADVISORY_TOOLS.isdisjoint(L.MUTATION_TOOLS)
    assert L.ALL_KNOWN_TOOLS == L.READ_ONLY_ADVISORY_TOOLS | L.MUTATION_TOOLS
    assert "route_request" in L.READ_ONLY_ADVISORY_TOOLS


def test_build_route_plan_buy_shape_is_deterministic():
    plan_a = L.build_route_plan(
        "buy_analysis", "kr",
        registered_tools=_ALL,
        verdict_thresholds=_fake_thresholds("kr", "buy"),
        policy_version=_VERSION,
    )
    plan_b = L.build_route_plan(
        "buy_analysis", "kr",
        registered_tools=_ALL,
        verdict_thresholds=_fake_thresholds("kr", "buy"),
        policy_version=_VERSION,
    )
    assert plan_a == plan_b
    assert plan_a["success"] is True
    assert plan_a["lane"] == "buy"
    assert plan_a["market"] == "kr"
    assert plan_a["policy_version"] == _VERSION
    steps = [s["tool"] for s in plan_a["standard_tool_sequence"]]
    assert steps[0] == "get_operating_briefing"
    assert "toss_place_order" in steps
    # step numbers are contiguous 1..n
    assert [s["step"] for s in plan_a["standard_tool_sequence"]] == list(
        range(1, len(steps) + 1)
    )


def test_blocked_actions_excludes_lanes_own_mutation_tools():
    plan = L.build_route_plan(
        "buy_analysis", "kr",
        registered_tools=_ALL,
        verdict_thresholds=_fake_thresholds("kr", "buy"),
        policy_version=_VERSION,
    )
    # buy lane's own place tools are allowed, not blocked
    assert "toss_place_order" not in plan["blocked_actions"]
    assert "kis_live_place_order" not in plan["blocked_actions"]
    # a non-buy mutation tool is blocked
    assert "toss_cancel_order" in plan["blocked_actions"]
    assert "toss_place_order" in plan["allowed_tools"]


def test_market_brief_blocks_all_mutation_and_has_empty_thresholds():
    plan = L.build_route_plan(
        "market_brief", "kr",
        registered_tools=_ALL,
        verdict_thresholds=_fake_thresholds("kr", "bootstrap", empty=True),
        policy_version=_VERSION,
    )
    assert plan["lane"] == "bootstrap"
    assert plan["verdict_thresholds"]["thresholds"] == {}
    # bootstrap sequence is all read-only -> every mutation tool is blocked
    assert set(plan["blocked_actions"]) == (L.MUTATION_TOOLS & _ALL)


def test_profile_intersection_drops_unregistered_tools():
    registered = _ALL - {"toss_place_order"}
    plan = L.build_route_plan(
        "discovery", "kr",
        registered_tools=registered,
        verdict_thresholds=_fake_thresholds("kr", "discovery"),
        policy_version=_VERSION,
    )
    tools = [s["tool"] for s in plan["standard_tool_sequence"]]
    assert "toss_place_order" not in tools
    assert "toss_place_order" not in plan["allowed_tools"]
    assert "toss_place_order" not in plan["blocked_actions"]


def test_hard_constraints_reference_policy_keys_not_numbers():
    plan = L.build_route_plan(
        "buy_analysis", "kr",
        registered_tools=_ALL,
        verdict_thresholds=_fake_thresholds("kr", "buy"),
        policy_version=_VERSION,
    )
    joined = " ".join(plan["hard_constraints"])
    assert "sell.loss_guard_min_multiple" in joined
    assert "1.01" not in joined
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_route_request_lanes.py -q`
Expected: FAIL — `ModuleNotFoundError: app.mcp_server.tooling.route_request_lanes`.

- [ ] **Step 3: Write the module**

```python
# app/mcp_server/tooling/route_request_lanes.py
"""Static lane definitions + pure route-plan builder for route_request (ROB-649).

No MCP dependency — fully unit-testable. Lane definitions are ported from the
machine-readable ``lanes:`` blocks of docs/playbooks/trading-decision-playbook.md
(ROB-643, the definition source) and kept in sync by
tests/test_route_request_registry_diff.py. Thresholds are NOT stored here — they
come from get_trading_policy (ROB-646); hard_constraints reference policy KEYS,
never values.
"""

from __future__ import annotations

from typing import Any

from app.mcp_server.tooling.orders_kis_variants import (
    KIS_LIVE_ORDER_TOOL_NAMES,
    KIS_MOCK_ORDER_TOOL_NAMES,
    LIVE_RECONCILE_TOOL_NAMES,
)
from app.mcp_server.tooling.orders_kiwoom_variants import KIWOOM_MOCK_TOOL_NAMES
from app.mcp_server.tooling.orders_registration import ORDER_TOOL_NAMES
from app.mcp_server.tooling.orders_toss_variants import TOSS_LIVE_ORDER_TOOL_NAMES

# intent enum (the only free LLM choice) -> playbook lane
INTENT_TO_LANE: dict[str, str] = {
    "buy_analysis": "buy",
    "profit_taking": "sell",
    "discovery": "discovery",
    "market_brief": "bootstrap",
}

VALID_MARKETS: frozenset[str] = frozenset({"kr", "us", "crypto"})

# playbook lane -> get_trading_policy lane (bootstrap has no policy thresholds)
LANE_TO_POLICY_LANE: dict[str, str | None] = {
    "buy": "buy",
    "sell": "sell",
    "discovery": "discovery",
    "bootstrap": None,
}

# Ordered standard tool sequence per lane, ported from the playbook lanes: blocks.
LANE_SEQUENCES: dict[str, list[dict[str, Any]]] = {
    "bootstrap": [
        {"tool": "get_operating_briefing", "purpose": "holdings, pending orders, latest report, session_context, analysis_artifacts"},
        {"tool": "session_context_get_recent", "purpose": "yesterday's decision journal"},
        {"tool": "analysis_artifact_list", "purpose": "reusable prior analysis (metadata)"},
        {"tool": "analysis_artifact_get", "purpose": "on-demand body fetch for a specific artifact"},
        {"tool": "get_market_index", "purpose": "market regime"},
        {"tool": "get_fx_rate", "purpose": "FX"},
    ],
    "buy": [
        {"tool": "get_operating_briefing", "purpose": "load prior-session decisions + positions"},
        {"tool": "get_market_index", "purpose": "market regime"},
        {"tool": "get_fx_rate", "purpose": "FX"},
        {"tool": "analyze_stock_batch", "purpose": "RSI, honest consensus, support/resistance, per-account position (mode=quick, include_position, <=10)"},
        {"tool": "get_intraday_investor_flow", "purpose": "foreign-flow gate (recovery_gate)"},
        {"tool": "toss_place_order", "purpose": "execute buy — Toss preferred (fee-free); deep limit, no chasing"},
        {"tool": "kis_live_place_order", "purpose": "spend down KIS deposit; dry_run preview -> live"},
    ],
    "sell": [
        {"tool": "toss_get_positions", "purpose": "scan in-the-money / near-breakeven names"},
        {"tool": "analyze_stock_batch", "purpose": "confirm distance to resistance, RSI, upside"},
        {"tool": "toss_place_order", "purpose": "sell-into-strength split ladder just under resistance"},
        {"tool": "sell_ladder_fill_preview", "purpose": "ROB-477 bottom-anchor rung, fill-safety"},
    ],
    "discovery": [
        {"tool": "screen_stocks_snapshot", "purpose": "multi-source fan-out candidate pool"},
        {"tool": "get_top_stocks", "purpose": "losers fan-out"},
        {"tool": "get_momentum_candidates", "purpose": "momentum fan-out"},
        {"tool": "screen_stocks", "purpose": "value/RSI screen fan-out"},
        {"tool": "get_sector_peers", "purpose": "rotation-sector peers"},
        {"tool": "get_disclosures", "purpose": "rights-issue / overhang filter"},
        {"tool": "analyze_stock_batch", "purpose": "deep confirm on ranked survivors"},
        {"tool": "toss_place_order", "purpose": "winners only, support-line limit"},
    ],
}

# Per-lane hard-constraint summaries. Reference policy KEYS, never values.
HARD_CONSTRAINTS: dict[str, list[str]] = {
    "buy": [
        "recovery gate: deploy reserve only when >= recovery_gate.min_conditions_met of 4 conditions",
        "loss guard (sell-side): sell price >= avg * sell.loss_guard_min_multiple",
        "KRX tick rounding",
        "DAY order expiry at order.day_expiry_kst -> re-place next day",
        "no two-sided (buy+sell) resting orders on same Toss symbol",
        "over-concentration cap: portfolio.sector_cluster_cap_pct per sector cluster",
        "portfolio.max_symbols_per_theme per theme; add-not-cut (average down, no stop-loss)",
    ],
    "sell": [
        "loss guard: sell price >= avg * sell.loss_guard_min_multiple",
        "KRX tick rounding",
        "no two-sided (buy+sell) resting orders on same Toss symbol",
        "DAY order expiry at order.day_expiry_kst -> re-place next day",
        "preserve core lot; trim over-concentrated sectors first (portfolio.sector_cluster_cap_pct)",
    ],
    "discovery": [
        "over-concentration cap: portfolio.sector_cluster_cap_pct per sector cluster",
        "portfolio.max_symbols_per_theme per theme",
        "rights-issue / overhang filter before ranking",
        "per-symbol sizing: buy.per_symbol_notional_krw_range",
    ],
    "bootstrap": [
        "context-load only; no order mutation in this lane",
        "recovery gate frame: recovery_gate.min_conditions_met of 4",
        "account routing: buys prefer Toss (fee-free); KIS deposit spent down in-account",
    ],
}

MUTATION_TOOLS: frozenset[str] = frozenset(
    ORDER_TOOL_NAMES
    | KIS_LIVE_ORDER_TOOL_NAMES
    | KIS_MOCK_ORDER_TOOL_NAMES
    | LIVE_RECONCILE_TOOL_NAMES
    | TOSS_LIVE_ORDER_TOOL_NAMES
    | KIWOOM_MOCK_TOOL_NAMES
)

# Every non-mutation tool in the DEFAULT profile (computed 2026-07-02 as
# DEFAULT-profile tools minus MUTATION_TOOLS) plus route_request itself. The
# set-equality partition test (test_route_request_registry_diff.py) fails if a
# new DEFAULT tool is not classified here or in MUTATION_TOOLS — this is the
# drift guard the issue requires.
READ_ONLY_ADVISORY_TOOLS: frozenset[str] = frozenset(
    {
        "route_request",
        "analysis_artifact_get",
        "analysis_artifact_list",
        "analysis_artifact_save",
        "analyze_portfolio",
        "analyze_stock",
        "analyze_stock_batch",
        "forecast_resolve",
        "forecast_save",
        "get_analyst_consensus",
        "get_available_capital",
        "get_cash_balance",
        "get_company_profile",
        "get_correlation",
        "get_cost_basis_distribution",
        "get_crypto_catalysts",
        "get_crypto_fear_greed",
        "get_crypto_funding_rate",
        "get_crypto_long_short_ratio",
        "get_crypto_market_regime",
        "get_crypto_open_interest",
        "get_crypto_order_flow",
        "get_crypto_profile",
        "get_crypto_social",
        "get_crypto_top_movers",
        "get_disclosures",
        "get_dividends",
        "get_earnings_calendar",
        "get_execution_strength",
        "get_financials",
        "get_forecast_calibration",
        "get_forecasts",
        "get_fx_rate",
        "get_holdings",
        "get_holdings_news",
        "get_indicators",
        "get_insider_transactions",
        "get_intraday_investor_flow",
        "get_investment_opinions",
        "get_investor_trends",
        "get_kimchi_premium",
        "get_latest_market_brief",
        "get_market_index",
        "get_market_issues",
        "get_market_news",
        "get_market_reports",
        "get_mock_loop_retrospective",
        "get_momentum_candidates",
        "get_news",
        "get_ohlcv",
        "get_operating_briefing",
        "get_orderbook",
        "get_portfolio_allocation",
        "get_position",
        "get_quote",
        "get_retail_sentiment",
        "get_retrospective_aggregate",
        "get_sector_peers",
        "get_short_interest",
        "get_support_resistance",
        "get_top_stocks",
        "get_toss_ai_signal",
        "get_toss_buy_balance",
        "get_trade_journal",
        "get_trade_retrospectives",
        "get_trading_policy",
        "get_upbit_altseason",
        "get_upbit_index",
        "get_user_setting",
        "get_valuation",
        "investment_report_activate_watch",
        "investment_report_add_items",
        "investment_report_context_get",
        "investment_report_create",
        "investment_report_decide_item",
        "investment_report_delta_get",
        "investment_report_get",
        "investment_report_list",
        "investment_report_set_status",
        "investment_report_update",
        "investment_watch_recommend",
        "list_active_journals",
        "list_active_watches",
        "modify_journal_entry",
        "research_session_get",
        "research_session_list_recent",
        "research_summary_get",
        "save_trade_journal",
        "save_trade_retrospective",
        "screen_stocks",
        "screen_stocks_snapshot",
        "search_symbol",
        "session_context_append",
        "session_context_get_recent",
        "set_user_setting",
        "stage_analysis_get",
        "suggest_order_account",
        "trade_retrospective_pending",
        "update_manual_holdings",
        "update_trade_journal",
    }
)

ALL_KNOWN_TOOLS: frozenset[str] = READ_ONLY_ADVISORY_TOOLS | MUTATION_TOOLS


def lane_tool_names(lane: str) -> set[str]:
    return {step["tool"] for step in LANE_SEQUENCES[lane]}


def build_route_plan(
    intent: str,
    market: str,
    *,
    registered_tools: set[str],
    verdict_thresholds: dict[str, Any],
    policy_version: dict[str, str],
) -> dict[str, Any]:
    """Assemble the deterministic route plan. Pure — no IO. Caller validates
    intent/market and resolves policy before calling."""
    lane = INTENT_TO_LANE[intent]
    seq_steps = [
        step for step in LANE_SEQUENCES[lane] if step["tool"] in registered_tools
    ]
    standard_tool_sequence = [
        {"step": i, "tool": step["tool"], "purpose": step["purpose"]}
        for i, step in enumerate(seq_steps, start=1)
    ]
    lane_own_mutation = lane_tool_names(lane) & MUTATION_TOOLS
    allowed = (
        (lane_tool_names(lane) | set(READ_ONLY_ADVISORY_TOOLS)) & registered_tools
    )
    blocked = (MUTATION_TOOLS - lane_own_mutation) & registered_tools
    return {
        "success": True,
        "intent": intent,
        "lane": lane,
        "market": market,
        "standard_tool_sequence": standard_tool_sequence,
        "allowed_tools": sorted(allowed),
        "blocked_actions": sorted(blocked),
        "verdict_thresholds": verdict_thresholds,
        "policy_version": policy_version,
        "hard_constraints": list(HARD_CONSTRAINTS[lane]),
    }


__all__ = [
    "INTENT_TO_LANE",
    "VALID_MARKETS",
    "LANE_TO_POLICY_LANE",
    "LANE_SEQUENCES",
    "HARD_CONSTRAINTS",
    "MUTATION_TOOLS",
    "READ_ONLY_ADVISORY_TOOLS",
    "ALL_KNOWN_TOOLS",
    "lane_tool_names",
    "build_route_plan",
]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_route_request_lanes.py -q`
Expected: PASS (7 tests).

- [ ] **Step 5: Commit**

```bash
git add app/mcp_server/tooling/route_request_lanes.py tests/test_route_request_lanes.py
git commit -m "feat(ROB-649): route_request lane data + pure build_route_plan"
```

---

### Task 2: `route_request` async tool + registration

**Files:**
- Modify: `tests/_mcp_tooling_support.py` (add `DummyMCP.list_tools`)
- Create: `app/mcp_server/tooling/route_request_registration.py`
- Test: `tests/test_route_request.py`

**Interfaces:**
- Consumes: `route_request_lanes.build_route_plan`, `INTENT_TO_LANE`, `VALID_MARKETS`, `LANE_TO_POLICY_LANE`, `ALL_KNOWN_TOOLS`; `get_policy_for` + `policy_version_stamp` from `app.services.trading_policy_service`.
- Produces:
  - `ROUTE_REQUEST_TOOL_NAMES: set[str]` = `{"route_request"}`
  - `register_route_request_tools(mcp) -> None`
  - the registered async `route_request(intent, market)` callable (captured in `DummyMCP.tools["route_request"]`).

- [ ] **Step 1: Add `list_tools` to DummyMCP**

In `tests/_mcp_tooling_support.py`, inside `class DummyMCP`, add (after the `tool` method), and add `from types import SimpleNamespace` to the imports at the top if not present (it is already imported):

```python
    def list_tools(self):
        """Mirror FastMCP.list_tools() — objects exposing a ``.name``."""
        return [SimpleNamespace(name=name) for name in self.tools]
```

- [ ] **Step 2: Write the failing test**

```python
# tests/test_route_request.py
from __future__ import annotations

import asyncio
from typing import Any, cast

from app.mcp_server.profiles import McpProfile
from app.mcp_server.tooling.route_request_registration import (
    ROUTE_REQUEST_TOOL_NAMES,
    register_route_request_tools,
)
from tests._mcp_tooling_support import DummyMCP, build_tools


def _route_tool() -> Any:
    mcp = DummyMCP()
    register_route_request_tools(cast(Any, mcp))
    return mcp.tools["route_request"]


def test_tool_name_registered():
    mcp = DummyMCP()
    register_route_request_tools(cast(Any, mcp))
    assert ROUTE_REQUEST_TOOL_NAMES == {"route_request"}
    assert "route_request" in mcp.tools


def test_unknown_intent_returns_error():
    route = _route_tool()
    out = asyncio.run(route(intent="sell_everything", market="kr"))
    assert out["success"] is False
    assert out["error"] == "unknown_intent"


def test_unknown_market_returns_error():
    route = _route_tool()
    out = asyncio.run(route(intent="buy_analysis", market="jp"))
    assert out["success"] is False
    assert out["error"] == "unknown_market"


def test_buy_analysis_echoes_policy_version_and_thresholds():
    route = _route_tool()
    out = asyncio.run(route(intent="buy_analysis", market="kr"))
    assert out["success"] is True
    assert out["lane"] == "buy"
    assert set(out["policy_version"]) == {"version", "content_hash"}
    # buy lane has policy thresholds
    assert out["verdict_thresholds"]["thresholds"]
    assert out["verdict_thresholds"]["lane"] == "buy"


def test_market_brief_has_version_but_empty_thresholds():
    route = _route_tool()
    out = asyncio.run(route(intent="market_brief", market="kr"))
    assert out["success"] is True
    assert out["lane"] == "bootstrap"
    assert set(out["policy_version"]) == {"version", "content_hash"}
    assert out["verdict_thresholds"]["thresholds"] == {}


def test_deterministic_same_input_same_output():
    route = _route_tool()
    a = asyncio.run(route(intent="discovery", market="kr"))
    b = asyncio.run(route(intent="discovery", market="kr"))
    assert a == b


def test_profile_intersection_crypto_drops_toss_place_order():
    # DEFAULT registers toss_place_order; CRYPTO does not. route_request reads
    # the live-registered surface via mcp.list_tools().
    default_mcp = DummyMCP()
    from app.mcp_server.tooling.registry import register_all_tools

    register_all_tools(cast(Any, default_mcp), profile=McpProfile.DEFAULT)
    default_route = default_mcp.tools["route_request"]
    default_out = asyncio.run(default_route(intent="buy_analysis", market="kr"))
    assert "toss_place_order" in [
        s["tool"] for s in default_out["standard_tool_sequence"]
    ]

    crypto_mcp = DummyMCP()
    register_all_tools(cast(Any, crypto_mcp), profile=McpProfile.CRYPTO)
    crypto_route = crypto_mcp.tools["route_request"]
    crypto_out = asyncio.run(crypto_route(intent="buy_analysis", market="crypto"))
    assert "toss_place_order" not in [
        s["tool"] for s in crypto_out["standard_tool_sequence"]
    ]
    assert "toss_place_order" not in crypto_out["allowed_tools"]
```

- [ ] **Step 3: Run test to verify it fails**

Run: `uv run pytest tests/test_route_request.py -q`
Expected: FAIL — `ModuleNotFoundError: app.mcp_server.tooling.route_request_registration`.

- [ ] **Step 4: Write the registration module**

```python
# app/mcp_server/tooling/route_request_registration.py
"""route_request advisory lane router MCP tool (ROB-649).

DIVERGENCE FROM tradingcodex: the original has no route MCP tool — it injects
lane guidance via a hook and maps lane->role->tool indirectly. auto_trader
exposes a DIRECT lane->tool ADVISORY tool with NO enforcement. Blocking
middleware is a separate follow-up issue (mutation tools only; reads
unrestricted; caller-header-keyed because MCP session state resets on
reconnect — ROB-469).
"""

from __future__ import annotations

import inspect
from typing import TYPE_CHECKING, Any

from app.mcp_server.tooling.route_request_lanes import (
    ALL_KNOWN_TOOLS,
    INTENT_TO_LANE,
    LANE_TO_POLICY_LANE,
    VALID_MARKETS,
    build_route_plan,
)
from app.services.trading_policy_service import (
    TradingPolicyKeyError,
    get_policy_for,
    policy_version_stamp,
)

if TYPE_CHECKING:
    from fastmcp import FastMCP

ROUTE_REQUEST_TOOL_NAMES: set[str] = {"route_request"}


async def _live_registered_names(mcp: Any) -> set[str]:
    """Best-effort live tool surface via FastMCP.list_tools(). Fail-open to the
    full known set (no filtering) if introspection is unavailable."""
    lister = getattr(mcp, "list_tools", None)
    if lister is None:
        return set(ALL_KNOWN_TOOLS)
    try:
        result = lister()
        if inspect.isawaitable(result):
            result = await result
        names = {getattr(t, "name", None) for t in result}
        names.discard(None)
        return cast_set(names) or set(ALL_KNOWN_TOOLS)
    except Exception:
        return set(ALL_KNOWN_TOOLS)


def cast_set(names: set[Any]) -> set[str]:
    return {str(n) for n in names}


def register_route_request_tools(mcp: FastMCP) -> None:
    async def route_request(intent: str, market: str) -> dict[str, Any]:
        if intent not in INTENT_TO_LANE:
            return {
                "success": False,
                "error": "unknown_intent",
                "detail": f"unknown intent {intent!r}; valid: {sorted(INTENT_TO_LANE)}",
            }
        if market not in VALID_MARKETS:
            return {
                "success": False,
                "error": "unknown_market",
                "detail": f"unknown market {market!r}; valid: {sorted(VALID_MARKETS)}",
            }
        lane = INTENT_TO_LANE[intent]
        policy_lane = LANE_TO_POLICY_LANE[lane]
        version = policy_version_stamp()
        if policy_lane is None:
            verdict_thresholds: dict[str, Any] = {
                "market": market,
                "lane": None,
                **version,
                "thresholds": {},
            }
        else:
            try:
                verdict_thresholds = get_policy_for(market, policy_lane)
            except TradingPolicyKeyError as exc:
                return {
                    "success": False,
                    "error": "unknown_market",
                    "detail": str(exc),
                }
        registered = await _live_registered_names(mcp)
        return build_route_plan(
            intent,
            market,
            registered_tools=registered,
            verdict_thresholds=verdict_thresholds,
            policy_version=version,
        )

    _ = mcp.tool(
        name="route_request",
        description=(
            "Advisory lane router: map a coarse intent to the standard tool "
            "sequence, allowed/blocked tools, policy thresholds + version stamp, "
            "and hard constraints for that decision lane. Args: intent in "
            "{buy_analysis, profit_taking, discovery, market_brief}, market in "
            "{kr, us, crypto} (required). Deterministic (same input -> same "
            "output). ADVISORY ONLY — it does not block anything; it echoes "
            "get_trading_policy (ROB-646) with policy_version so a verdict can "
            "cite the criteria. standard_tool_sequence is intersected with the "
            "live-registered tool surface (unregistered tools are dropped). "
            "Unknown intent/market returns success=false."
        ),
    )(route_request)


__all__ = [
    "ROUTE_REQUEST_TOOL_NAMES",
    "register_route_request_tools",
]
```

Note: remove the `cast_set` helper indirection if `ruff`/`ty` prefers inline — it exists only to keep the set typed as `set[str]`. Acceptable inline alternative: `names_str = {str(n) for n in names}; return names_str or set(ALL_KNOWN_TOOLS)`.

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_route_request.py -q`
Expected: PASS (7 tests).

- [ ] **Step 6: Commit**

```bash
git add app/mcp_server/tooling/route_request_registration.py tests/test_route_request.py tests/_mcp_tooling_support.py
git commit -m "feat(ROB-649): route_request async tool + registration (advisory, policy echo, profile intersection)"
```

---

### Task 3: Wire into the registry (all profiles)

**Files:**
- Modify: `app/mcp_server/tooling/registry.py`
- Test: `tests/test_route_request.py` (add a per-profile presence test)

**Interfaces:**
- Consumes: `register_route_request_tools` from `route_request_registration`.
- Produces: `route_request` present in every profile's tool surface.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_route_request.py`:

```python
import pytest


class TestRouteRequestRegisteredEveryProfile:
    @pytest.mark.parametrize("profile", list(McpProfile))
    def test_route_request_present(self, profile: McpProfile) -> None:
        tools = build_tools(profile=profile)
        assert "route_request" in tools
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_route_request.py::TestRouteRequestRegisteredEveryProfile -q`
Expected: FAIL — `route_request` not registered by `register_all_tools`.

- [ ] **Step 3: Wire the registry**

In `app/mcp_server/tooling/registry.py`, add the import next to the other tooling imports (after the `trading_policy_registration` import block, lines ~110-112):

```python
from app.mcp_server.tooling.route_request_registration import (
    register_route_request_tools,
)
```

Then in `register_all_tools`, in the always-registered block right after `register_trading_policy_tools(mcp)` (line ~150), add:

```python
    # ROB-649 — advisory lane router; always registered (read-only, no order
    # surface). Intersects lane tool sequences with the live-registered surface.
    register_route_request_tools(mcp)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_route_request.py -q`
Expected: PASS (all, including the 6-profile parametrization).

- [ ] **Step 5: Commit**

```bash
git add app/mcp_server/tooling/registry.py tests/test_route_request.py
git commit -m "feat(ROB-649): register route_request on every MCP profile"
```

---

### Task 4: Registry-diff set-equality partition + playbook drift test

**Files:**
- Create: `tests/test_route_request_registry_diff.py`

**Interfaces:**
- Consumes: `READ_ONLY_ADVISORY_TOOLS`, `MUTATION_TOOLS`, `LANE_SEQUENCES`, `lane_tool_names` from `route_request_lanes`; `register_all_tools`; `DummyMCP`; the playbook YAML.
- Produces: CI failure when a new DEFAULT tool is unclassified, when a classified read-only tool disappears, or when `LANE_SEQUENCES` drifts from the playbook.

- [ ] **Step 1: Write the test**

```python
# tests/test_route_request_registry_diff.py
"""Registry-diff guard for route_request lane classification (ROB-649).

Forces every DEFAULT-profile tool into exactly one of two disjoint buckets
(READ_ONLY_ADVISORY_TOOLS vs MUTATION_TOOLS): a new unclassified tool makes the
partition non-total and fails CI (the silent-drift guard the issue requires,
motivated by the trade_profile tools that sat unregistered for months). Lane
membership is a cross-cutting label — each lane tool is itself either read-only
or a mutation tool — so it is validated separately against the playbook.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, cast

import yaml

from app.mcp_server.profiles import McpProfile
from app.mcp_server.tooling.registry import register_all_tools
from app.mcp_server.tooling.route_request_lanes import (
    ALL_KNOWN_TOOLS,
    LANE_SEQUENCES,
    MUTATION_TOOLS,
    READ_ONLY_ADVISORY_TOOLS,
    lane_tool_names,
)
from tests._mcp_tooling_support import DummyMCP

_PLAYBOOK_PATH = (
    Path(__file__).resolve().parents[1]
    / "docs"
    / "playbooks"
    / "trading-decision-playbook.md"
)
_YAML_BLOCK_RE = re.compile(r"```yaml\n(.*?)```", re.DOTALL)


def _default_tools() -> set[str]:
    mcp = DummyMCP()
    register_all_tools(cast(Any, mcp), profile=McpProfile.DEFAULT)
    return set(mcp.tools.keys())


def _collect_tool_refs(node: Any) -> list[str]:
    found: list[str] = []
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "tool" and isinstance(value, str):
                found.append(value)
            else:
                found.extend(_collect_tool_refs(value))
    elif isinstance(node, list):
        for item in node:
            found.extend(_collect_tool_refs(item))
    return found


def _playbook_lane_tools() -> dict[str, set[str]]:
    text = _PLAYBOOK_PATH.read_text(encoding="utf-8")
    per_lane: dict[str, set[str]] = {}
    for block in _YAML_BLOCK_RE.findall(text):
        parsed = yaml.safe_load(block)
        if isinstance(parsed, dict) and isinstance(parsed.get("lanes"), dict):
            for lane, body in parsed["lanes"].items():
                per_lane.setdefault(lane, set()).update(_collect_tool_refs(body))
    return per_lane


def test_buckets_are_disjoint():
    assert READ_ONLY_ADVISORY_TOOLS.isdisjoint(MUTATION_TOOLS)


def test_every_default_tool_is_classified():
    default = _default_tools()
    unclassified = default - ALL_KNOWN_TOOLS
    assert not unclassified, (
        "new DEFAULT-profile tool(s) not assigned to a route_request bucket "
        "(add to READ_ONLY_ADVISORY_TOOLS or the appropriate mutation set): "
        f"{sorted(unclassified)}"
    )


def test_read_only_bucket_has_no_phantom_tools():
    # A classified read-only tool that no longer registers = rename/removal drift.
    # Tolerate flag-gated read-only tools that are absent at default settings.
    default = _default_tools()
    _FLAG_GATED_OR_OPTIONAL: set[str] = set()
    phantom = READ_ONLY_ADVISORY_TOOLS - default - _FLAG_GATED_OR_OPTIONAL
    assert not phantom, (
        f"READ_ONLY_ADVISORY_TOOLS references unregistered tools: {sorted(phantom)}"
    )


def test_partition_is_total_at_default_settings():
    default = _default_tools()
    assert default == (READ_ONLY_ADVISORY_TOOLS | MUTATION_TOOLS) & default


def test_lane_sequences_match_playbook():
    playbook = _playbook_lane_tools()
    for lane in LANE_SEQUENCES:
        assert lane in playbook, f"lane {lane!r} missing from playbook"
        assert lane_tool_names(lane) == playbook[lane], (
            f"lane {lane!r} drifted from playbook: "
            f"code={sorted(lane_tool_names(lane))} playbook={sorted(playbook[lane])}"
        )


def test_lane_tools_registered_in_default():
    default = _default_tools()
    for lane in LANE_SEQUENCES:
        missing = lane_tool_names(lane) - default
        assert not missing, f"lane {lane!r} references unregistered tools: {sorted(missing)}"
```

- [ ] **Step 2: Run tests to verify they pass**

Run: `uv run pytest tests/test_route_request_registry_diff.py -q`
Expected: PASS (6 tests).

- [ ] **Step 3: Sanity-check the guard actually fails on drift (manual, revert after)**

Temporarily remove one entry (e.g. `"get_quote"`) from `READ_ONLY_ADVISORY_TOOLS`, then:
Run: `uv run pytest tests/test_route_request_registry_diff.py::test_every_default_tool_is_classified -q`
Expected: FAIL listing `['get_quote']`. Restore the entry and re-run → PASS.

- [ ] **Step 4: Commit**

```bash
git add tests/test_route_request_registry_diff.py
git commit -m "test(ROB-649): registry-diff set-equality partition + playbook lane drift guard"
```

---

### Task 5: Divergence documentation

**Files:**
- Modify: `app/mcp_server/README.md`

**Interfaces:** none (docs only).

- [ ] **Step 1: Add the divergence section**

Locate the tools/profiles documentation area of `app/mcp_server/README.md` (search for an existing tool-surface or profile heading with `grep -n "route\|profile\|Tool surface\|## " app/mcp_server/README.md | head`). Add a subsection:

```markdown
### route_request — advisory lane router (ROB-649)

`route_request(intent, market)` maps a coarse intent
(`buy_analysis`/`profit_taking`/`discovery`/`market_brief`) to the standard tool
sequence, advisory allowed/blocked tools, `get_trading_policy` thresholds +
version stamp, and hard constraints for that lane. Deterministic; registered on
every profile; read-only.

**Divergence from tradingcodex:** the original has no route MCP tool — it
injects lane guidance via a hook and maps lane→role→tool indirectly. auto_trader
exposes a **direct lane→tool advisory** tool with **no enforcement**. Blocking
middleware (mutation tools only, reads unrestricted, caller-header-keyed because
MCP session state resets on reconnect — ROB-469) is a separate follow-up issue.

Lane definitions come from the machine-readable `lanes:` blocks in
`docs/playbooks/trading-decision-playbook.md`; `route_request_lanes.LANE_SEQUENCES`
is kept in sync by `tests/test_route_request_registry_diff.py`. Every DEFAULT
tool must be classified into `READ_ONLY_ADVISORY_TOOLS` or a mutation set or CI
fails (silent-drift guard).
```

- [ ] **Step 2: Commit**

```bash
git add app/mcp_server/README.md
git commit -m "docs(ROB-649): document route_request advisory lane router + tradingcodex divergence"
```

---

### Task 6: Full verification

**Files:** none (verification only).

- [ ] **Step 1: Run the ROB-649 test suite + neighbors**

Run: `uv run pytest tests/test_route_request_lanes.py tests/test_route_request.py tests/test_route_request_registry_diff.py tests/test_mcp_profiles.py tests/test_playbook_tool_names.py -q`
Expected: PASS all.

- [ ] **Step 2: Lint + typecheck**

Run: `make lint` (Ruff + ty over `app/ tests/`)
Expected: clean. Fix any findings (notably the `cast_set` helper note in Task 2 — inline it if `ty` flags redundancy).

Run: `make format` if lint reports formatting.

- [ ] **Step 3: Confirm no migration was created**

Run: `git status --short alembic/versions/`
Expected: empty (migration 0).

- [ ] **Step 4: Final commit (only if lint/format changed files)**

```bash
git add -A
git commit -m "chore(ROB-649): lint/format pass"
```

---

## Self-Review

**Spec coverage:**
- Inputs `route_request(intent, market)`, enums, required market → Task 2 (validation) + Task 1 (`VALID_MARKETS`). ✓
- Intent→lane mapping incl. `market_brief`→`bootstrap` → Task 1 `INTENT_TO_LANE`. ✓
- Return contract (all keys) → Task 1 `build_route_plan` + Task 2 policy resolution. ✓
- Deterministic → Task 1 + Task 2 determinism tests. ✓
- Policy echo + `policy_version` (incl. empty for market_brief) → Task 2. ✓
- Profile intersection (req 3) → Task 1 builder + Task 2 crypto test + `DummyMCP.list_tools`. ✓
- Registry-diff set-equality "new unassigned tool fails" (req 2) → Task 4. ✓
- Static dict, definition source = playbook, drift guard → Task 1 + Task 4 `test_lane_sequences_match_playbook`. ✓
- Registered on every profile (req: all profiles) → Task 3. ✓
- Divergence doc (req 4) → Task 2 (tool description) + Task 5 (README). ✓
- migration 0 → Task 6 Step 3. ✓

**Placeholder scan:** no TBD/TODO; all code shown in full; the one `cast_set` indirection has an explicit inline alternative noted. ✓

**Type consistency:** `build_route_plan(intent, market, *, registered_tools, verdict_thresholds, policy_version)` signature identical across Task 1 definition and Task 2 call. `ROUTE_REQUEST_TOOL_NAMES` set in Task 2 matches its use. `lane_tool_names` used consistently in Tasks 1 & 4. ✓
