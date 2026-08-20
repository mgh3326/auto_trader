from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.services.buy_gate_ab_shadow.evaluate import (
    CandidateEvidence,
    evaluate_candidate,
)
from app.services.buy_gate_ab_shadow.forecast_tag import (
    FORBIDDEN_PAYLOAD_KEYS,
    assumed_notional,
    build_shadow_buy_forecasts,
)
from app.services.buy_gate_ab_shadow.spec import EXPERIMENT_ID, PINNED_SPEC_SHA256
from app.services.trade_journal.forecast_service import _validate_forecast_target

_AS_OF = datetime(2026, 8, 20, 6, 30, tzinfo=UTC)


def _eval(*, strength: str = "moderate"):
    return evaluate_candidate(
        CandidateEvidence.from_mapping(
            {
                "symbol": "005930",
                "market": "kr",
                "current_price": "70000",
                "support_strength": strength,
                "support_distance_pct": "4",
                "rsi": "40",
                "honest_upside_pct": "45",
                "other_gate_bits": {
                    "liquid_midcap": True,
                    "concentration": True,
                    "overhang": True,
                },
            }
        ),
        evaluation_as_of=_AS_OF,
    )


def test_b_only_emits_two_window_forecasts_with_frozen_tags() -> None:
    payloads = build_shadow_buy_forecasts(_eval(), created_by="orch-mock")
    assert [row["horizon"] for row in payloads] == ["5d", "20d"]
    for payload in payloads:
        target = payload["forecast_target"]
        _validate_forecast_target(target, instrument_type=payload["instrument_type"])
        assert payload["session_label"] == EXPERIMENT_ID
        assert payload["probability"] == 0.5
        assert target["cohort"] == "shadow_buy"
        assert target["variant"] == "B"
        assert target["promote"] is False
        assert target["live_gate_impact"] is False
        assert target["calibration_eligibility"] == "calibration_exclude"
        assert target["trade_performance_eligibility"] == "trade_performance_exclude"
        assert target["spec_sha256"] == PINNED_SPEC_SHA256
        assert target["evaluation_as_of"] == _AS_OF.isoformat()
        assert target["input_snapshot_sha256"]
        assert target["input_snapshot"]["symbol"] == "005930"
        assert target["entry_price"] == "70000"
        assert target["assumed_notional"] == str(assumed_notional("kr"))
        assert FORBIDDEN_PAYLOAD_KEYS.isdisjoint(payload)
        assert FORBIDDEN_PAYLOAD_KEYS.isdisjoint(target)
        assert "order_proposal_create" not in payload
        assert (
            "do not promote to proposal, order, or watch"
            in payload["contrary_evidence"]
        )
        assert payload["review_date"] >= "2026-08-20"


def test_a_pass_and_neither_do_not_emit_forecasts() -> None:
    assert build_shadow_buy_forecasts(_eval(strength="strong"), created_by="x") == []
    assert build_shadow_buy_forecasts(_eval(strength="weak"), created_by="x") == []


def test_blank_created_by_is_rejected() -> None:
    with pytest.raises(ValueError, match="created_by"):
        build_shadow_buy_forecasts(_eval(), created_by="  ")
