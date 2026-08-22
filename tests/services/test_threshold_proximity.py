"""ROB-1315 §7-3 — near-miss tagging arithmetic. Pure, no I/O."""

from __future__ import annotations

import pytest

from app.services import threshold_proximity as tp

pytestmark = pytest.mark.unit


def test_ciena_rsi_case_is_a_near_miss():
    """CIEN: RSI 45.03 against a 45 ceiling (retro §7-3 cited case)."""

    tag = tp.evaluate(
        gate="screen.rsi_max",
        metric="rsi_14",
        observed=45.03,
        threshold=45,
        comparison="max",
        unit="rsi_points",
    )

    assert tag is not None
    assert tag["within_band"] is True
    assert tag["miss"] == pytest.approx(0.03)
    assert tag["threshold"] == 45
    assert tag["observed"] == pytest.approx(45.03)
    assert tag["comparison"] == "max"
    assert tag["verdict_changed"] is False


def test_rddt_upside_case_is_a_near_miss():
    """RDDT: honest upside 39.93% against a 40% floor (retro §7-3)."""

    tag = tp.evaluate(
        gate="buy.support_reserve_net.honest_upside_pct_min",
        metric="honest_upside_pct",
        observed=39.93,
        threshold=40,
        comparison="min",
        unit="percent",
    )

    assert tag is not None
    assert tag["within_band"] is True
    assert tag["miss"] == pytest.approx(0.07)
    assert tag["miss_pct_of_threshold"] == pytest.approx(0.175)


def test_a_wide_miss_is_recorded_but_outside_the_band():
    tag = tp.evaluate(
        gate="screen.rsi_max",
        metric="rsi_14",
        observed=78.0,
        threshold=45,
        comparison="max",
        unit="rsi_points",
    )

    assert tag is not None
    assert tag["within_band"] is False
    assert tag["miss"] == pytest.approx(33.0)
    # the band-filtered helper drops it
    assert (
        tp.near_miss(
            gate="screen.rsi_max",
            metric="rsi_14",
            observed=78.0,
            threshold=45,
            comparison="max",
            unit="rsi_points",
        )
        is None
    )


def test_a_passing_observation_has_nothing_to_tag():
    assert (
        tp.evaluate(
            gate="screen.rsi_max",
            metric="rsi_14",
            observed=44.0,
            threshold=45,
            comparison="max",
            unit="rsi_points",
        )
        is None
    )
    # exactly at the threshold passes a `max` gate, so it is not a rejection
    assert (
        tp.evaluate(
            gate="screen.rsi_max",
            metric="rsi_14",
            observed=45.0,
            threshold=45,
            comparison="max",
            unit="rsi_points",
        )
        is None
    )


def test_a_missing_observation_is_not_a_near_miss():
    """A gate that failed for lack of data is not a marginal rejection."""

    assert (
        tp.evaluate(
            gate="screen.rsi_max",
            metric="rsi_14",
            observed=None,
            threshold=45,
            comparison="max",
            unit="rsi_points",
        )
        is None
    )


def test_band_edge_is_inclusive_and_just_outside_is_not():
    inside = tp.near_miss(
        gate="g",
        metric="m",
        observed=46.0,
        threshold=45,
        comparison="max",
        unit="rsi_points",
    )
    outside = tp.near_miss(
        gate="g",
        metric="m",
        observed=46.001,
        threshold=45,
        comparison="max",
        unit="rsi_points",
    )

    assert inside is not None and inside["miss"] == pytest.approx(tp.PROXIMITY_BAND)
    assert outside is None


def test_forecast_tag_picks_the_closest_gate_and_stays_non_promoting():
    tags = [
        tp.evaluate(
            gate="screen.rsi_max",
            metric="rsi_14",
            observed=45.5,
            threshold=45,
            comparison="max",
            unit="rsi_points",
        ),
        tp.evaluate(
            gate="buy.support_reserve_net.honest_upside_pct_min",
            metric="honest_upside_pct",
            observed=39.93,
            threshold=40,
            comparison="min",
            unit="percent",
        ),
    ]

    fragment = tp.build_forecast_tag(tags, market="us", symbol="RDDT")

    assert fragment is not None
    block = fragment[tp.TAG]
    assert block["closest_gate"] == "buy.support_reserve_net.honest_upside_pct_min"
    assert block["closest_miss"] == pytest.approx(0.07)
    assert len(block["gates"]) == 2
    assert block["gate_verdict_changed"] is False
    assert block["promote"] is False
    assert block["live_gate_impact"] is False
    assert fragment["decision_bucket_hint"] == "deferred_no_action"


def test_forecast_tag_is_none_when_nothing_is_within_band():
    wide = tp.evaluate(
        gate="screen.rsi_max",
        metric="rsi_14",
        observed=70,
        threshold=45,
        comparison="max",
        unit="rsi_points",
    )

    assert tp.build_forecast_tag([wide], market="kr", symbol="005930") is None
    assert tp.build_forecast_tag([], market="kr", symbol="005930") is None


def test_module_has_no_io_imports():
    """The tag must never reach a DB, broker, network, or clock."""

    source = __import__("pathlib").Path(tp.__file__).read_text(encoding="utf-8")
    for forbidden in (
        "import httpx",
        "sqlalchemy",
        "app.core.db",
        "app.services.brokers",
        "datetime",
        "requests",
    ):
        assert forbidden not in source
