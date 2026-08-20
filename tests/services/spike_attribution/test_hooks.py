"""Hook ⓐ / ⓑ guard rails: neither may manufacture sufficiency or a trade."""

from __future__ import annotations

import datetime as dt
import json
from decimal import Decimal

import pytest

from app.services.spike_attribution.attribute import build_attribution
from app.services.spike_attribution.catalyst_basis import build_catalyst_basis
from app.services.spike_attribution.contract import ELIGIBILITY_ELIGIBLE
from app.services.spike_attribution.forecast_tag import (
    FORBIDDEN_PAYLOAD_KEYS,
    ForecastTagError,
    build_prereg_forecasts,
    target_direction,
    target_price,
)
from app.services.spike_attribution.spec import PINNED_SPEC_SHA256
from tests.services.spike_attribution.test_attribute import (
    KST,
    item,
    make_event,
    materials,
)


def attributed_record():
    return build_attribution(
        event=make_event(),
        materials=materials(
            item(
                attribution_type="disclosure",
                published_at=dt.datetime(2026, 8, 20, 10, 40, tzinfo=KST),
                eligibility=ELIGIBILITY_ELIGIBLE,
                title="조회공시요구(풍문또는보도)",
            )
        ),
    )


def unattributed_record():
    return build_attribution(event=make_event(), materials=materials())


# --- hook (a) ------------------------------------------------------------


def test_catalyst_basis_carries_citations_when_attributed() -> None:
    block = build_catalyst_basis(attributed_record())
    assert block["satisfies_catalyst_basis_requirement"] is True
    assert block["unsatisfied_reason"] is None
    assert block["citations"][0]["title"] == "조회공시요구(풍문또는보도)"
    assert block["citations"][0]["url"]
    assert block["policy_tier"] == "momentum_spike_profit_ladder"


def test_catalyst_basis_cannot_manufacture_sufficiency_for_unattributed() -> None:
    block = build_catalyst_basis(unattributed_record())
    assert block["satisfies_catalyst_basis_requirement"] is False
    assert block["citations"] == []
    assert block["unsatisfied_reason"]
    assert "재료로 설명되지 않음" in block["unsatisfied_reason"]


def test_catalyst_basis_never_claims_the_evidence_pair_is_complete() -> None:
    # The tier requires catalyst_basis AND flow_basis. We supply one.
    for record in (attributed_record(), unattributed_record()):
        block = build_catalyst_basis(record)
        assert block["required_thesis_evidence_complete"] is False
        assert block["flow_basis"]["supplied"] is False
        assert block["does_not_supply"] == ["flow_basis"]
        assert block["can_loosen_live_gate"] is False
        assert block["promote"] is False


def test_catalyst_basis_is_json_serialisable() -> None:
    json.dumps(build_catalyst_basis(attributed_record()))


# --- hook (b) ------------------------------------------------------------


def test_prereg_forecasts_cover_every_pinned_window() -> None:
    payloads = build_prereg_forecasts(attributed_record(), created_by="test")
    assert [p["horizon"] for p in payloads] == ["3d", "10d"]
    assert {p["session_label"] for p in payloads} == {"rob-1303-spike-attribution"}
    for payload in payloads:
        target = payload["forecast_target"]
        assert target["spec_sha256"] == PINNED_SPEC_SHA256
        assert target["promote"] is False
        assert target["calibration_eligibility"] == "calibration_exclude"
        assert target["trade_performance_eligibility"] == "trade_performance_exclude"
        assert target["do_not_use_forecast_resolve_as_experiment_score"] is True


def test_the_record_travels_inside_the_forecast_with_its_evidence_links() -> None:
    payload = build_prereg_forecasts(attributed_record(), created_by="test")[0]
    record = payload["forecast_target"]["attribution_record"]
    assert record["candidates"][0]["url"]
    assert record["event"]["evidence_window"]["end_inclusive"]
    assert record["material_availability"]
    json.dumps(payload)


def test_unattributed_spikes_are_pre_registered_too() -> None:
    payloads = build_prereg_forecasts(unattributed_record(), created_by="test")
    assert payloads
    for payload in payloads:
        assert payload["forecast_target"]["scored_class"] == "unattributed"
        assert payload["forecast_target"]["unattributed"] is True


def test_target_price_is_the_retained_boundary_and_direction_follows_the_spike() -> (
    None
):
    record = attributed_record()
    # prev 208000 → close 219500; the 0.5 retention boundary is 213750.
    assert target_price(record) == Decimal("213750.0")
    assert target_direction(record) == "at_or_above"


def test_review_dates_are_derived_from_the_spike_session() -> None:
    payloads = build_prereg_forecasts(attributed_record(), created_by="test")
    assert payloads[0]["forecast_start_date"] == "2026-08-20"
    assert payloads[0]["review_date"] == "2026-08-25"
    assert payloads[1]["review_date"] == "2026-09-03"


def test_no_execution_key_can_ride_along() -> None:
    payloads = build_prereg_forecasts(attributed_record(), created_by="test")
    for payload in payloads:
        assert not FORBIDDEN_PAYLOAD_KEYS.intersection(payload)
        assert not FORBIDDEN_PAYLOAD_KEYS.intersection(payload["forecast_target"])


def test_missing_author_is_refused() -> None:
    with pytest.raises(ForecastTagError):
        build_prereg_forecasts(attributed_record(), created_by="  ")


def test_forecast_direction_always_agrees_with_the_scoring_denominator() -> None:
    from app.services.spike_attribution.contract import SpikeEvent

    base = make_event()
    for close, expected in (
        (Decimal("219500"), "at_or_above"),
        (Decimal("190000"), "at_or_below"),
    ):
        event = SpikeEvent(
            market=base.market,
            symbol=base.symbol,
            session_date=base.session_date,
            direction="up" if close > base.prev_close else "down",
            prev_close=base.prev_close,
            close=close,
            high=base.high,
            low=base.low,
            close_to_close_pct=Decimal("0"),
            intraday_extreme_pct=Decimal("0"),
            triggered_bases=("close_to_close",),
            window_start_exclusive=base.window_start_exclusive,
            window_end_inclusive=base.window_end_inclusive,
        )
        record = build_attribution(event=event, materials=materials())
        assert target_direction(record) == expected
        denominator_sign = 1 if close > base.prev_close else -1
        assert (target_price(record) - base.prev_close) * denominator_sign > 0


def test_a_flat_close_is_not_pre_registered_at_all() -> None:
    from app.services.spike_attribution.contract import SpikeEvent
    from app.services.spike_attribution.forecast_tag import prereg_skipped_reason

    base = make_event()
    flat = SpikeEvent(
        market=base.market,
        symbol=base.symbol,
        session_date=base.session_date,
        direction="up",
        prev_close=base.prev_close,
        close=base.prev_close,  # intraday-only spike that closed unchanged
        high=base.high,
        low=base.low,
        close_to_close_pct=Decimal("0.0000"),
        intraday_extreme_pct=Decimal("7.4519"),
        triggered_bases=("intraday_extreme",),
        window_start_exclusive=base.window_start_exclusive,
        window_end_inclusive=base.window_end_inclusive,
    )
    record = build_attribution(event=flat, materials=materials())
    assert prereg_skipped_reason(record) == "zero_denominator_close_equals_prev_close"
    assert build_prereg_forecasts(record, created_by="test") == []
