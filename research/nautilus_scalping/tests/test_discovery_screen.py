"""ROB-339 — discovery fast-fail classifier + artifact assembly (pure).

Non-canonical recommendations only: screened_out / needs_more_data /
promote_to_full_validation. Promote always carries in_sample_only=True. The
artifact records hypotheses_tested (multiple-comparison transparency) and a note
stating discovery never produces a gate verdict.
"""

from __future__ import annotations

from discovery.screen import (
    HypothesisSummary,
    build_artifact,
    classify,
)


def _summary(**kw) -> HypothesisSummary:
    base = {
        "name": "momentum_continuation",
        "conditions": "ret_3m>0 and vol_expansion",
        "sample_count": 500,
        "gross_expectancy_bps": 12.0,
        "fee_adjusted_bps": 4.0,
        "oos_fee_adjusted_bps": 3.0,
    }
    base.update(kw)
    return HypothesisSummary(**base)


def test_low_sample_count_needs_more_data() -> None:
    c = classify(_summary(sample_count=50), min_samples=200)
    assert c.recommendation == "needs_more_data"
    assert c.in_sample_only is False


def test_nonpositive_fee_adjusted_is_screened_out() -> None:
    c = classify(_summary(fee_adjusted_bps=-1.5))
    assert c.recommendation == "screened_out"
    assert "fee" in c.reason.lower()


def test_high_missed_fill_ratio_is_screened_out() -> None:
    c = classify(_summary(missed_fill_ratio=0.75), missed_fill_max=0.6)
    assert c.recommendation == "screened_out"
    assert "missed" in c.reason.lower()


def test_oos_sign_disagreement_is_screened_out() -> None:
    # positive in-sample edge but OOS tail flips negative -> not robust
    c = classify(_summary(fee_adjusted_bps=4.0, oos_fee_adjusted_bps=-2.0))
    assert c.recommendation == "screened_out"
    assert "oos" in c.reason.lower()


def test_positive_in_sample_and_oos_promotes() -> None:
    c = classify(_summary(fee_adjusted_bps=4.0, oos_fee_adjusted_bps=3.0))
    assert c.recommendation == "promote_to_full_validation"
    assert c.in_sample_only is True


def test_acceptable_missed_fill_still_promotes() -> None:
    c = classify(_summary(missed_fill_ratio=0.4), missed_fill_max=0.6)
    assert c.recommendation == "promote_to_full_validation"


def test_build_artifact_records_count_and_noncanonical_note() -> None:
    classified = [
        classify(_summary()),
        classify(_summary(name="sweep_reversal", fee_adjusted_bps=-3.0)),
    ]
    run = {
        "symbols": ["XRPUSDT"],
        "window": {"from": "2026-03-01", "to": "2026-05-14"},
        "fee_budget_bps": 8.0,
    }
    art = build_artifact(classified, run)
    assert art["schema_version"] == "scalping_discovery.v1"
    assert art["hypotheses_tested"] == 2
    assert len(art["hypotheses"]) == 2
    # recommendations stay within the non-canonical vocabulary
    recs = {h["recommendation"] for h in art["hypotheses"]}
    assert recs <= {"screened_out", "needs_more_data", "promote_to_full_validation"}
    # the note must make clear this is not a gate verdict
    assert "validated" in art["note"].lower() and "non-canonical" in art["note"].lower()
