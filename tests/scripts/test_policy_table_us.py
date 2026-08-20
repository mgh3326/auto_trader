"""Unit tests for ROB-1230 P-2-US / B0-X U-1 policy_table US adapter.

Pure-compute path only (no DB/network). Covers labels, tick, determinism,
D3 engine parity, and sell-side MISMATCH stamping.
"""

from __future__ import annotations

import ast
from decimal import Decimal
from pathlib import Path

import pytest

from research.kr_corpus.d3_engine.indicators import (
    OhlcPoint,
    bollinger_bands,
    rsi_wilder,
    scan_fib_window,
)
from research.kr_corpus.d3_engine.tick import TickTable
from scripts.policy_table.adapters import us as us_adapter
from scripts.policy_table.core.schema import (
    canonical_json_bytes,
    compute_policy_table_hash,
)
from scripts.policy_table.core.signal_math import FIB_WINDOW, compute_symbol_signal
from scripts.policy_table.core.trust_labels import (
    CROSS_MARKET_TRANSFER_UNVALIDATED,
    TRUST_LABELS,
    US_TRUST_LABELS,
)
from scripts.policy_table.core.us_tick import TICK_SOURCE, build_us_equity_tick_table


def _synthetic_bars(
    n: int = 150, *, start: Decimal = Decimal("100")
) -> list[list[str]]:
    """Ascending OHLC with mild trend — enough history for fib 120."""

    bars: list[list[str]] = []
    price = start
    for i in range(n):
        # Deterministic mild oscillation so BB/RSI are well-defined.
        delta = Decimal("0.5") if i % 5 else Decimal("-0.3")
        close = (price + delta).quantize(Decimal("0.01"))
        high = (close + Decimal("1.00")).quantize(Decimal("0.01"))
        low = (close - Decimal("1.00")).quantize(Decimal("0.01"))
        bars.append([str(close), str(high), str(low)])
        price = close
    return bars


def _sample_raw(*, bars: list[list[str]] | None = None) -> us_adapter.RawInputs:
    bars = bars if bars is not None else _synthetic_bars()
    return us_adapter.RawInputs(
        as_of="2026-08-08T12:00:00+00:00",
        holdings=[
            {
                "symbol": "AAPL",
                "quantity": "10",
                "average_price": "150.00",
            }
        ],
        watch_alerts=[],
        universe_pool=[
            {
                "symbol": "AAPL",
                "latest_close": bars[-1][0],
                "market_cap": "3000000000000",
                "market_cap_source": "market_valuation_snapshots:yahoo",
                "daily_turnover": "5000000000",
                "daily_volume": "50000000",
                "snapshot_date": "2026-08-07",
                "exchange": "NASD",
                "is_common_stock": True,
            },
            {
                "symbol": "TINY",
                "latest_close": "2.00",
                "market_cap": "1000000",  # below $100M floor
                "market_cap_source": "market_valuation_snapshots:yahoo",
                "daily_turnover": "5000",
                "daily_volume": "2500",
                "snapshot_date": "2026-08-07",
                "exchange": "NASD",
                "is_common_stock": True,
            },
            {
                "symbol": "PREF",
                "latest_close": "25.00",
                "market_cap": "500000000",
                "market_cap_source": "market_valuation_snapshots:yahoo",
                "daily_turnover": "2000000",
                "daily_volume": "80000",
                "snapshot_date": "2026-08-07",
                "exchange": "NYSE",
                "is_common_stock": False,  # preferred — filter out
            },
        ],
        snapshot_partition_date="2026-08-07",
        snapshot_breadth={
            "total": 3,
            "advancers": 1,
            "decliners": 1,
            "unchanged": 1,
            "partition_date": "2026-08-07",
        },
        # filter_passed(AAPL) ∪ holdings(AAPL) — TINY/PREF excluded by filter
        universe_symbols=["AAPL"],
        candles={"AAPL": bars},
        market_cap_fill_stats={
            "snapshot_total": 3,
            "snapshot_market_cap_non_null": 0,
            "valuation_yahoo_fallback_used": 3,
            "market_cap_missing": 0,
            "note": "unit-test fixture",
        },
    )


@pytest.mark.unit
def test_us_max_table_age_stamp_is_contract_v11_36h() -> None:
    """US table stamps MAX_TABLE_AGE=36h from contract v1.1 §2-2 (not invented)."""

    from scripts.policy_table.core.max_table_age import (
        CONTRACT_V11_SECTION_2_2,
        MAX_TABLE_AGE_HOURS,
    )

    assert MAX_TABLE_AGE_HOURS["us"] == 36
    payload = us_adapter.compute_policy_table(_sample_raw())
    assert payload["config"]["max_table_age_hours"] == 36
    assert payload["config"]["max_table_age_source"] == CONTRACT_V11_SECTION_2_2
    assert "§2-2" in payload["config"]["max_table_age_source"]
    assert "97278b0e" in payload["config"]["max_table_age_source"]


@pytest.mark.unit
def test_us_trust_labels_are_four_including_cross_market() -> None:
    assert len(US_TRUST_LABELS) == 4
    assert US_TRUST_LABELS[:3] == TRUST_LABELS
    assert US_TRUST_LABELS[3] == CROSS_MARKET_TRANSFER_UNVALIDATED
    assert "CROSS_MARKET_TRANSFER_UNVALIDATED" in US_TRUST_LABELS[3]
    payload = us_adapter.compute_policy_table(_sample_raw())
    assert payload["trust_labels"] == list(US_TRUST_LABELS)
    assert payload["config"]["b0x_labels"] == [
        "B0_UNVALIDATED",
        "SELL_SIDE_MODEL_MISMATCH",
        "FIDELITY_INCONCLUSIVE_COVERAGE",
        "CROSS_MARKET_TRANSFER_UNVALIDATED",
    ]


@pytest.mark.unit
def test_us_tick_table_reg_nms_rule_612() -> None:
    table = build_us_equity_tick_table()
    assert isinstance(table, TickTable)
    assert table.align_buy(Decimal("10.234")) == Decimal("10.23")
    assert table.align_sell(Decimal("10.231")) == Decimal("10.24")
    assert table.align_buy(Decimal("0.55555")) == Decimal("0.5555")
    assert "Rule 612" in TICK_SOURCE or "0.01" in TICK_SOURCE


@pytest.mark.unit
def test_sell_side_mismatch_label_per_computed_row() -> None:
    payload = us_adapter.compute_policy_table(_sample_raw())
    computed = [r for r in payload["rows"] if not r["insufficient_history"]]
    assert computed
    for row in computed:
        assert row["B_sell_side"]["label"] == "SELL_SIDE_MODEL_MISMATCH"


@pytest.mark.unit
def test_filter_counts_and_skipped_reasons() -> None:
    # AAPL has history; add a symbol with no candles to force skip accounting.
    raw = _sample_raw()
    raw = us_adapter.RawInputs(
        as_of=raw.as_of,
        holdings=raw.holdings,
        watch_alerts=raw.watch_alerts,
        universe_pool=raw.universe_pool,
        snapshot_partition_date=raw.snapshot_partition_date,
        snapshot_breadth=raw.snapshot_breadth,
        universe_symbols=["AAPL", "NEWIPO"],
        candles=raw.candles,  # NEWIPO absent → no candles
        market_cap_fill_stats=raw.market_cap_fill_stats,
    )
    payload = us_adapter.compute_policy_table(raw)
    u = payload["universe"]
    assert u["snapshot_total_symbols"] == 3
    assert u["filter_passed_symbols"] == 1  # AAPL only
    assert u["attempted_symbols"] == 2
    assert u["computed_symbols"] == 1
    assert any(s["reason"] == "no_candles_in_db" for s in u["skipped"])


@pytest.mark.unit
def test_compute_deterministic_two_runs() -> None:
    raw = _sample_raw()
    p1 = us_adapter.compute_policy_table(raw)
    p1["stamps"] = {
        "policy_table_hash": compute_policy_table_hash(p1),
        "auto_trader_head": "test",
        "indicator_code_commit": "test",
        "engine_module_sha256": {},
        "input_as_of": p1["generated_at"],
    }
    p2 = us_adapter.compute_policy_table(raw)
    p2["stamps"] = {
        "policy_table_hash": compute_policy_table_hash(p2),
        "auto_trader_head": "test",
        "indicator_code_commit": "test",
        "engine_module_sha256": {},
        "input_as_of": p2["generated_at"],
    }
    b1 = canonical_json_bytes(p1)
    b2 = canonical_json_bytes(p2)
    assert b1 == b2
    assert p1["stamps"]["policy_table_hash"] == p2["stamps"]["policy_table_hash"]


@pytest.mark.unit
def test_engine_match_rsi_bb_buy_l1() -> None:
    """Table row values match a direct D3-engine call on the same bars."""

    bars = _synthetic_bars(150)
    closes = [Decimal(b[0]) for b in bars]
    highs = [Decimal(b[1]) for b in bars]
    lows = [Decimal(b[2]) for b in bars]
    tick = build_us_equity_tick_table()
    signal = compute_symbol_signal(
        closes=closes, highs=highs, lows=lows, tick_table=tick
    )

    # Direct D3 pieces (same as signal_math).
    points = [
        OhlcPoint(high=highs[i], low=lows[i], close=closes[i])
        for i in range(len(closes))
    ]
    points.append(points[-1])
    window = scan_fib_window(points, decision_index=len(closes))
    rsi_series = rsi_wilder(closes, period=14)
    bands = bollinger_bands(closes, window=20, sigma=Decimal("2"))
    expected_buy_l1 = tick.align_buy(closes[-1] * Decimal("0.97"))

    assert signal.rsi == (
        rsi_series[-1].quantize(Decimal("0.0001"))
        if rsi_series[-1] is not None
        else None
    )
    assert signal.bb_lower == bands.lower
    assert signal.bb_upper == bands.upper
    assert signal.fib_window_low == window.low
    assert signal.fib_window_high == window.high
    assert signal.buy_l1 == expected_buy_l1

    raw = _sample_raw(bars=bars)
    payload = us_adapter.compute_policy_table(raw)
    row = next(r for r in payload["rows"] if r["symbol"] == "AAPL")
    assert row["insufficient_history"] is False
    assert row["rsi"] == signal.rsi
    assert row["bollinger_bands"]["lower"] == signal.bb_lower
    assert row["A_buy_side"]["buy_l1"]["price"] == signal.buy_l1
    assert row["bars_used"] >= FIB_WINDOW


@pytest.mark.unit
def test_insufficient_history_is_explicit_not_silent_fill() -> None:
    short = _synthetic_bars(50)
    raw = _sample_raw(bars=short)
    payload = us_adapter.compute_policy_table(raw)
    row = payload["rows"][0]
    assert row["insufficient_history"] is True
    assert row["bars_available"] == 50
    assert row["bars_required"] == 120
    assert payload["universe"]["skipped"][0]["reason"] == (
        "insufficient_history_lt_120_sessions"
    )


@pytest.mark.unit
def test_alpaca_account_mode_is_lab_not_default_paper() -> None:
    assert us_adapter.ALPACA_ACCOUNT_MODE == "alpaca_paper_lab"
    payload = us_adapter.compute_policy_table(_sample_raw())
    assert payload["config"]["account_mode"] == "alpaca_paper_lab"
    row = next(r for r in payload["rows"] if not r["insufficient_history"])
    assert row["A_buy_side"]["sizing_band"]["account_mode"] == "alpaca_paper_lab"


@pytest.mark.unit
def test_averaging_levels_use_shared_math_only_when_position_inputs_exist() -> None:
    """A(k) is populated for holdings and explicitly absent without cost inputs."""

    held_payload = us_adapter.compute_policy_table(_sample_raw())
    held_row = next(row for row in held_payload["rows"] if row["symbol"] == "AAPL")
    assert held_payload["config"]["averaging_k_levels"] == [
        Decimal("0.05"),
        Decimal("0.10"),
    ]
    assert set(held_row["A_buy_side"]["averaging_math"]) == {
        "k_5pct",
        "k_10pct",
    }

    raw = _sample_raw()
    unheld_payload = us_adapter.compute_policy_table(
        us_adapter.RawInputs(
            as_of=raw.as_of,
            holdings=[],
            watch_alerts=raw.watch_alerts,
            universe_pool=raw.universe_pool,
            snapshot_partition_date=raw.snapshot_partition_date,
            snapshot_breadth=raw.snapshot_breadth,
            universe_symbols=raw.universe_symbols,
            candles=raw.candles,
            market_cap_fill_stats=raw.market_cap_fill_stats,
        )
    )
    unheld_row = next(row for row in unheld_payload["rows"] if row["symbol"] == "AAPL")
    assert unheld_row["A_buy_side"]["averaging_math"] is None


@pytest.mark.unit
def test_no_order_tool_imports_in_policy_table_tree() -> None:
    """Static AST: scripts/policy_table must not import order placement modules."""

    root = Path("scripts/policy_table")
    forbidden_substrings = (
        "orders",
        "order_execution",
        "place_order",
        "submit_order",
        "DomesticOrderClient",
        "OverseasOrderClient",
    )
    offenders: list[str] = []
    for path in root.rglob("*.py"):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    name = alias.name
                    if any(f in name for f in forbidden_substrings):
                        offenders.append(f"{path}:{name}")
            elif isinstance(node, ast.ImportFrom):
                mod = node.module or ""
                if any(f in mod for f in forbidden_substrings):
                    offenders.append(f"{path}:{mod}")
                for alias in node.names:
                    if any(f in alias.name for f in forbidden_substrings):
                        offenders.append(f"{path}:{mod}.{alias.name}")
    assert offenders == []


@pytest.mark.unit
def test_summary_md_includes_four_labels_and_universe_funnel() -> None:
    payload = us_adapter.compute_policy_table(_sample_raw())
    payload["stamps"] = {
        "policy_table_hash": "sha256:test",
        "auto_trader_head": "deadbeef",
    }
    md = us_adapter.render_summary_md(payload, top_n=10)
    assert "CROSS_MARKET_TRANSFER_UNVALIDATED" in md
    assert "B0_UNVALIDATED" in md
    assert "snapshot_total=" in md
    assert "filter_passed=" in md
    assert "computed=" in md
