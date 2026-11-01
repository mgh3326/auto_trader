"""Labels, policies, the contract simulator and the gate, on fixture bars."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, date, datetime
from decimal import Decimal
from types import SimpleNamespace

import numpy as np
import pytest

from research.eval_contract.adapters.kr_d3 import _daily_context
from research.eval_contract.contract import (
    BRACKET_CONSERVATIVE,
    BRACKET_OPTIMISTIC,
    EvaluationReport,
    MissingRegistryReference,
    Provenance,
    RegistryRef,
    Side,
)
from research.eval_contract.fill_models import (
    ConservativeThrough,
    EmpiricalFillModel,
    OptimisticTouch,
    VolumeShare,
)
from research.eval_contract.report import render_report
from research.kr_boosting import train
from research.kr_boosting.eligibility import Signal
from research.kr_boosting.features import compute_features
from research.kr_boosting.gate import (
    CONTINUE,
    STOP,
    bootstrap_p,
    decide,
    holm,
    max_drawdown,
)
from research.kr_boosting.labels import admit_label, touch_label, xret_label
from research.kr_boosting.panel import build_panel
from research.kr_boosting.payout_exposure import (
    FlaggedSymbols,
    exposure,
    exposure_table,
    load_flags,
)
from research.kr_boosting.policies import (
    Candidate,
    LadderRow,
    admit_all_plan,
    gbm_admit_plan,
    gbm_entry_plan,
    krx_ticks,
    ladder,
    mean_reversion_plan,
    momentum_plan,
)
from research.kr_boosting.simulator import (
    WRITE_OFF_SEMANTICS,
    daily_context,
    evaluation_report,
    fill_models,
    simulate,
)
from research.kr_corpus.d3_engine.models import Bar
from tests.research.kr_boosting.helpers import flat_rows, frame, row, weekdays

SESSIONS = weekdays(date(2019, 1, 1), 40)
TICKS = krx_ticks()


def _candidate(
    li: int, t: int, limit: str, *, rest: int = 1, hold: int = 5, from_fill=False
):
    return Candidate(li, t, Decimal(limit), rest, hold, from_fill)


def _run(panel, plan, model, **kwargs):
    return simulate(
        panel,
        plan,
        model,
        ticks=TICKS,
        hypothesis_id="H-admit",
        first_session=1,
        last_session=len(panel.calendar) - 1,
        **kwargs,
    )


# ------------------------------------------------------------------- labels


def test_admit_label_runs_from_next_open_to_fifth_close() -> None:
    rows = flat_rows("000001", SESSIONS)
    rows[11] = row("000001", SESSIONS[11], 10_000, 10_100, 9_900, 10_000)
    rows[15] = row("000001", SESSIONS[15], 11_000, 11_100, 10_900, 11_000)
    panel = build_panel(frame(rows))
    assert admit_label(panel, 0, 10) == pytest.approx(0.10)


def test_admit_label_is_unknown_when_a_price_is_missing() -> None:
    rows = flat_rows("000001", SESSIONS)
    rows[15] = row("000001", SESSIONS[15], 0, 0, 0, 0, state="invalid_ohlc")
    panel = build_panel(frame(rows))
    assert np.isnan(admit_label(panel, 0, 10))


def _terminal_rows(last: int = 13):
    rows = flat_rows("000001", SESSIONS[: last + 1])
    rows[last] = row(
        "000001",
        SESSIONS[last],
        8_000,
        8_100,
        7_900,
        8_000,
        terminal=True,
        payout="residual_after_liquidation_trading",
        terminal_at=datetime(2019, 1, 18, 15, 0, tzinfo=UTC),
    )
    return rows + flat_rows("000002", SESSIONS)


def test_terminal_listing_exits_at_last_close_and_is_not_censored() -> None:
    panel = build_panel(frame(_terminal_rows()))
    assert admit_label(panel, 0, 10) == pytest.approx(8_000 / 10_000 - 1)


def test_touch_is_unknown_on_an_unobserved_session() -> None:
    rows = flat_rows("000001", SESSIONS)
    rows[12] = row("000001", SESSIONS[12], 0, 0, 0, 0, state="invalid_ohlc")
    panel = build_panel(frame(rows))
    touch = touch_label(panel, 0, 10, 9_000.0)
    assert np.isnan(touch.touched)


def test_touch_gaps_at_the_open_and_ignores_no_trade_days() -> None:
    rows = flat_rows("000001", SESSIONS)
    rows[11] = row("000001", SESSIONS[11], 0, 0, 0, 10_000, state="no_trade")
    rows[12] = row("000001", SESSIONS[12], 9_000, 9_100, 8_900, 9_050)
    panel = build_panel(frame(rows))
    touch = touch_label(panel, 0, 10, 9_500.0)
    assert (touch.touched, touch.fill_idx, touch.entry) == (1.0, 12, 9_000.0)


def test_untouched_window_ended_by_a_terminal_event_is_zero() -> None:
    panel = build_panel(frame(_terminal_rows(last=13)))
    assert touch_label(panel, 0, 10, 1_000.0).touched == 0.0


def test_window_running_past_the_data_is_unknown() -> None:
    panel = build_panel(frame(flat_rows("000001", SESSIONS)))
    assert np.isnan(touch_label(panel, 0, len(SESSIONS) - 3, 1.0).touched)


def test_xret_subtracts_the_market_over_the_hold() -> None:
    sessions = weekdays(date(2019, 1, 1), 60)
    rows = flat_rows("000001", sessions)
    rows[12] = row("000001", sessions[12], 9_000, 9_100, 8_900, 9_000)
    rows[32] = row("000001", sessions[32], 9_900, 9_950, 9_850, 9_900)
    panel = build_panel(frame(rows))
    touch = touch_label(panel, 0, 10, 9_500.0)
    market = np.zeros(len(sessions))
    market[20] = np.log(1.05)
    assert xret_label(panel, 0, touch, market) == pytest.approx(
        9_900 / 9_000 - 1 - 0.05
    )


# ---------------------------------------------------------------- simulator


def test_daily_context_matches_the_contract_adapter() -> None:
    bar = Bar(
        SESSIONS[0], "000001", Decimal(100), Decimal(110), Decimal(90), Decimal(105)
    )
    ours = daily_context(
        Side.BUY, Decimal(95), 1, Decimal(1), open_price=100, extreme=90, volume=0
    )
    theirs = _daily_context(Side.BUY, Decimal(95), bar, Decimal(1))
    for name in (
        "limit",
        "tick",
        "optimistic_extreme",
        "conservative_extreme",
        "first_price",
    ):
        assert getattr(ours, name) == getattr(theirs, name)


def _touch_panel(low: float = 9_500.0):
    rows = flat_rows("000001", SESSIONS)
    rows[11] = row("000001", SESSIONS[11], 10_000, 10_050, low, 10_000)
    return build_panel(frame(rows))


def test_bracket_separates_a_touch_from_a_trade_through() -> None:
    panel = _touch_panel()
    plan = {10: [_candidate(0, 10, "9500")]}
    optimistic = _run(panel, plan, OptimisticTouch())
    conservative = _run(panel, plan, ConservativeThrough())
    assert len([f for f in optimistic.result.fills if f.side is Side.BUY]) == 1
    assert [f for f in conservative.result.fills if f.side is Side.BUY] == []
    assert conservative.result.net_vs_cash == 0


def test_trade_through_fills_conservative_too() -> None:
    panel = _touch_panel(low=9_400.0)
    out = _run(panel, {10: [_candidate(0, 10, "9500")]}, ConservativeThrough())
    assert [f.filled_price for f in out.result.fills if f.side is Side.BUY] == [
        Decimal(9500)
    ]


def test_gap_fills_at_the_open_under_both_ends() -> None:
    rows = flat_rows("000001", SESSIONS)
    rows[11] = row("000001", SESSIONS[11], 9_000, 9_100, 8_900, 9_000)
    panel = build_panel(frame(rows))
    plan = {10: [_candidate(0, 10, "9500")]}
    for model in (OptimisticTouch(), ConservativeThrough()):
        out = _run(panel, plan, model)
        assert [f.filled_price for f in out.result.fills if f.side is Side.BUY] == [
            Decimal(9000)
        ]


def test_volume_share_caps_a_buy_at_participation() -> None:
    rows = flat_rows("000001", SESSIONS)
    rows[11] = row("000001", SESSIONS[11], 10_000, 10_050, 9_950, 10_000, 100.0)
    panel = build_panel(frame(rows))
    out = _run(panel, {10: [_candidate(0, 10, "10000")]}, VolumeShare(Decimal("0.1")))
    buys = [f for f in out.result.fills if f.side is Side.BUY]
    assert [f.filled_qty for f in buys] == [Decimal(10)]  # 30 wanted, 100 x 0.1 traded


def test_empirical_is_reported_unavailable_with_no_numbers() -> None:
    panel = _touch_panel()
    out = _run(panel, {10: [_candidate(0, 10, "9500")]}, EmpiricalFillModel())
    assert out.result.status == "unavailable"
    assert out.result.net_vs_cash is None


def test_position_exits_at_the_planned_close_and_pays_both_fees() -> None:
    rows = flat_rows("000001", SESSIONS)
    rows[15] = row("000001", SESSIONS[15], 10_000, 11_100, 9_900, 11_000)
    panel = build_panel(frame(rows))
    out = _run(panel, {10: [_candidate(0, 10, "10000")]}, OptimisticTouch())
    buy, sell = out.result.fills
    assert buy.side is Side.BUY and sell.side is Side.SELL
    assert sell.filled_price == Decimal(11000)
    assert sell.filled_at.date() == SESSIONS[15]
    qty = Decimal(30)
    expected = (
        Decimal("10000000")
        - qty * 10_000 * Decimal("1.00215")
        + qty * 11_000 * Decimal("0.99785")
    )
    assert out.result.terminal_value == expected


def test_outcomes_censor_a_horizon_past_the_data() -> None:
    sessions = weekdays(date(2019, 1, 1), 14)
    panel = build_panel(frame(flat_rows("000001", sessions)))
    out = _run(panel, {2: [_candidate(0, 2, "10000")]}, OptimisticTouch())
    by_h = {o.horizon: o for o in out.result.outcomes}
    assert not by_h[5].censored
    assert by_h[20].censored and by_h[20].net_return is None


def test_capacity_and_one_position_per_listing_hold_for_every_policy() -> None:
    rows: list[dict[str, object]] = []
    for i in range(6):
        rows += flat_rows(f"00000{i}", SESSIONS)
    panel = build_panel(frame(rows))
    plan = {10: [_candidate(i, 10, "10000") for i in range(6)]}
    plan[11] = [_candidate(0, 11, "10000")]  # already held
    out = _run(panel, plan, OptimisticTouch())
    buys = [f for f in out.result.fills if f.side is Side.BUY]
    assert len(buys) == 3


def test_resting_order_is_one_day_child_per_session() -> None:
    panel = _touch_panel(low=9_950.0)
    plan = {
        10: [
            Candidate(
                0, 10, Decimal(9000), rest_sessions=5, hold=20, hold_from_fill=True
            )
        ]
    }
    out = _run(panel, plan, OptimisticTouch())
    assert len(out.result.orders) == 5
    assert len(out.result.decisions) == 1
    assert out.result.fill_rate == 0


def test_entry_skipped_for_cash_expires_with_its_rest_window() -> None:
    """A day with no cash uses up the window; the entry must not wake up later."""

    rows: list[dict[str, object]] = []
    for i in range(2):
        rows += flat_rows(f"00000{i}", SESSIONS)
    panel = build_panel(frame(rows))
    # One order's worth of cash: listing 0 takes it and returns it at the next close.
    plan = {10: [_candidate(0, 10, "10000", hold=1), _candidate(1, 10, "10000")]}
    out = _run(panel, plan, OptimisticTouch(), capital=Decimal(310_000))
    assert {o.symbol for o in out.result.orders} == {panel.symbols[0]}
    assert out.result.intents[1].intent_id.endswith(panel.symbols[1])


def test_entry_skipped_for_cash_can_still_fill_inside_its_window() -> None:
    rows: list[dict[str, object]] = []
    for i in range(2):
        rows += flat_rows(f"00000{i}", SESSIONS)
    panel = build_panel(frame(rows))
    plan = {
        10: [
            _candidate(0, 10, "10000", hold=1),
            _candidate(1, 10, "10000", rest=3),
        ]
    }
    out = _run(panel, plan, OptimisticTouch(), capital=Decimal(310_000))
    buys = [
        o
        for o in out.result.orders
        if o.symbol == panel.symbols[1] and o.side is Side.BUY
    ]
    assert [o.submitted_at.date() for o in buys] == [SESSIONS[12]]


def test_terminal_position_settles_at_last_close_on_the_next_session() -> None:
    panel = build_panel(frame(_terminal_rows(last=13)))
    plan = {10: [_candidate(0, 10, "10000", hold=20)]}
    out = _run(panel, plan, OptimisticTouch())
    assert out.writeoffs == 0
    sell = [f for f in out.result.fills if f.side is Side.SELL]
    assert [f.filled_price for f in sell] == [Decimal(8000)]
    assert sell[0].filled_at.date() == SESSIONS[14]


def test_terminal_row_without_a_close_settles_at_the_last_mark() -> None:
    rows = flat_rows("000001", SESSIONS[:14])
    rows[13] = row(
        "000001",
        SESSIONS[13],
        0,
        0,
        0,
        0,
        state="invalid_ohlc",
        terminal=True,
        payout="ambiguous",
        terminal_at=datetime(2019, 1, 18, 15, 0, tzinfo=UTC),
    )
    panel = build_panel(frame(rows + flat_rows("000002", SESSIONS)))
    out = _run(panel, {10: [_candidate(0, 10, "10000", hold=20)]}, OptimisticTouch())
    sell = [f for f in out.result.fills if f.side is Side.SELL]
    assert [f.filled_price for f in sell] == [Decimal(10000)]


def test_writeoff_stress_loses_the_whole_position_and_reports_it() -> None:
    panel = build_panel(frame(_terminal_rows(last=13)))
    plan = {10: [_candidate(0, 10, "10000", hold=20)]}
    base = _run(panel, plan, OptimisticTouch())
    stress = _run(panel, plan, OptimisticTouch(), terminal_writeoff=WRITE_OFF_SEMANTICS)
    assert stress.writeoffs == 1
    assert stress.result.terminal_value < base.result.terminal_value
    assert "terminal_writeoffs=1" in stress.result.notes


def test_halted_exit_waits_for_the_next_valid_bar() -> None:
    rows = flat_rows("000001", SESSIONS)
    rows[15] = row("000001", SESSIONS[15], 0, 0, 0, 10_000, state="no_trade")
    panel = build_panel(frame(rows))
    out = _run(panel, {10: [_candidate(0, 10, "10000")]}, OptimisticTouch())
    sell = [f for f in out.result.fills if f.side is Side.SELL]
    assert sell[0].filled_at.date() == SESSIONS[16]


def test_contract_report_needs_a_registry_reference_and_both_ends() -> None:
    panel = _touch_panel()
    plan = {10: [_candidate(0, 10, "9500")]}
    outputs = {m.model_id: _run(panel, plan, m) for m in fill_models()}
    registry = RegistryRef("H-admit", "ab" * 32, 1)
    report = evaluation_report(
        hypothesis_id="H-admit",
        outputs=outputs,
        registry=registry,
        code_ref="fixture",
        data_manifest_sha256="cd" * 32,
        config={"policy": "fixture"},
    )
    assert isinstance(report, EvaluationReport)
    assert report.result(BRACKET_OPTIMISTIC).status == "ok"
    assert report.result(BRACKET_CONSERVATIVE).status == "ok"
    assert report.result("volume_share").status == "ok"
    assert report.result("empirical").status == "unavailable"
    text = render_report(report)
    assert "k=1" in text
    with pytest.raises(MissingRegistryReference):
        Provenance("fixture", "cd" * 32, "ef" * 32, None)


# ----------------------------------------------------------------- policies


def _signal(li: int, t: int, rsi: float, close: int = 10_000) -> Signal:
    return Signal(li, t, rsi, Decimal(close) * Decimal("0.95"), -0.05, None)


def _panel_of(count: int):
    rows: list[dict[str, object]] = []
    for i in range(count):
        rows += flat_rows(f"00000{i}", SESSIONS)
    return build_panel(frame(rows))


def test_unchanged_policy_ranks_by_d3_order() -> None:
    panel = _panel_of(3)
    signals = [_signal(0, 10, 44.0), _signal(1, 10, 30.0), _signal(2, 10, 40.0)]
    plan = admit_all_plan(panel, TICKS, signals)
    assert [c.li for c in plan[10]] == [1, 2, 0]
    assert plan[10][0].rest_sessions == 1


def test_admit_rule_needs_both_quantiles() -> None:
    panel = _panel_of(3)
    signals = [_signal(0, 10, 40.0), _signal(1, 10, 40.0), _signal(2, 10, 40.0)]
    q10 = np.array([-0.05, -0.20, -0.05])
    q50 = np.array([0.02, 0.05, 0.001])  # 2 fails the tail, 3 fails the round trip
    plan = gbm_admit_plan(panel, TICKS, signals, q10, q50)
    assert [c.li for c in plan[10]] == [0]


def test_entry_rule_picks_best_rung_and_skips_negative_scores() -> None:
    panel = _panel_of(2)
    signals = [_signal(0, 10, 40.0), _signal(1, 10, 40.0)]
    rows = [
        LadderRow(0, "near_0p5", Decimal(9950)),
        LadderRow(0, "l1_3", Decimal(9700)),
        LadderRow(1, "near_0p5", Decimal(9950)),
    ]
    p = np.array([0.9, 0.4, 0.9])
    x = np.array([0.01, 0.10, -0.01])
    plan = gbm_entry_plan(panel, TICKS, signals, rows, p, x)
    assert [(c.li, c.limit) for c in plan[10]] == [(0, Decimal(9700))]
    assert plan[10][0].hold_from_fill and plan[10][0].rest_sessions == 5


def test_ladder_collapses_duplicate_prices_after_the_tick_floor() -> None:
    panel = _panel_of(1)
    signal = Signal(0, 10, 40.0, Decimal("9702"), -0.03, Decimal("9500.5"))
    kinds = dict(ladder(panel, TICKS, signal))
    assert kinds["near_0p5"] == Decimal(9950)
    assert kinds["fib_nearest"] == Decimal(9500)
    assert "anchor_l2" not in kinds  # 9702 floors onto L1 at 9700


def _control_panel():
    sessions = weekdays(date(2019, 1, 1), 90)
    rows: list[dict[str, object]] = []
    for i, step in enumerate((0.01, 0.0, -0.01)):
        price = 10_000.0
        for d in sessions:
            price *= 1 + step
            p = round(price, -1)
            rows.append(row(f"00000{i}", d, p, p * 1.01, p * 0.99, p, 100_000.0))
    # Too thin to trade: must not rank even with the best momentum.
    price = 10_000.0
    for d in sessions:
        price *= 1.02
        p = round(price, -1)
        rows.append(row("000009", d, p, p * 1.01, p * 0.99, p, 1.0))
    return build_panel(frame(rows))


def test_frozen_controls_use_the_liquid_universe_and_stated_ranks() -> None:
    panel = _control_panel()
    store = compute_features(panel)
    t = 80
    momentum = momentum_plan(panel, TICKS, store, [t], "admit")
    reversal = mean_reversion_plan(panel, TICKS, store, [t], "admit")
    assert [panel.symbols[c.li] for c in momentum[t]] == ["000000", "000001", "000002"]
    assert [panel.symbols[c.li] for c in reversal[t]] == ["000002", "000001", "000000"]


# --------------------------------------------------------------------- gate


def _nav(daily: float, n: int = 400, capital: float = 1.0) -> np.ndarray:
    return capital + np.cumsum(np.full(n, daily))


def _verdict(gbm: float, control: float, optimistic: float | None = None):
    n = 400
    years = [2019 + i // 100 for i in range(n)]
    rng = np.random.default_rng(0)
    noise = rng.normal(0, 1e-4, n)
    gbm_nav = _nav(gbm) + np.cumsum(noise)
    return decide(
        "H-admit",
        capital=1.0,
        gbm_conservative=gbm_nav,
        gbm_optimistic=_nav(gbm if optimistic is None else optimistic),
        controls_conservative={
            "control-unchanged": _nav(control),
            "control-momentum": _nav(control),
            "control-cash": np.ones(n),
        },
        unchanged="control-unchanged",
        years=years,
    )


def test_gate_continues_when_every_condition_holds() -> None:
    verdict = _verdict(gbm=0.001, control=0.0)
    assert verdict.verdict == CONTINUE
    assert verdict.g1_net_vs_cash and verdict.g2_controls
    assert verdict.g3_stability and verdict.g4_bracket


def test_gate_stops_when_a_control_is_not_beaten() -> None:
    verdict = _verdict(gbm=0.001, control=0.001)
    assert verdict.verdict == STOP
    assert not verdict.g2_controls


def test_gate_stops_when_net_vs_cash_is_negative() -> None:
    verdict = _verdict(gbm=-0.001, control=-0.002)
    assert verdict.verdict == STOP
    assert not verdict.g1_net_vs_cash


def test_gate_stops_on_a_negative_optimistic_end() -> None:
    verdict = _verdict(gbm=0.001, control=0.0, optimistic=-0.001)
    assert verdict.verdict == STOP
    assert not verdict.g4_bracket


def test_gate_stops_when_only_one_year_carries_the_result() -> None:
    n = 400
    years = [2019 + i // 100 for i in range(n)]
    daily = np.zeros(n)
    daily[:100] = 0.01
    nav = 1.0 + np.cumsum(daily)
    verdict = decide(
        "H-admit",
        capital=1.0,
        gbm_conservative=nav,
        gbm_optimistic=nav,
        controls_conservative={"control-unchanged": np.ones(n)},
        unchanged="control-unchanged",
        years=years,
    )
    assert verdict.verdict == STOP
    assert not verdict.g3_stability


def test_holm_steps_down_and_stays_monotonic() -> None:
    adjusted = holm({"a": 0.01, "b": 0.04, "c": 0.03})
    assert adjusted == pytest.approx({"a": 0.03, "c": 0.06, "b": 0.06})


def test_bootstrap_is_deterministic_and_separates_signal_from_noise() -> None:
    rng = np.random.default_rng(1)
    noise = rng.normal(0, 1e-3, 1000)
    assert bootstrap_p(noise + 1e-3) == bootstrap_p(noise + 1e-3)
    assert bootstrap_p(noise + 1e-3) < 0.01
    assert bootstrap_p(noise - 1e-3) > 0.99


def test_max_drawdown() -> None:
    assert max_drawdown(np.array([100.0, 120.0, 90.0, 110.0])) == pytest.approx(-0.25)


# -------------------------------------------------------------------- fit cache


def test_fit_cache_reuses_identical_fits_and_misses_on_changed_input(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    calls: list[int] = []

    def fake_fit(family, cfg, seed, x, y):
        calls.append(seed)
        return train.Fitted((f"model-{seed}-{y.sum()}",))

    monkeypatch.setattr(train, "_fit", fake_fit)
    monkeypatch.setattr(train, "_lgb", lambda: SimpleNamespace(__version__="fixture"))
    x = np.ones((4, 2), dtype=np.float32)
    y = np.arange(4, dtype=np.float64)
    cfg = {"num_leaves": 15}

    first = train.fit("touch", cfg, 1, x, y, cache_dir=tmp_path)
    again = train.fit("touch", cfg, 1, x, y, cache_dir=tmp_path)
    assert again == first
    assert calls == [1]

    train.fit("touch", cfg, 2, x, y, cache_dir=tmp_path)
    train.fit("touch", cfg, 1, x, y + 1, cache_dir=tmp_path)
    train.fit("touch", {"num_leaves": 31}, 1, x, y, cache_dir=tmp_path)
    assert len(calls) == 4
    assert not list(tmp_path.glob("*.tmp"))


# -------------------------------------------------------------- payout exposure


def _flags(*dissolved: str) -> FlaggedSymbols:
    empty: frozenset[str] = frozenset()
    return FlaggedSymbols(empty, empty, frozenset(dissolved), empty)


def test_exposure_counts_terminal_settlements_and_splits_the_flagged_ones() -> None:
    panel = build_panel(frame(_terminal_rows(last=13)))
    plan = {10: [_candidate(0, 10, "10000", hold=20)]}
    result = _run(panel, plan, OptimisticTouch()).result
    symbol = panel.symbols[0]

    clean = exposure(result, _flags("999999"))
    assert clean.settlements == 1
    assert clean.settled_value == Decimal(8000) * 30
    assert clean.flagged_settlements == 0

    flagged = exposure(result, _flags(symbol))
    assert flagged.flagged_value == flagged.settled_value
    assert flagged.flagged_symbols == (symbol,)
    assert flagged.bound_annual(Decimal(240_000), 2.0) == pytest.approx(0.5)


def test_exposure_ignores_a_planned_exit() -> None:
    panel = build_panel(frame(flat_rows("000001", SESSIONS)))
    plan = {10: [_candidate(0, 10, "10000", hold=2)]}
    result = _run(panel, plan, OptimisticTouch()).result
    assert any(f.side is Side.SELL for f in result.fills)
    assert exposure(result, _flags()).settlements == 0


def test_exposure_table_has_a_row_per_policy() -> None:
    panel = build_panel(frame(_terminal_rows(last=13)))
    plan = {10: [_candidate(0, 10, "10000", hold=20)]}
    result = _run(panel, plan, OptimisticTouch()).result
    lines = exposure_table(
        {"a": {"m": result}},
        order=("a",),
        model_id="m",
        flags=_flags(panel.symbols[0]),
        capital=Decimal(240_000),
        years=2.0,
    )
    assert len(lines) == 3
    assert "-50.00%" in lines[2]
    assert panel.symbols[0] in lines[2]


def test_flags_read_the_audit_caveats_from_a_hash_checked_table(tmp_path) -> None:
    table = tmp_path / "delist_resolution.v1.csv"
    table.write_text(
        "symbol,delist_reason\n"
        "000010,해산 사유 발생\n"
        "000020,감사의견 거절\n"
        "000030,합병\n"
        "000030,감사의견 거절\n"
    )
    digest = hashlib.sha256(table.read_bytes()).hexdigest()
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps({"files": {"delist_resolution": {"sha256": digest}}})
    )

    flags = load_flags(tmp_path)
    assert flags.dissolution_label == {"000010"}
    assert flags.same_code_relisting == {"000030"}
    assert {"003415", "002250"} <= flags.all
    assert "000020" not in flags.all

    manifest.write_text(json.dumps({"files": {"delist_resolution": {"sha256": "00"}}}))
    with pytest.raises(ValueError, match="sha256"):
        load_flags(tmp_path)
