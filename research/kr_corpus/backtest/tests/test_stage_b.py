from __future__ import annotations

from dataclasses import replace
from datetime import date, timedelta

import pytest
from evidence import write_stage_b_evidence
from holdout_guard import HoldoutDateBlocked
from pit import Bar, LookaheadViolation
from stage_b import build_run_contract, run_stage_b


def _bars() -> list[Bar]:
    start = date(2024, 1, 2)
    closes = [100] * 20 + [101, 102, 103, 104, 105, 106]
    volumes = [100] * 20 + [120, 100, 100, 100, 100, 100]
    return [
        Bar(
            symbol="005930",
            session_date=start + timedelta(days=index),
            open=close,
            high=close,
            low=close,
            close=close,
            volume=volume,
            trading_value=None,
            market="KOSPI",
            price_mode="adjusted",
            source_product="fixture",
        )
        for index, (close, volume) in enumerate(zip(closes, volumes, strict=True))
    ]


def test_stage_b_uses_t_plus_one_open_and_d_plus_five_close() -> None:
    contract = build_run_contract(
        cost_profile="43bp",
        window_start=date(2024, 1, 2),
        window_end=date(2024, 1, 31),
    )
    result = run_stage_b(bars=_bars(), contract=contract)

    assert len(result.trades) == 1
    trade = result.trades[0]
    assert trade.signal_session == date(2024, 1, 22)
    assert trade.entry_session == date(2024, 1, 23)
    assert trade.exit_session == date(2024, 1, 27)
    assert trade.entry_open == 102
    assert trade.exit_close == 106
    assert trade.cost_bp == 43
    assert result.lookahead_checks == len(_bars())


def test_cost_contract_is_explicit_and_no_contract_fails() -> None:
    assert (
        build_run_contract(
            cost_profile="43bp",
            window_start=date(2024, 1, 2),
            window_end=date(2024, 1, 31),
        ).cost.round_trip_bp
        == 43
    )
    assert (
        build_run_contract(
            cost_profile="83bp",
            window_start=date(2024, 1, 2),
            window_end=date(2024, 1, 31),
        ).cost.round_trip_bp
        == 83
    )
    with pytest.raises(ValueError, match="explicit Stage-B run contract"):
        run_stage_b(bars=_bars(), contract=None)  # type: ignore[arg-type]


def test_d5_session_gap_fails_closed_with_explicit_reason() -> None:
    bars = _bars()
    # Synthetic 300-day suspension after entry; index+5 must not become D+5.
    bars = [
        replace(bar, session_date=bar.session_date + timedelta(days=300))
        if index >= 22
        else bar
        for index, bar in enumerate(bars)
    ]
    contract = build_run_contract(
        cost_profile="43bp",
        window_start=date(2024, 1, 2),
        window_end=date(2024, 12, 31),
    )
    result = run_stage_b(bars=bars, contract=contract)
    assert result.trades == ()
    assert "session_gap_before_d5_exit" in result.skipped_signal_reasons
    assert result.to_dict()["pit_boundary_checked"] is True
    assert "lookahead_checks" not in result.to_dict()


def test_stage_b_contract_rejects_holdout_window() -> None:
    with pytest.raises(HoldoutDateBlocked):
        build_run_contract(
            cost_profile="43bp",
            window_start=date(2025, 1, 1),
            window_end=date(2025, 1, 5),
        )


def test_trial_evidence_writer_is_rejudgable(tmp_path) -> None:
    contract = build_run_contract(
        cost_profile="43bp",
        window_start=date(2024, 1, 2),
        window_end=date(2024, 1, 31),
    )
    # Duplicate a second independent symbol to satisfy the canonical trial sample floor.
    bars = _bars() + [
        bar.__class__(**{**bar.__dict__, "symbol": "000660"}) for bar in _bars()
    ]
    result = run_stage_b(bars=bars, contract=contract)
    payload = write_stage_b_evidence(tmp_path / "stageb.json", result)
    assert payload["track"] == "strategy_backtest"
    assert payload["acceptance_track_separate"] is True
    assert payload["trial_evidence"]["schema_version"] == "honest_trial.v3"
    assert payload["signal_contract_hash"] == contract.signal_contract_hash
    assert payload["trial_evidence"]["execution_cost"] == {
        "fee_bps": 3.0,
        "transaction_tax_bps": 20.0,
        "half_spread_bps": 0.0,
        "slippage_bps": 20.0,
    }
    assert payload["trial_evidence"]["sharpe_method"] == "pooled_sample_sharpe"
    assert payload["trial_evidence"]["p_value_method"] == "not_computed"
    assert (
        payload["trial_evidence"]["selection_score_method"]
        == "arithmetic_mean_net_return"
    )


def test_lookahead_guard_remains_hard_error() -> None:
    future = _bars()[-1]
    with pytest.raises(LookaheadViolation):
        from pit import assert_no_lookahead

        assert_no_lookahead([future], date(2024, 1, 2))
