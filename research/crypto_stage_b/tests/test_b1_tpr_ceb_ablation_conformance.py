from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import date, timedelta

import pytest

from research.crypto_stage_b.contracts import CryptoStageBRunContract
from research.crypto_stage_b.engine import run_candidate_pair, run_execution_arm
from research.crypto_stage_b.signals import evaluate_signal
from research.crypto_stage_b.source import DailyBar, InMemoryDailyBarSource
from research.crypto_stage_b.tests.conftest import candidate, cost, etr_bars

_START = date(2024, 1, 1)


def _contract(
    strategy_id: str, *, venue: str, end_index: int
) -> CryptoStageBRunContract:
    return CryptoStageBRunContract(
        candidate=candidate(strategy_id),
        venue=venue,
        exploration_start=_START,
        exploration_end=_START + timedelta(days=end_index),
        cost=cost(venue),
    )


def _tpr_bars(*, symbol: str, quote_volume_on_signal: float) -> tuple[DailyBar, ...]:
    """Return two otherwise-identical trend signals with a controlled qv tuple."""
    bars: list[DailyBar] = []
    for index in range(131):
        close = 100.0 + 0.2 * index
        bars.append(
            DailyBar(
                venue="upbit_krw",
                symbol=symbol,
                session=_START + timedelta(days=index),
                open=close - 0.5,
                high=close + 1.0,
                low=close - 1.0,
                close=close,
                base_volume=100.0,
                quote_volume=(quote_volume_on_signal if index == 119 else 100.0),
            )
        )
    return tuple(bars)


def _ceb_bars(
    *,
    symbol: str,
    signal_high: float,
    signal_low: float,
    signal_close: float,
    quote_volume_on_signal: float,
) -> tuple[DailyBar, ...]:
    """Return a raw-breakout-only fixture with controlled ranking inputs."""
    bars: list[DailyBar] = []
    for index in range(129):
        if index == 121:
            open_price = 100.0
            high = signal_high
            low = signal_low
            close = signal_close
            quote_volume = quote_volume_on_signal
        else:
            open_price = 100.0
            high = 101.0
            low = 99.0
            close = 100.0
            quote_volume = 100.0
        bars.append(
            DailyBar(
                venue="upbit_krw",
                symbol=symbol,
                session=_START + timedelta(days=index),
                open=open_price,
                high=high,
                low=low,
                close=close,
                base_volume=100.0,
                quote_volume=quote_volume,
            )
        )
    return tuple(bars)


def _simultaneous_outcomes(
    *, strategy_id: str, bars: tuple[DailyBar, ...], signal_index: int
):
    result = run_execution_arm(
        source=InMemoryDailyBarSource(bars),
        contract=_contract(
            strategy_id,
            venue="upbit_krw",
            end_index=130 if strategy_id == "CR-SPOT-TPR-01" else 128,
        ),
        arm="ablation",
    )
    signal_session = _START + timedelta(days=signal_index)
    return [item for item in result.outcomes if item.signal_session == signal_session]


def test_tpr_ablation_alphabetic_fixture_uses_qv_before_symbol() -> None:
    """The lexically last symbol wins only through TPR's frozen third key."""
    outcomes = _simultaneous_outcomes(
        strategy_id="CR-SPOT-TPR-01",
        bars=_tpr_bars(symbol="A", quote_volume_on_signal=100.0)
        + _tpr_bars(symbol="Z", quote_volume_on_signal=300.0),
        signal_index=119,
    )

    assert [item.symbol for item in outcomes] == ["Z", "A"]
    assert outcomes[0].ranking_metrics["pullback_extension"] == pytest.approx(
        outcomes[1].ranking_metrics["pullback_extension"]
    )
    assert outcomes[0].ranking_metrics["trend_slope"] == pytest.approx(
        outcomes[1].ranking_metrics["trend_slope"]
    )
    assert outcomes[0].ranking_metrics["qv_ratio"] == pytest.approx(3.0)
    assert outcomes[1].ranking_metrics["qv_ratio"] == pytest.approx(1.0)


def test_tpr_ablation_missing_third_rank_mutant_would_choose_alphabetically() -> None:
    """Deleting qv_ratio is distinguishable from the frozen three-key ranking."""
    outcomes = _simultaneous_outcomes(
        strategy_id="CR-SPOT-TPR-01",
        bars=_tpr_bars(symbol="A", quote_volume_on_signal=100.0)
        + _tpr_bars(symbol="M", quote_volume_on_signal=200.0)
        + _tpr_bars(symbol="Z", quote_volume_on_signal=400.0),
        signal_index=119,
    )

    assert [item.symbol for item in outcomes] == ["Z", "M", "A"]
    assert [
        item.symbol
        for item in sorted(
            outcomes,
            key=lambda item: (
                -item.ranking_metrics["pullback_extension"],
                -item.ranking_metrics["trend_slope"],
                item.symbol,
            ),
        )
    ] == ["A", "M", "Z"]


def test_ceb_ablation_alphabetic_fixture_uses_first_two_frozen_keys() -> None:
    """The lexical first symbol loses although breakout extension is tied."""
    outcomes = _simultaneous_outcomes(
        strategy_id="CR-SPOT-CEB-01",
        bars=_ceb_bars(
            symbol="A",
            signal_high=102.2,
            signal_low=99.0,
            signal_close=102.0,
            quote_volume_on_signal=150.0,
        )
        + _ceb_bars(
            symbol="Z",
            signal_high=105.0,
            signal_low=99.0,
            signal_close=102.0,
            quote_volume_on_signal=300.0,
        ),
        signal_index=121,
    )

    assert [item.symbol for item in outcomes] == ["Z", "A"]
    assert (
        outcomes[0].ranking_metrics["qv_ratio"]
        > outcomes[1].ranking_metrics["qv_ratio"]
    )
    assert (
        outcomes[0].ranking_metrics["range_ratio"]
        > outcomes[1].ranking_metrics["range_ratio"]
    )
    assert outcomes[0].ranking_metrics["breakout_extension"] == pytest.approx(
        outcomes[1].ranking_metrics["breakout_extension"]
    )


def test_ceb_ablation_third_rank_substitution_mutant_is_defeated() -> None:
    """A breakout-only substitute picks A; the frozen tuple must select Z."""
    outcomes = _simultaneous_outcomes(
        strategy_id="CR-SPOT-CEB-01",
        bars=_ceb_bars(
            symbol="A",
            signal_high=106.0,
            signal_low=99.0,
            signal_close=105.0,
            quote_volume_on_signal=150.0,
        )
        + _ceb_bars(
            symbol="Z",
            signal_high=110.0,
            signal_low=99.0,
            signal_close=102.0,
            quote_volume_on_signal=300.0,
        ),
        signal_index=121,
    )

    assert [item.symbol for item in outcomes] == ["Z", "A"]
    assert (
        outcomes[0].ranking_metrics["qv_ratio"]
        > outcomes[1].ranking_metrics["qv_ratio"]
    )
    assert (
        outcomes[0].ranking_metrics["range_ratio"]
        > outcomes[1].ranking_metrics["range_ratio"]
    )
    assert (
        outcomes[0].ranking_metrics["breakout_extension"]
        < outcomes[1].ranking_metrics["breakout_extension"]
    )
    assert [
        item.symbol
        for item in sorted(
            outcomes,
            key=lambda item: (-item.ranking_metrics["breakout_extension"], item.symbol),
        )
    ] == ["A", "Z"]


def test_tpr_ablation_populates_qv_without_full_arm_admission() -> None:
    bars = _tpr_bars(symbol="TPR", quote_volume_on_signal=50.0)

    full = evaluate_signal(candidate("CR-SPOT-TPR-01"), bars[:120], arm="full")
    ablation = evaluate_signal(candidate("CR-SPOT-TPR-01"), bars[:120], arm="ablation")

    assert full.signal is False
    assert ablation.eligible is True
    assert ablation.signal is True
    assert ablation.stages == {"trend_state": True}
    assert ablation.metrics["qv_ratio"] == pytest.approx(0.5)


def test_tpr_ablation_keeps_missing_qv_last_when_raw_volume_is_invalid() -> None:
    bars = list(_tpr_bars(symbol="TPR", quote_volume_on_signal=300.0))
    bars[119] = replace(bars[119], base_volume=0.0)

    ablation = evaluate_signal(candidate("CR-SPOT-TPR-01"), bars[:120], arm="ablation")

    assert ablation.eligible is True
    assert ablation.signal is True
    assert "qv_ratio" not in ablation.metrics


def test_ceb_ablation_populates_ranking_inputs_without_full_arm_admission() -> None:
    bars = _ceb_bars(
        symbol="CEB",
        signal_high=106.0,
        signal_low=99.0,
        signal_close=105.0,
        quote_volume_on_signal=100.0,
    )

    full = evaluate_signal(candidate("CR-SPOT-CEB-01"), bars[:122], arm="full")
    ablation = evaluate_signal(candidate("CR-SPOT-CEB-01"), bars[:122], arm="ablation")

    assert full.signal is False
    assert ablation.eligible is True
    assert ablation.signal is True
    assert ablation.stages == {
        "compression_state": True,
        "raw_20d_breakout": True,
    }
    assert ablation.metrics["qv_ratio"] == pytest.approx(1.0)
    assert ablation.metrics["range_ratio"] > 1.0


def test_ceb_ablation_keeps_missing_ranking_inputs_last_when_raw_volume_is_invalid() -> (
    None
):
    bars = _ceb_bars(
        symbol="CEB",
        signal_high=106.0,
        signal_low=99.0,
        signal_close=105.0,
        quote_volume_on_signal=0.0,
    )

    ablation = evaluate_signal(candidate("CR-SPOT-CEB-01"), bars[:122], arm="ablation")

    assert ablation.eligible is True
    assert ablation.signal is True
    assert "qv_ratio" not in ablation.metrics
    assert "range_ratio" not in ablation.metrics


def _payload_sha256(payload: object) -> str:
    return hashlib.sha256(
        json.dumps(
            payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True
        ).encode("utf-8")
    ).hexdigest()


def test_fixed_fixture_full_arms_and_etr_all_arms_are_unchanged_from_base() -> None:
    """Base ``b342b09f5`` hashes prove the correction has no other arm effect."""
    etr_pair = run_candidate_pair(
        source=InMemoryDailyBarSource(etr_bars()),
        contract=_contract("CR-SPOT-ETR-01", venue="upbit_krw", end_index=255),
    )
    tpr_full = run_execution_arm(
        source=InMemoryDailyBarSource(
            _tpr_bars(symbol="TPR", quote_volume_on_signal=300.0)
        ),
        contract=_contract("CR-SPOT-TPR-01", venue="upbit_krw", end_index=130),
        arm="full",
    )
    ceb_full = run_execution_arm(
        source=InMemoryDailyBarSource(
            _ceb_bars(
                symbol="CEB",
                signal_high=106.0,
                signal_low=99.0,
                signal_close=105.0,
                quote_volume_on_signal=300.0,
            )
        ),
        contract=_contract("CR-SPOT-CEB-01", venue="upbit_krw", end_index=128),
        arm="full",
    )

    assert {
        "etr_pair_all_arms": _payload_sha256(etr_pair.to_dict()),
        "tpr_full": _payload_sha256(tpr_full.to_dict()),
        "ceb_full": _payload_sha256(ceb_full.to_dict()),
    } == {
        "etr_pair_all_arms": "14a57dc10a52f6553a7b7a3e31e484d74f263526cf08e28cf513c0cc47a92b5a",
        "tpr_full": "ca00c7c4890ad1bb1fb8e3acb52aa38b0da08a6911596f53fc38b4383c9188ef",
        "ceb_full": "6c82a7759bf5cb9d9d032d796745f957c421bdd40d8ebe56aa0ab405e1e6ee0c",
    }
