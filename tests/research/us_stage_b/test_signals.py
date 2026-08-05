from __future__ import annotations

import math

import pytest

from research.us_stage_b.registry import US_CANDIDATE_ORDER
from research.us_stage_b.signals import evaluate_signal
from research.us_stage_b.source import USStageBDailyBar

from .conftest import candidate, sequential_sessions, volbreak_bars


def _mom_bars() -> tuple[USStageBDailyBar, ...]:
    sessions = sequential_sessions(127)
    close = 100.0
    bars: list[USStageBDailyBar] = []
    for index, session in enumerate(sessions):
        if index:
            close *= 1.0025 if index % 2 else 1.0015
        bars.append(
            USStageBDailyBar(
                symbol="MOM",
                session_date=session,
                open=close,
                adjusted_close=close,
                # Current volume is intentionally enormous; ADV20-pre must not use it.
                volume=5_000_000.0 if index == 126 else 50_000.0,
            )
        )
    return tuple(bars)


def _rev_bars() -> tuple[USStageBDailyBar, ...]:
    sessions = sequential_sessions(127)
    close = 100.0
    bars: list[USStageBDailyBar] = []
    for index, session in enumerate(sessions):
        if index:
            close *= 0.97 if index >= 124 else 1.001
        bars.append(
            USStageBDailyBar(
                symbol="REV",
                session_date=session,
                open=close,
                adjusted_close=close,
                volume=50_000.0,
            )
        )
    return tuple(bars)


def _sample_std(values: list[float]) -> float:
    average = sum(values) / len(values)
    return math.sqrt(
        sum((value - average) ** 2 for value in values) / (len(values) - 1)
    )


def test_mom_uses_sample_sigma63_sqrt126_and_pre_signal_adv_only(registry) -> None:
    binding = candidate(registry, US_CANDIDATE_ORDER[0])
    bars = _mom_bars()
    observation = evaluate_signal(
        binding,
        symbol="MOM",
        session_date=bars[-1].session_date,
        history=bars,
        no_active_position=True,
    )

    closes = [float(bar.adjusted_close) for bar in bars]
    returns = [closes[index] / closes[index - 1] - 1.0 for index in range(64, 127)]
    sigma63 = _sample_std(returns)
    expected_z = (closes[-1] / closes[0] - 1.0) / (sigma63 * math.sqrt(126))
    expected_adv = (
        sum(closes[index] * float(bars[index].volume) for index in range(106, 126)) / 20
    )

    assert observation.signal is True
    assert observation.metrics["sigma63_ddof1"] == pytest.approx(sigma63)
    assert observation.metrics["z126"] == pytest.approx(expected_z)
    assert observation.adv20_pre_proxy == pytest.approx(expected_adv)
    assert observation.adv20_pre_proxy < 10_000_000.0


def test_mom_keeps_the_no_active_position_condition_outside_ranking(registry) -> None:
    binding = candidate(registry, US_CANDIDATE_ORDER[0])
    bars = _mom_bars()
    observation = evaluate_signal(
        binding,
        symbol="MOM",
        session_date=bars[-1].session_date,
        history=bars,
        no_active_position=False,
    )

    assert observation.universe_eligible is True
    assert observation.technical_signal is True
    assert observation.signal is False
    assert observation.exclusion_reason == "active_position"


def test_rev_uses_r3_sigma60_sqrt3_and_r126_trend(registry) -> None:
    binding = candidate(registry, US_CANDIDATE_ORDER[1])
    bars = _rev_bars()
    observation = evaluate_signal(
        binding,
        symbol="REV",
        session_date=bars[-1].session_date,
        history=bars,
        no_active_position=True,
    )

    closes = [float(bar.adjusted_close) for bar in bars]
    returns = [closes[index] / closes[index - 1] - 1.0 for index in range(67, 127)]
    sigma60 = _sample_std(returns)
    expected_r3 = closes[-1] / closes[-4] - 1.0
    expected_z = expected_r3 / (sigma60 * math.sqrt(3))

    assert observation.signal is True
    assert observation.metrics["sigma60_ddof1"] == pytest.approx(sigma60)
    assert observation.metrics["r3"] == pytest.approx(expected_r3)
    assert observation.metrics["z3"] == pytest.approx(expected_z)
    assert observation.metrics["r126"] > 0.0


def test_volbreak_uses_prior_close_window_and_current_to_prior_volume_ratio(
    registry,
) -> None:
    binding = candidate(registry, US_CANDIDATE_ORDER[2])
    sessions = sequential_sessions(56)
    bars = volbreak_bars("VOL", sessions)
    observation = evaluate_signal(
        binding,
        symbol="VOL",
        session_date=sessions[-1],
        history=bars,
        no_active_position=True,
    )

    assert observation.signal is True
    assert observation.metrics["prior_close_high55"] == pytest.approx(105.4)
    assert observation.metrics["r1"] > 0.0
    assert observation.metrics["volume_ratio20"] == pytest.approx(2.0)
    assert "VOLUME_CA_UNRESOLVED" in observation.labels
    assert not hasattr(bars[-1], "high")
    assert not hasattr(bars[-1], "low")


def test_volbreak_refuses_nonpositive_current_volume_without_imputation(
    registry,
) -> None:
    binding = candidate(registry, US_CANDIDATE_ORDER[2])
    sessions = sequential_sessions(56)
    bars = list(volbreak_bars("VOL", sessions))
    bars[-1] = USStageBDailyBar(
        symbol="VOL",
        session_date=sessions[-1],
        open=107.0,
        adjusted_close=107.0,
        volume=0.0,
    )
    observation = evaluate_signal(
        binding,
        symbol="VOL",
        session_date=sessions[-1],
        history=bars,
        no_active_position=True,
    )

    assert observation.signal is False
    assert observation.exclusion_reason == "invalid_current_or_prior_volume"
