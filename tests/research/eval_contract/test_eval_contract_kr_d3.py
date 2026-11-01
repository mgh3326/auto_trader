"""Acceptance 2: the KR adapter reproduces the legacy D3 harness exactly.

Fixtures are the D3 engine's own deterministic ones
(``research.kr_corpus.d3_engine.acceptance._contract_signal_bars`` and the
four-symbol global-rank-cap set); the pinned numbers are the ones the legacy
tests in ``tests/research/kr_corpus/d3_engine/test_d3_engine_core.py`` pin.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal

import pytest

from research.eval_contract.adapters import kr_d3
from research.eval_contract.contract import (
    BRACKET_CONSERVATIVE,
    BRACKET_OPTIMISTIC,
    ContractError,
    DecisionAction,
    Outcome,
    Side,
)
from research.eval_contract.fill_models import (
    ConservativeThrough,
    FillContext,
    FillInputMissing,
    FillOutcome,
    OptimisticTouch,
    VolumeShare,
)
from research.kr_corpus.d3_engine.engine import PortfolioEngine
from research.kr_corpus.d3_engine.models import (
    Arm,
    Bar,
    CashflowView,
    EngineResult,
    Fill,
    OrderClass,
    OrderSide,
    PortfolioRunInput,
)
from tests.research.eval_contract.helpers import (
    LEGACY_TERMINAL_NAV,
    REGISTRY,
    make_run_input,
    ticks,
)


def _same(a: EngineResult, b: EngineResult) -> None:
    assert a.status == b.status
    assert a.events == b.events
    assert a.fills == b.fills
    assert a.terminal_positions == b.terminal_positions
    assert a.metrics == b.metrics
    assert a.evidence == b.evidence


@pytest.mark.parametrize("arm", list(Arm))
def test_legacy_mode_is_the_untouched_engine_with_pinned_results(arm: Arm) -> None:
    run_input = make_run_input(arm)
    direct = PortfolioEngine(ticks()).run(run_input)
    wrapped = kr_d3.run_engine(run_input, ticks(), None)

    _same(wrapped, direct)
    # Pinned legacy values (test_d3_engine_core::test_all_four_arms_...).
    assert [f.price for f in wrapped.fills] == [Decimal(9600), Decimal(9450)]
    assert wrapped.metrics["signals_submitted"] == 2
    assert wrapped.metrics["terminal_nav"] == LEGACY_TERMINAL_NAV


@pytest.mark.parametrize("arm", list(Arm))
def test_optimistic_touch_through_the_hooks_equals_the_legacy_engine(arm: Arm) -> None:
    """The contract's optimistic end *is* the legacy daily fill rule."""

    run_input = make_run_input(arm)
    _same(
        kr_d3.run_engine(run_input, ticks(), OptimisticTouch()),
        PortfolioEngine(ticks()).run(run_input),
    )


def test_global_rank_cap_fixture_is_reproduced() -> None:
    run_input = make_run_input(symbols=("000001", "000002", "000003", "000004"))
    legacy = PortfolioEngine(ticks()).run(run_input)
    optimistic = kr_d3.run_engine(run_input, ticks(), OptimisticTouch())

    _same(optimistic, legacy)
    assert len(legacy.evidence["counterfactual_demand_pairs"]) == 3
    assert len(optimistic.fills) == 6


def test_evaluation_is_deterministic_and_rows_mirror_the_engine() -> None:
    run_input = make_run_input()
    first = kr_d3.evaluate(run_input, ticks(), registry=REGISTRY, code_ref="t")
    second = kr_d3.evaluate(run_input, ticks(), registry=REGISTRY, code_ref="t")
    assert first == second
    assert first.sha256 == second.sha256

    legacy = PortfolioEngine(ticks()).run(run_input)
    for model_id in (kr_d3.LEGACY_MODEL_ID, BRACKET_OPTIMISTIC):
        result = first.result(model_id)
        assert result.terminal_value == LEGACY_TERMINAL_NAV
        assert [
            (r.broker_order_id, r.filled_price, r.filled_qty, r.fee_amount)
            for r in result.fills
        ] == [(f.order_id, f.price, Decimal(f.quantity), f.fee) for f in legacy.fills]
        assert all(r.source == f"simulated:{model_id}" for r in result.fills)
        assert all(r.account_mode == "backtest" for r in result.fills)
        assert len(result.orders) == legacy.metrics["signals_submitted"]
        decision = result.decisions[0]
        assert decision.action is DecisionAction.ENTER
        assert decision.data_cutoff < decision.decided_at
        assert decision.data_cutoff.date() < decision.decided_at.date()


def test_touch_only_bar_separates_the_bracket_ends() -> None:
    """Rung 2 (limit 9450) is touched by the low but not traded through."""

    run_input = make_run_input(last_low=Decimal(9450))
    report = kr_d3.evaluate(run_input, ticks(), registry=REGISTRY, code_ref="t")
    optimistic = report.result(BRACKET_OPTIMISTIC)
    conservative = report.result(BRACKET_CONSERVATIVE)

    assert [r.filled_price for r in optimistic.fills] == [
        Decimal(9600),
        Decimal(9450),
    ]
    # Open 9600 is not one tick through 9600; the low 9450 is through 9600
    # but not through 9450.
    assert [r.filled_price for r in conservative.fills] == [Decimal(9600)]
    assert conservative.fill_rate == Decimal("0.5")
    assert optimistic.fill_rate == Decimal(1)
    assert optimistic.net_vs_cash != conservative.net_vs_cash


def test_gap_open_fills_at_the_open_under_both_ends() -> None:
    bar = Bar(
        date(2014, 1, 2),
        "GAP",
        Decimal(9400),
        Decimal(9500),
        Decimal(9300),
        Decimal(9450),
    )
    for model in (OptimisticTouch(), ConservativeThrough()):
        engine = kr_d3._FillModelEngine(ticks(), model)
        assert engine._buy_fill_price(Decimal(9600), bar) == Decimal(9400)
        assert engine._sell_fill_price(Decimal(9300), bar) == Decimal(9400)


def test_sell_hooks_follow_the_bracket() -> None:
    bar = Bar(
        date(2014, 1, 2),
        "S",
        Decimal(10900),
        Decimal(10960),
        Decimal(10800),
        Decimal(10900),
    )
    legacy = PortfolioEngine._sell_fill_price(Decimal(10960), bar)
    assert (
        kr_d3._FillModelEngine(ticks(), OptimisticTouch())._sell_fill_price(
            Decimal(10960), bar
        )
        == legacy
        == Decimal(10960)
    )
    assert (
        kr_d3._FillModelEngine(ticks(), ConservativeThrough())._sell_fill_price(
            Decimal(10960), bar
        )
        is None
    )


def test_volume_share_and_empirical_are_reported_unavailable_not_guessed() -> None:
    report = kr_d3.evaluate(make_run_input(), ticks(), registry=REGISTRY, code_ref="t")
    volume = report.result("volume_share")
    empirical = report.result("empirical")
    assert volume.status == "unavailable"
    assert "FillInputMissing" in (volume.unavailable_reason or "")
    assert empirical.status == "unavailable"
    assert "EmpiricalModelNotFitted" in (empirical.unavailable_reason or "")
    assert volume.net_vs_cash is None and volume.fills == ()


class _HalfFill:
    model_id = "half_fill"

    def params(self) -> dict[str, object]:
        return {}

    def fill(self, ctx: FillContext) -> FillOutcome:
        return FillOutcome(ctx.quantity / 2, ctx.limit, "partial")


def test_partial_fill_models_are_refused_by_the_harness() -> None:
    bar = Bar(
        date(2014, 1, 2),
        "X",
        Decimal(9600),
        Decimal(9700),
        Decimal(9000),
        Decimal(9650),
    )
    with pytest.raises(kr_d3.HarnessUnsupported, match="full-or-nothing"):
        kr_d3._FillModelEngine(ticks(), _HalfFill())._buy_fill_price(Decimal(9500), bar)
    with pytest.raises(FillInputMissing):
        kr_d3._FillModelEngine(ticks(), VolumeShare())._buy_fill_price(
            Decimal(9500), bar
        )
    result = kr_d3.evaluate_model(
        make_run_input(), ticks(), _HalfFill(), hypothesis_id="H"
    )
    assert result.status == "unavailable"
    assert "HarnessUnsupported" in (result.unavailable_reason or "")


def _extended(*, gap_at: int | None = None) -> PortfolioRunInput:
    """The contract fixture plus 20 later sessions with distinct closes.

    Close on extra session ``i`` (1-based) is ``9900 + 10 * i``. ``gap_at``
    drops the symbol's bar on that session while the market session stays.
    """

    run_input = make_run_input()
    sessions = list(run_input.market_sessions)
    extra_sessions = [sessions[-1] + timedelta(days=i) for i in range(1, 21)]
    extra = tuple(
        Bar(
            s,
            "005930",
            Decimal(9900 + 10 * i),
            Decimal(9950 + 10 * i),
            Decimal(9850 + 10 * i),
            Decimal(9900 + 10 * i),
        )
        for i, s in enumerate(extra_sessions, start=1)
        if i != gap_at
    )
    return replace(
        run_input,
        bars=run_input.bars + extra,
        market_sessions=tuple(sessions + extra_sessions),
        index_closes=run_input.index_closes
        + tuple((s, Decimal(3000 + i)) for i, s in enumerate(extra_sessions)),
    )


def _first_buy_outcomes(run_input: PortfolioRunInput) -> dict[int, Outcome]:
    report = kr_d3.evaluate(run_input, ticks(), registry=REGISTRY, code_ref="t")
    result = report.result(BRACKET_OPTIMISTIC)
    first = result.fills[0]
    assert first.side is Side.BUY and first.filled_price == Decimal(9600)
    return {
        o.horizon: o for o in result.outcomes if o.order_id == first.broker_order_id
    }


def test_markouts_read_the_close_h_sessions_after_the_fill() -> None:
    outcomes = _first_buy_outcomes(_extended())
    assert set(outcomes) == {1, 5, 20}
    fee = Decimal("0.00215")
    for h in (1, 5, 20):
        close = Decimal(9900 + 10 * h)
        net = close * (1 - fee) / (Decimal(9600) * (1 + fee)) - 1
        assert outcomes[h].censored is False
        assert outcomes[h].gross_return == close / Decimal(9600) - 1
        assert outcomes[h].net_return == net
        assert outcomes[h].cash_return == 0
        assert outcomes[h].net_vs_cash == net


def test_markouts_count_sessions_and_censor_a_missing_bar() -> None:
    outcomes = _first_buy_outcomes(_extended(gap_at=5))
    assert outcomes[5].censored is True and outcomes[5].net_return is None
    # R_20 still reads session +20, not the 20th bar after the fill.
    assert outcomes[20].gross_return == Decimal(10100) / Decimal(9600) - 1
    assert outcomes[1].gross_return == Decimal(9910) / Decimal(9600) - 1


def test_a_stray_bar_off_the_market_calendar_does_not_shift_horizons() -> None:
    """Bars are looked up for closes only; market_sessions is the calendar."""

    run_input = _extended()
    result = PortfolioEngine(ticks()).run(run_input)
    stray = sorted(run_input.market_sessions)[-18]  # extra session 3
    off_calendar = replace(
        run_input,
        market_sessions=tuple(d for d in run_input.market_sessions if d != stray),
    )
    rows = kr_d3.translate(
        off_calendar, result, hypothesis_id="H", fill_model_id="legacy_d3_engine"
    )
    first = result.fills[0]
    outcomes = {o.horizon: o for o in rows.outcomes if o.order_id == first.order_id}
    # Calendar without extra session 3: +1 -> extra 1, +5 -> extra 6.
    assert outcomes[1].gross_return == Decimal(9910) / Decimal(9600) - 1
    assert outcomes[5].gross_return == Decimal(9960) / Decimal(9600) - 1
    assert outcomes[20].censored is True  # only 19 market sessions follow


def test_time_trim_fills_become_marked_exit_orders() -> None:
    """C3 time trims fill at the open with no order_submitted event."""

    run_input = make_run_input(Arm.C3)
    result = PortfolioEngine(ticks()).run(run_input)
    session = result.fills[-1].session
    trim = Fill(
        order_id=f"D3-{session.isoformat()}-TIME-005930-90",
        session=session,
        symbol="005930",
        side=OrderSide.SELL,
        order_class=OrderClass.TIME_TRIM,
        quantity=10,
        price=Decimal(9600),
        gross=Decimal(96000),
        fee=Decimal("206.40"),
    )
    rows = kr_d3.translate(
        run_input,
        replace(result, fills=result.fills + (trim,)),
        hypothesis_id="H",
        fill_model_id="legacy_d3_engine",
    )
    spec = next(o for o in rows.orders if o.order_id == trim.order_id)
    assert spec.origin == "engine_time_trim" and spec.side is Side.SELL
    assert spec.limit_price == Decimal(9600) and spec.quantity == Decimal(10)
    exit_decision = next(d for d in rows.decisions if d.decision_id == spec.intent_id)
    assert exit_decision.action is DecisionAction.EXIT
    assert rows.fills[-1].correlation_id == spec.intent_id
    unknown = replace(trim, order_id="X", order_class=OrderClass.NEW)
    with pytest.raises(ValueError, match="no submitted order"):
        kr_d3.translate(
            run_input,
            replace(result, fills=result.fills + (unknown,)),
            hypothesis_id="H",
            fill_model_id="legacy_d3_engine",
        )


def test_conservative_buy_at_a_band_boundary_uses_the_lower_tick() -> None:
    """Through 5000 is 4995 (tick 5 below 5000), not 4990."""

    bar = Bar(
        date(2014, 1, 2),
        "B",
        Decimal(5100),
        Decimal(5200),
        Decimal(4995),
        Decimal(5050),
    )
    engine = kr_d3._FillModelEngine(ticks(), ConservativeThrough())
    assert engine._buy_fill_price(Decimal(5000), bar) == Decimal(5000)
    assert engine._buy_fill_price(Decimal(4995), bar) is None
    # Sells go through upwards: band of the limit itself.
    up = Bar(
        date(2014, 1, 2),
        "S",
        Decimal(4900),
        Decimal(5010),
        Decimal(4800),
        Decimal(4950),
    )
    assert engine._sell_fill_price(Decimal(5000), up) == Decimal(5000)
    assert engine._sell_fill_price(Decimal(5005), up) is None


def test_with_contribution_is_refused_for_equal_capital() -> None:
    run_input = replace(make_run_input(), cashflow_view=CashflowView.WITH_CONTRIBUTION)
    with pytest.raises(ValueError, match="equal capital"):
        kr_d3.evaluate(run_input, ticks(), registry=REGISTRY, code_ref="t")


def test_registry_must_match_every_decision() -> None:
    report = kr_d3.evaluate(make_run_input(), ticks(), registry=REGISTRY, code_ref="t")
    assert all(
        d.hypothesis_id == REGISTRY.hypothesis_id
        for r in report.results
        for d in r.decisions
    )
    with pytest.raises(ContractError, match="registry entry"):
        replace(report, hypothesis_id="H-other")
