"""Contract tests for bounded, observation-only candidate fan-out."""

from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import app.mcp_server.tooling.buy_candidate_fanout as fanout
from app.mcp_server.tooling.buy_candidate_fanout import (
    MAX_SNAPSHOT_PRESETS_PER_CALL,
    TOP_N_PER_SOURCE,
    TOP_N_REVALIDATION,
    discover_buy_candidates_fanout_impl,
)

pytestmark = pytest.mark.unit


def _source_row(symbol: str, rank: int = 1) -> dict[str, Any]:
    return {"symbol": symbol, "name": f"name-{symbol}", "rank": rank}


def _fresh_row(
    *, rsi: float = 35, target: float = 145, support: float = 95
) -> dict[str, Any]:
    return {
        "data_state": "fresh",
        "current_price": 100,
        "rsi_14": rsi,
        "consensus": {"avg_target_price": target},
        "supports": [
            {
                "price": support,
                "strength": "moderate",
                "sources": ["fib_50", "bb_lower"],
            }
        ],
    }


@pytest.mark.asyncio
async def test_five_families_are_bounded_deduped_and_funnelled() -> None:
    live_calls: list[tuple[str, dict[str, Any]]] = []
    snapshot_calls: list[tuple[str, tuple[str, ...], int]] = []
    revalidation_calls: list[list[str]] = []

    async def live_reader(source: Any, market: str, top_n: int) -> dict[str, Any]:
        assert market == "kr"
        request = {
            "market": market,
            "sort_by": source.sort_by,
            "sort_order": source.sort_order,
            "limit": top_n,
        }
        live_calls.append((source.source, request))
        rows_by_source = {
            # Generic live `screen_stocks` uses `code`, while snapshots use
            # `symbol`; both must normalize through the DB symbol contract.
            "rsi": [{"code": "BRK-B", "name": "name-BRK-B", "rank": 1}],
            "change_rate": [_source_row("BRK/B")],
            "trade_amount": [_source_row("005930")],
        }
        return {
            "source": source.source,
            "family": source.source,
            "kind": "live",
            "rows": rows_by_source[source.source],
            "metadata": {"request": request},
        }

    async def snapshot_reader(
        family: str, presets: tuple[str, ...], market: str, top_n: int
    ) -> list[dict[str, Any]]:
        assert market == "kr"
        snapshot_calls.append((family, presets, top_n))
        payloads: list[dict[str, Any]] = []
        for preset in presets:
            symbol = "BRK.B" if preset == "cheap_value" else f"{family[:4]}-{preset}"
            payloads.append(
                {
                    "source": f"{family}:{preset}",
                    "family": family,
                    "kind": "snapshot",
                    "rows": [_source_row(symbol)],
                    "metadata": {"scheduled_preset_count": len(presets)},
                }
            )
        return payloads

    async def fresh_revalidator(
        symbols: list[str], market: str
    ) -> dict[str, dict[str, Any]]:
        assert market == "kr"
        revalidation_calls.append(symbols)
        return {
            symbol: _fresh_row(rsi=55 if symbol == "BRK.B" else 35)
            for symbol in symbols
        }

    result = await discover_buy_candidates_fanout_impl(
        _live_reader=live_reader,
        _snapshot_reader=snapshot_reader,
        _fresh_revalidator=fresh_revalidator,
    )

    assert [
        (name, request["sort_by"], request["sort_order"])
        for name, request in live_calls
    ] == [
        ("rsi", "rsi", "asc"),
        ("change_rate", "change_rate", "asc"),
        ("trade_amount", "trade_amount", "desc"),
    ]
    # RSI source is ordering only, never a max_rsi candidate prefilter.
    assert "max_rsi" not in live_calls[0][1]
    assert all(top_n == TOP_N_PER_SOURCE for _, _, top_n in snapshot_calls)
    assert all(
        len(presets) <= MAX_SNAPSHOT_PRESETS_PER_CALL
        for _, presets, _ in snapshot_calls
    )
    assert {family for family, _, _ in snapshot_calls} == {
        "snapshot_support_flow",
        "snapshot_value_catalyst",
    }

    duplicate = next(
        candidate
        for candidate in result["candidates"]
        if candidate["symbol"] == "BRK.B"
    )
    assert duplicate["matched_sources"] == [
        "rsi",
        "change_rate",
        "snapshot_value_catalyst:cheap_value",
    ]
    assert duplicate["rsi_only_fail_candidate"] is True
    assert duplicate["regular_evidence_eligible"] is False
    assert duplicate["funnel"]["rsi"]["status"] == "rsi_only_fail"
    assert duplicate["funnel"]["budget"]["status"] == "deferred"

    regular = next(
        candidate
        for candidate in result["candidates"]
        if candidate["symbol"] == "005930"
    )
    assert regular["regular_evidence_eligible"] is True
    assert regular["actionable"] is False
    assert list(regular["funnel"]) == [
        "source",
        "base_eligibility",
        "support_source_count",
        "upside",
        "rsi",
        "anchor_band",
        "budget",
    ]
    assert regular["funnel"]["anchor_band"]["non_executable"] is True

    assert len(revalidation_calls) == 1
    assert len(revalidation_calls[0]) == TOP_N_REVALIDATION
    unrevalidated = result["candidates"][TOP_N_REVALIDATION]
    assert unrevalidated["revalidation"]["status"] == "not_revalidated_top_n_limit"
    assert result["bounds"] == {
        "top_n_per_source": 10,
        "top_n_revalidation": 10,
        "max_snapshot_presets_per_call": 5,
        "snapshot_max_stale_sessions": 1,
        "snapshot_values_are_input_only_until_fresh_revalidation": True,
    }
    assert result["digest_observation"]["actionable_count"] == 0
    assert (
        result["digest_observation"][
            "not_for_pnl_scoring_or_immediate_threshold_tuning"
        ]
        is True
    )
    rsi_stats = next(
        stats
        for stats in result["digest_observation"]["source_stats"]
        if stats["source"] == "rsi"
    )
    assert {
        "incoming_count",
        "top_n_count",
        "dropped_reasons",
        "regular_evidence_eligible_count",
        "rsi_only_fail_candidate_count",
        "actionable_count",
        "final_eligible_counts",
    } <= rsi_stats.keys()
    assert rsi_stats["rsi_only_fail_candidate_count"] == 1
    assert rsi_stats["final_eligible_counts"]["rsi_only_fail_candidate"] == 1


@pytest.mark.asyncio
async def test_source_top_n_cap_precedes_dedupe_and_revalidation() -> None:
    revalidation_calls: list[list[str]] = []

    async def live_reader(source: Any, market: str, top_n: int) -> dict[str, Any]:
        rows = (
            [_source_row(f"TOP-{index}") for index in range(TOP_N_PER_SOURCE + 1)]
            if source.source == "rsi"
            else []
        )
        return {
            "source": source.source,
            "family": source.source,
            "kind": "live",
            "rows": rows,
            "metadata": {},
        }

    async def snapshot_reader(
        family: str, presets: tuple[str, ...], market: str, top_n: int
    ) -> list[dict[str, Any]]:
        return [
            {
                "source": f"{family}:{preset}",
                "family": family,
                "kind": "snapshot",
                "rows": [],
                "metadata": {},
            }
            for preset in presets
        ]

    async def fresh_revalidator(
        symbols: list[str], market: str
    ) -> dict[str, dict[str, Any]]:
        revalidation_calls.append(symbols)
        return {symbol: _fresh_row() for symbol in symbols}

    result = await discover_buy_candidates_fanout_impl(
        _live_reader=live_reader,
        _snapshot_reader=snapshot_reader,
        _fresh_revalidator=fresh_revalidator,
    )

    assert len(result["candidates"]) == TOP_N_PER_SOURCE
    assert revalidation_calls == [[f"TOP.{index}" for index in range(TOP_N_PER_SOURCE)]]
    rsi_stats = next(
        stats
        for stats in result["digest_observation"]["source_stats"]
        if stats["source"] == "rsi"
    )
    assert rsi_stats["incoming_count"] == TOP_N_PER_SOURCE + 1
    assert rsi_stats["top_n_count"] == TOP_N_PER_SOURCE
    assert rsi_stats["dropped_reasons"]["outside_source_top_n"] == 1


@pytest.mark.asyncio
async def test_honest_upside_and_support_distance_stay_fail_closed() -> None:
    gates = fanout._FanoutGates.from_policy(fanout.load_trading_policy())
    candidate = {"matched_sources": ["rsi"]}

    low_upside = fanout._evaluate_funnel(candidate, _fresh_row(target=135), gates)
    assert low_upside["funnel"]["upside"]["status"] == "fail"
    assert low_upside["funnel"]["upside"]["reason"] == "honest_upside_below_40pct"

    distant_support = fanout._evaluate_funnel(candidate, _fresh_row(support=91), gates)
    assert distant_support["funnel"]["support_source_count"]["status"] == "fail"
    assert distant_support["funnel"]["support_source_count"]["reason"] == (
        "support_more_than_8pct_below_current"
    )


@pytest.mark.asyncio
async def test_fresh_revalidation_requires_fresh_data_and_clear_restrictions() -> None:
    gates = fanout._FanoutGates.from_policy(fanout.load_trading_policy())
    candidate = {"matched_sources": ["snapshot_support_flow:support_proximity"]}

    stale = fanout._evaluate_funnel(
        candidate, {**_fresh_row(), "data_state": "stale"}, gates
    )
    assert stale["freshness"]["status"] == "not_fresh"
    assert stale["funnel"]["base_eligibility"]["status"] == "fail"
    assert stale["funnel"]["base_eligibility"]["reason"] == (
        "fresh_revalidation_data_state_not_fresh"
    )

    suspended = fanout._evaluate_funnel(
        candidate, {**_fresh_row(), "trading_suspended": True}, gates
    )
    assert suspended["funnel"]["base_eligibility"]["reason"] == "trading_suspended"


@pytest.mark.asyncio
async def test_kr_regular_session_compact_shape_is_undetermined_not_a_zero_funnel() -> (
    None
):
    """A KR RTH compact-shaped reply lacks data_state but remains observation-only."""

    async def live_reader(source: Any, market: str, top_n: int) -> dict[str, Any]:
        assert market == "kr"
        assert top_n == TOP_N_PER_SOURCE
        return {
            "source": source.source,
            "family": source.source,
            "kind": "live",
            "rows": [_source_row("005930")] if source.source == "rsi" else [],
            "metadata": {},
        }

    async def snapshot_reader(
        family: str, presets: tuple[str, ...], market: str, top_n: int
    ) -> list[dict[str, Any]]:
        return [
            {
                "source": f"{family}:{preset}",
                "family": family,
                "kind": "snapshot",
                "rows": [],
                "metadata": {},
            }
            for preset in presets
        ]

    async def rth_compact_shaped_revalidator(
        symbols: list[str], market: str
    ) -> dict[str, dict[str, Any]]:
        assert symbols == ["005930"]
        assert market == "kr"
        # This is the historic KR regular-session compact shape: all pricing
        # evidence is present, but the top-level aggregate data_state is absent.
        compact_shaped = _fresh_row()
        compact_shaped.pop("data_state")
        return {"005930": compact_shaped}

    result = await discover_buy_candidates_fanout_impl(
        _live_reader=live_reader,
        _snapshot_reader=snapshot_reader,
        _fresh_revalidator=rth_compact_shaped_revalidator,
    )

    candidate = result["candidates"][0]
    assert candidate["freshness"] == {
        "status": "undetermined",
        "reason": "freshness_data_state_missing",
        "data_state": None,
        "evidence_source": "full_analysis_top_level_data_state",
        "eligibility_blocked": True,
    }
    assert candidate["regular_evidence_eligible"] is False
    assert candidate["rsi_only_fail_candidate"] is False
    assert candidate["observation_gate_path_complete"] is True
    assert candidate["actionable"] is False
    assert {
        stage: candidate["funnel"][stage]["status"]
        for stage in result["funnel_stage_order"]
    } == {
        "source": "pass",
        "base_eligibility": "undetermined",
        "support_source_count": "pass",
        "upside": "pass",
        "rsi": "regular_pass",
        "anchor_band": "pass",
        "budget": "deferred",
    }
    assert candidate["funnel"]["base_eligibility"]["continued_as_observation_only"]
    assert candidate["funnel"]["support_source_count"][
        "eligibility_blocked_by_freshness"
    ]

    digest = result["digest_observation"]
    assert digest["freshness_undetermined_count"] == 1
    assert digest["freshness_undetermined_reasons"] == {
        "freshness_data_state_missing": 1
    }
    assert digest["funnel_stage_counts"] == {
        "source": {"pass": 1},
        "base_eligibility": {"undetermined": 1},
        "support_source_count": {"pass": 1},
        "upside": {"pass": 1},
        "rsi": {"regular_pass": 1},
        "anchor_band": {"pass": 1},
        "budget": {"deferred": 1},
    }
    rsi_stats = next(
        stats for stats in digest["source_stats"] if stats["source"] == "rsi"
    )
    assert rsi_stats["freshness_undetermined_reasons"] == {
        "freshness_data_state_missing": 1
    }
    assert "freshness_data_state_missing" not in rsi_stats["dropped_reasons"]
    assert rsi_stats["funnel_stage_counts"] == digest["funnel_stage_counts"]


@pytest.mark.asyncio
async def test_undetermined_freshness_never_counts_as_rsi_only_fail_candidate() -> None:
    """Unproven freshness must block both regular and RSI-only eligibility lanes."""

    async def live_reader(source: Any, market: str, top_n: int) -> dict[str, Any]:
        return {
            "source": source.source,
            "family": source.source,
            "kind": "live",
            "rows": [_source_row("005930")] if source.source == "rsi" else [],
            "metadata": {},
        }

    async def snapshot_reader(
        family: str, presets: tuple[str, ...], market: str, top_n: int
    ) -> list[dict[str, Any]]:
        return [
            {
                "source": f"{family}:{preset}",
                "family": family,
                "kind": "snapshot",
                "rows": [],
                "metadata": {},
            }
            for preset in presets
        ]

    async def unknown_freshness_revalidator(
        symbols: list[str], market: str
    ) -> dict[str, dict[str, Any]]:
        assert symbols == ["005930"]
        assert market == "kr"
        unknown = _fresh_row(rsi=62)
        unknown.pop("data_state")
        return {"005930": unknown}

    result = await discover_buy_candidates_fanout_impl(
        _live_reader=live_reader,
        _snapshot_reader=snapshot_reader,
        _fresh_revalidator=unknown_freshness_revalidator,
    )

    candidate = result["candidates"][0]
    assert candidate["freshness"]["status"] == "undetermined"
    assert candidate["funnel"]["rsi"]["status"] == "rsi_only_fail"
    assert candidate["regular_evidence_eligible"] is False
    assert candidate["rsi_only_fail_candidate"] is False

    digest = result["digest_observation"]
    assert digest["rsi_only_fail_candidate_count"] == 0
    assert digest["final_eligible_counts"]["rsi_only_fail_candidate"] == 0
    rsi_stats = next(
        stats for stats in digest["source_stats"] if stats["source"] == "rsi"
    )
    assert rsi_stats["rsi_only_fail_candidate_count"] == 0
    assert rsi_stats["final_eligible_counts"]["rsi_only_fail_candidate"] == 0


def test_support_strength_and_independent_family_gates_are_pinned() -> None:
    gates = fanout._FanoutGates.from_policy(fanout.load_trading_policy())

    weak_support, weak_reason = fanout._support_evidence(
        [
            {
                "price": 95,
                "strength": "weak",
                "sources": ["fib_50", "bb_lower"],
            }
        ],
        current_price=100,
        gates=gates,
    )
    one_family_support, one_family_reason = fanout._support_evidence(
        [
            {
                "price": 95,
                "strength": "moderate",
                "sources": ["fib_50"],
            }
        ],
        current_price=100,
        gates=gates,
    )

    assert weak_support is None
    assert weak_reason == "support_strength_below_moderate"
    assert one_family_support is None
    assert one_family_reason == "independent_support_family_count_below_2"


def test_anchor_band_empty_intersection_stays_excluded() -> None:
    gates = fanout._FanoutGates.from_policy(fanout.load_trading_policy())

    anchor, reason = fanout._anchor_band(
        support_price=70,
        current_price=100,
        gates=gates,
    )

    assert anchor is None
    assert reason == "anchor_band_outside_final_distance_range"


@pytest.mark.parametrize(
    ("field", "drifted_value"),
    [
        ("rsi_max", 44),
        ("independent_support_source_count_min", 1),
        ("support_within_current_pct_max", 12),
        ("honest_upside_pct_min", 30),
        ("support_strength_min", "weak"),
        ("independent_support_source_families", ["fib"]),
        ("discount_below_support_pct_range", [4, 10]),
        ("final_limit_distance_from_current_pct_range", [-20, -5]),
        ("all_pending_buy_required_cash_hard_cap_pct", 91),
        ("tier_armed_required_cash_cap_pct", 51),
    ],
)
def test_frozen_gate_literal_guard_rejects_each_drift(
    field: str, drifted_value: Any
) -> None:
    policy = fanout.load_trading_policy().model_copy(deep=True)
    if field == "rsi_max":
        policy.thresholds["screen.rsi_max"].value = drifted_value
    else:
        reserve = policy.decision_rules["buy.support_reserve_net"]
        setattr(reserve, field, drifted_value)

    with pytest.raises(ValueError, match="fanout gate literals"):
        fanout._FanoutGates.from_policy(policy)


def test_snapshot_group_contract_rejects_more_than_five_presets() -> None:
    with pytest.raises(ValueError, match="exceeds"):
        fanout._validate_snapshot_preset_group(
            "too_many", tuple(str(value) for value in range(6))
        )


def test_snapshot_staleness_contract_allows_at_most_one_prior_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from datetime import date

    from app.services.invest_screener_snapshots import freshness

    monkeypatch.setattr(
        freshness, "expected_baseline_date", lambda market: date(2026, 8, 13)
    )
    within_limit = fanout._snapshot_staleness_contract(
        {"primary": {"snapshotDate": "2026-08-12"}}, "kr"
    )
    too_old = fanout._snapshot_staleness_contract(
        {"primary": {"snapshotDate": "2026-08-11"}}, "kr"
    )

    assert within_limit["within_limit"] is True
    assert within_limit["minimum_allowed_snapshot_date"] == "2026-08-12"
    assert too_old["within_limit"] is False
    assert too_old["reason"] == "snapshot_more_than_one_session_stale"


@pytest.mark.asyncio
async def test_live_reader_calls_real_screen_adapter_without_rsi_prefilter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.mcp_server.tooling import analysis_tool_handlers

    received: dict[str, Any] = {}

    async def fake_screen_stocks_impl(**request: Any) -> dict[str, Any]:
        received.update(request)
        return {"results": [{"code": "005930", "rank": 1}], "total_count": 73}

    monkeypatch.setattr(
        analysis_tool_handlers, "screen_stocks_impl", fake_screen_stocks_impl
    )

    payload = await fanout._read_live_source(
        fanout._LIVE_SOURCES[0], "kr", TOP_N_PER_SOURCE
    )

    assert received == {
        "market": "kr",
        "sort_by": "rsi",
        "sort_order": "asc",
        "limit": TOP_N_PER_SOURCE,
    }
    assert "max_rsi" not in received
    assert payload["rows"] == [{"code": "005930", "rank": 1}]
    assert payload["metadata"]["upstream_total_count"] == 73
    assert fanout._initial_source_stats(payload)["source_population"] == {
        "status": "reported",
        "reported_total_count": 73,
        "top_n_read_count": 1,
    }


@pytest.mark.asyncio
async def test_snapshot_reader_calls_persisted_view_model_with_no_portfolio_relation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from datetime import date

    import app.core.db as db
    import app.services.invest_screener_snapshots.freshness as freshness
    import app.services.invest_view_model.screener_service as view_model
    import app.services.screener_service as screener_service

    class _Dumpable:
        def __init__(self, value: dict[str, Any]) -> None:
            self.value = value

        def model_dump(self, *, mode: str) -> dict[str, Any]:
            assert mode == "json"
            return dict(self.value)

    class _Session:
        async def __aenter__(self) -> _Session:
            return self

        async def __aexit__(
            self,
            exc_type: object,
            exc: object,
            traceback: object,
        ) -> None:
            return None

    session_instance = _Session()
    calls: list[str] = []

    async def fake_build_screener_results(
        preset: str,
        service: object,
        resolver: Any,
        *,
        market: str,
        session: object,
    ) -> SimpleNamespace:
        assert service is not None
        assert market == "kr"
        assert session is session_instance
        assert resolver.relation("kr", "005930") == "none"
        calls.append(preset)
        return SimpleNamespace(
            results=[
                _Dumpable({"symbol": "005930", "rank": rank})
                for rank in range(TOP_N_PER_SOURCE + 1)
            ],
            freshness=_Dumpable({"primary": {"snapshotDate": "2026-08-13"}}),
        )

    # The fake session factory and fake view-model builder make this a direct
    # contract test of the production reader body; no DB/MCP/broker call occurs.
    monkeypatch.setattr(db, "AsyncSessionLocal", lambda: session_instance)
    monkeypatch.setattr(screener_service, "ScreenerService", lambda: object())
    monkeypatch.setattr(
        view_model, "build_screener_results", fake_build_screener_results
    )
    monkeypatch.setattr(
        freshness, "expected_baseline_date", lambda market: date(2026, 8, 13)
    )

    presets = ("one", "two", "three", "four", "five")
    payloads = await fanout._read_snapshot_group(
        "snapshot_support_flow", presets, "kr", TOP_N_PER_SOURCE
    )

    assert calls == list(presets)
    assert len(payloads) == MAX_SNAPSHOT_PRESETS_PER_CALL
    assert all(len(payload["rows"]) == TOP_N_PER_SOURCE for payload in payloads)
    assert all(
        payload["metadata"]["snapshot_staleness_contract"]["within_limit"]
        for payload in payloads
    )


@pytest.mark.asyncio
async def test_full_revalidator_preserves_rth_data_state_and_all_support_levels(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.mcp_server.tooling import analysis_tool_handlers

    received: dict[str, Any] = {}
    supports = [
        {
            "price": 99 - index,
            "strength": "moderate",
            "sources": ["fib_50", "bb_lower"],
        }
        for index in range(4)
    ]

    async def fake_analyze_stock_batch_impl(**request: Any) -> dict[str, Any]:
        received.update(request)
        return {
            "results": {
                "005930": {
                    "data_state": "fresh",
                    "quote": {"price": 100},
                    "indicators": {"rsi": {"14": 35}},
                    "support_resistance": {"supports": supports},
                    "opinions": {"consensus": {"avg_target_price": 145}},
                    "trading_suspended": False,
                }
            }
        }

    monkeypatch.setattr(
        analysis_tool_handlers,
        "analyze_stock_batch_impl",
        fake_analyze_stock_batch_impl,
    )

    revalidated = await fanout._fresh_revalidate(["005930"], "kr")

    assert received == {
        "symbols": ["005930"],
        "market": "kr",
        "include_peers": False,
        "quick": False,
        "include_position": False,
        "refresh": False,
    }
    assert revalidated == {
        "005930": {
            "data_state": "fresh",
            "current_price": 100.0,
            "rsi_14": 35.0,
            "supports": supports,
            "consensus": {"avg_target_price": 145},
            "trading_suspended": False,
        }
    }


def test_live_source_population_is_explicitly_bounded_unknown_without_total() -> None:
    stats = fanout._initial_source_stats(
        {
            "source": "rsi",
            "family": "rsi",
            "kind": "live",
            "rows": [_source_row("005930")],
            "metadata": {},
        }
    )

    assert stats["source_population"] == {
        "status": "bounded_unknown",
        "reason": "upstream_total_not_reported_with_top_n_only_read",
        "top_n_read_count": 1,
    }


def test_registration_is_read_only_and_observation_only() -> None:
    from app.mcp_server.tooling.analysis_readonly_registration import (
        ANALYSIS_READONLY_TOOL_NAMES,
    )
    from app.mcp_server.tooling.buy_candidate_fanout_registration import (
        register_buy_candidate_fanout_tools,
    )

    class _FakeMCP:
        def __init__(self) -> None:
            self.descriptions: dict[str, str] = {}

        def tool(self, *, name: str, description: str, **_: Any) -> Any:
            def decorate(function: Any) -> Any:
                self.descriptions[name] = description
                return function

            return decorate

    mcp = _FakeMCP()
    register_buy_candidate_fanout_tools(mcp)  # type: ignore[arg-type]
    assert "discover_buy_candidates_fanout" in ANALYSIS_READONLY_TOOL_NAMES
    description = mcp.descriptions["discover_buy_candidates_fanout"].lower()
    assert "read-only" in description
    assert "never a proposal or order" in description
    assert "threshold tuning" in description
    assert "undetermined" in description


def test_no_broker_or_order_surface_imported_by_fanout_module() -> None:
    source = fanout.__file__
    assert source is not None
    text = Path(source).read_text(encoding="utf-8")
    tree = ast.parse(text)
    imported_modules = {
        node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
    } | {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    forbidden_prefixes = (
        "app.services.order_proposals",
        "app.services.brokers",
        "app.mcp_server.tooling.orders",
        "app.mcp_server.tooling.portfolio",
    )
    assert all(not module.startswith(forbidden_prefixes) for module in imported_modules)


def test_runbook_states_observation_only_and_no_tuning() -> None:
    runbook = (
        Path(__file__).resolve().parents[3]
        / "docs"
        / "runbooks"
        / "buy-candidate-fanout.md"
    ).read_text(encoding="utf-8")
    normalized = " ".join(runbook.split())
    assert "do not use it as PnL scoring" in normalized
    assert "immediate threshold-tuning evidence" in normalized
    assert "actionable_count` is always zero" in normalized
    assert "freshness_data_state_missing" in normalized
    assert "bounded_unknown" in normalized
