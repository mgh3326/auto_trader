"""Contract tests for bounded, observation-only candidate fan-out."""

from __future__ import annotations

import ast
from pathlib import Path
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
    assert stale["funnel"]["base_eligibility"]["reason"] == (
        "fresh_revalidation_data_state_not_fresh"
    )

    suspended = fanout._evaluate_funnel(
        candidate, {**_fresh_row(), "trading_suspended": True}, gates
    )
    assert suspended["funnel"]["base_eligibility"]["reason"] == "trading_suspended"


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
