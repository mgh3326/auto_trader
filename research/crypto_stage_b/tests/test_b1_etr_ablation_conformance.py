from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import date, timedelta

import pytest

from research.crypto_stage_b.contracts import CryptoStageBRunContract
from research.crypto_stage_b.engine import run_execution_arm
from research.crypto_stage_b.signals import evaluate_signal
from research.crypto_stage_b.source import DailyBar, InMemoryDailyBarSource
from research.crypto_stage_b.tests.conftest import candidate, cost, etr_bars


def _contract(
    strategy_id: str, *, venue: str, end_index: int
) -> CryptoStageBRunContract:
    start = date(2024, 1, 1)
    return CryptoStageBRunContract(
        candidate=candidate(strategy_id),
        venue=venue,
        exploration_start=start,
        exploration_end=start + timedelta(days=end_index),
        cost=cost(venue),
    )


def _etr_ablation_ranking_result():
    """Build simultaneous tail days with the strongest tuple at symbol ``F``."""
    profiles = {
        "A": (80.0, 100.0, 40.0, 80.0, 100.0),
        "B": (75.0, 95.0, 40.0, 75.0, 200.0),
        "C": (70.0, 90.0, 40.0, 70.0, 300.0),
        "D": (65.0, 85.0, 40.0, 65.0, 400.0),
        "E": (60.0, 80.0, 40.0, 60.0, 500.0),
        "F": (41.0, 51.0, 40.0, 50.0, 700.0),
    }
    signal_session = date(2024, 1, 1) + timedelta(days=251)
    bars: list[DailyBar] = []
    for symbol, (open_price, high, low, close, quote_volume) in profiles.items():
        for bar in etr_bars(symbol=symbol):
            if bar.session == signal_session:
                bar = replace(
                    bar,
                    open=open_price,
                    high=high,
                    low=low,
                    close=close,
                    quote_volume=quote_volume,
                )
            bars.append(bar)
    return run_execution_arm(
        source=InMemoryDailyBarSource(bars),
        contract=_contract("CR-SPOT-ETR-01", venue="upbit_krw", end_index=255),
        arm="ablation",
    )


def test_etr_ablation_uses_frozen_ranking_not_alphabetical_selection() -> None:
    result = _etr_ablation_ranking_result()
    signal_session = date(2024, 1, 1) + timedelta(days=251)
    simultaneous = [
        item for item in result.outcomes if item.signal_session == signal_session
    ]

    assert [item.symbol for item in simultaneous] == ["F", "E", "D", "C", "B", "A"]
    assert [item.rank for item in simultaneous] == [1, 2, 3, 4, 5, 6]
    assert [item.status for item in simultaneous] == [
        "completed",
        "completed",
        "completed",
        "completed",
        "completed",
        "capacity_rejected",
    ]
    assert simultaneous[0].ranking_metrics["qv_ratio"] == pytest.approx(7.0)
    assert simultaneous[0].ranking_metrics["clv"] == pytest.approx(10.0 / 11.0)
    assert (
        simultaneous[0].ranking_metrics["tail_severity"]
        > simultaneous[1].ranking_metrics["tail_severity"]
    )


def _simultaneous_ablation_outcomes(
    bars_by_symbol: dict[str, tuple[DailyBar, ...]],
) -> list:
    result = run_execution_arm(
        source=InMemoryDailyBarSource(
            [bar for bars in bars_by_symbol.values() for bar in bars]
        ),
        contract=_contract("CR-SPOT-ETR-01", venue="upbit_krw", end_index=255),
        arm="ablation",
    )
    signal_session = date(2024, 1, 1) + timedelta(days=251)
    return [item for item in result.outcomes if item.signal_session == signal_session]


def test_etr_ablation_tied_qv_ratio_uses_clv_before_symbol() -> None:
    """A later symbol wins a primary-key tie through the second frozen key."""
    profiles = {
        "A": (41.0, 51.0, 40.0, 45.0, 200.0),
        "Z": (41.0, 51.0, 40.0, 50.0, 200.0),
    }
    signal_session = date(2024, 1, 1) + timedelta(days=251)
    bars_by_symbol: dict[str, tuple[DailyBar, ...]] = {}
    for symbol, (open_price, high, low, close, quote_volume) in profiles.items():
        bars_by_symbol[symbol] = tuple(
            replace(
                bar,
                open=open_price,
                high=high,
                low=low,
                close=close,
                quote_volume=quote_volume,
            )
            if bar.session == signal_session
            else bar
            for bar in etr_bars(symbol=symbol)
        )

    simultaneous = _simultaneous_ablation_outcomes(bars_by_symbol)

    assert [item.symbol for item in simultaneous] == ["Z", "A"]
    assert [item.rank for item in simultaneous] == [1, 2]
    assert simultaneous[0].ranking_metrics["qv_ratio"] == pytest.approx(
        simultaneous[1].ranking_metrics["qv_ratio"]
    )
    assert (
        simultaneous[0].ranking_metrics["clv"] > simultaneous[1].ranking_metrics["clv"]
    )


def test_etr_ablation_tied_qv_and_clv_uses_tail_severity_before_symbol() -> None:
    """A later symbol wins ties on the first two frozen ranking keys."""
    weaker_tail = list(etr_bars(symbol="A"))
    for index in range(1, 50, 2):
        weaker_tail[index] = replace(weaker_tail[index], low=89.0, close=90.0)

    simultaneous = _simultaneous_ablation_outcomes(
        {
            "A": tuple(weaker_tail),
            "Z": etr_bars(symbol="Z"),
        }
    )

    assert [item.symbol for item in simultaneous] == ["Z", "A"]
    assert [item.rank for item in simultaneous] == [1, 2]
    assert simultaneous[0].ranking_metrics["qv_ratio"] == pytest.approx(
        simultaneous[1].ranking_metrics["qv_ratio"]
    )
    assert simultaneous[0].ranking_metrics["clv"] == pytest.approx(
        simultaneous[1].ranking_metrics["clv"]
    )
    assert (
        simultaneous[0].ranking_metrics["tail_severity"]
        > simultaneous[1].ranking_metrics["tail_severity"]
    )


def test_etr_ablation_populates_ranking_metrics_without_full_arm_admission() -> None:
    bars = etr_bars(quote_volume_on_signal=100.0)

    full = evaluate_signal(candidate("CR-SPOT-ETR-01"), bars[:252], arm="full")
    ablation = evaluate_signal(candidate("CR-SPOT-ETR-01"), bars[:252], arm="ablation")

    assert full.signal is False
    assert ablation.eligible is True
    assert ablation.signal is True
    assert ablation.stages == {"tail_day": True}
    assert ablation.metrics["qv_ratio"] == pytest.approx(1.0)
    assert ablation.metrics["clv"] == pytest.approx(0.8)
    assert ablation.metrics["tail_severity"] > 0.0


def test_etr_ablation_retains_tail_only_eligibility_when_rank_inputs_are_invalid() -> (
    None
):
    bars = list(etr_bars())
    bars[251] = replace(bars[251], high=40.0, low=40.0)

    ablation = evaluate_signal(candidate("CR-SPOT-ETR-01"), bars[:252], arm="ablation")

    assert ablation.eligible is True
    assert ablation.signal is True
    assert ablation.metrics["tail_severity"] > 0.0
    assert "qv_ratio" not in ablation.metrics
    assert "clv" not in ablation.metrics


def _steady_bars(*, strategy_id: str, venue: str, symbol: str) -> tuple[DailyBar, ...]:
    start = date(2024, 1, 1)
    return tuple(
        DailyBar(
            venue=venue,
            symbol=symbol,
            session=start + timedelta(days=index),
            open=100.0,
            high=101.0,
            low=99.0,
            close=100.0,
            base_volume=100.0,
            quote_volume=100.0,
        )
        for index in range(candidate(strategy_id).required_history_days)
    )


def _payload_sha256(payload: object) -> str:
    return hashlib.sha256(
        json.dumps(
            payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True
        ).encode("utf-8")
    ).hexdigest()


def _fixed_etr_full_output_hash() -> str:
    etr_full = run_execution_arm(
        source=InMemoryDailyBarSource(
            _steady_bars(
                strategy_id="CR-SPOT-ETR-01", venue="upbit_krw", symbol="KRW-ETR"
            )
        ),
        contract=_contract("CR-SPOT-ETR-01", venue="upbit_krw", end_index=251),
        arm="full",
    )
    return _payload_sha256(etr_full.to_dict())


def test_fixed_etr_full_fixture_is_unchanged() -> None:
    assert (
        _fixed_etr_full_output_hash()
        == "2731e2e4f21636039c715730e9ad19031e8d02b7810d558ddb562cd351cc10ec"
    )
