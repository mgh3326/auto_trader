import pytest
from rob974_h4_contracts import H4SourcePins, exact_h4_folds
from rob974_h4_plan import build_fixture_plan


def test_fixture_plan_has_registered_eight_folds_and_48_logical_attempts() -> None:
    plan = build_fixture_plan(
        source_pins=H4SourcePins("1" * 64, "2" * 64),
    )
    assert len(exact_h4_folds()) == 8
    assert plan.payload["logical_attempt_count"] == 48
    assert plan.expected_attempt_ids == tuple(f"S3-{i:02d}" for i in range(24)) + tuple(
        f"S4-{i:02d}" for i in range(24)
    )
    assert (
        plan.payload["production_state"] == "fixture_non_production_pending_actual_h6a"
    )


def test_plan_rejects_placeholder_or_untyped_source_pins() -> None:
    with pytest.raises(ValueError, match="zero placeholder"):
        H4SourcePins("0" * 64, "2" * 64)
    with pytest.raises(TypeError, match="exact H4SourcePins"):
        build_fixture_plan(source_pins={"runner_bundle_sha256": "1" * 64})


def test_fold_schedule_is_registered_and_ninth_is_dropped() -> None:
    folds = exact_h4_folds()
    assert [
        (fold.fold_id, fold.train_end_ms - fold.train_start_ms) for fold in folds
    ] == [(f"fold-{index:02d}", 120 * 86_400_000) for index in range(8)]
    assert folds[-1].oos_end_ms == 1_781_060_400_000
