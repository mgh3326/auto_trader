"""Adversarial tests for the OOS PnL mask and dry-count authority."""

from __future__ import annotations

import copy
import gc
import inspect
import json
import pickle
import weakref

import blind_counts as bc
import oos_mask as om
import pytest

_SECRET_PNL_VALUE = -12345.6789


class _SecretBox:
    pass


def _counts(*, modeled_entries: int = 7) -> bc.BlindCounts:
    return bc.BlindCounts(
        total_decision_records=modeled_entries,
        modeled_entries_count=modeled_entries,
        closed_trades_count=modeled_entries,
        open_positions_count=0,
        entry_unfilled_count=0,
        exit_unfilled_count=0,
        fill_window_incomplete_count=0,
        holding_days=tuple(3 for _ in range(modeled_entries)),
        reason_code_histogram={"ENTRY_ACCEPTED": modeled_entries},
    )


def _masked(
    *,
    secret: object = _SECRET_PNL_VALUE,
    counts: bc.BlindCounts | None = None,
    **overrides: str,
) -> tuple[om.Masked, bc.BlindCounts]:
    actual_counts = counts or _counts()
    kwargs = {"fold_id": "fold-0", "family": "AP-A1", "config_id": "AP-A1-00"}
    kwargs.update(overrides)
    return (
        om.mask(secret, dry_counts=actual_counts, **kwargs),
        actual_counts,
    )


def _aggregate_pass(
    *,
    secret: object = _SECRET_PNL_VALUE,
    target_counts: bc.BlindCounts | None = None,
) -> tuple[om.Masked, bc.BlindCounts, om.DryCountPassEvidence]:
    masked_by_fold: dict[str, tuple[om.Masked, ...]] = {}
    counts_by_fold: dict[str, bc.BlindCounts] = {}
    target_masked: om.Masked | None = None
    target_actual_counts: bc.BlindCounts | None = None
    for index in range(8):
        fold_id = f"fold-{index}"
        counts = (
            target_counts if index == 0 and target_counts is not None else _counts()
        )
        masked, actual_counts = _masked(
            secret=secret if index == 0 else 0.0,
            counts=counts,
            fold_id=fold_id,
        )
        masked_by_fold[fold_id] = (masked,)
        counts_by_fold[fold_id] = actual_counts
        if index == 0:
            target_masked = masked
            target_actual_counts = actual_counts
    assert target_masked is not None
    assert target_actual_counts is not None
    evidence = om.issue_all_folds_dry_count_pass(
        masked_by_fold=masked_by_fold,
        dry_counts_by_fold=counts_by_fold,
    )
    return target_masked, target_actual_counts, evidence


def _aggregate_inputs(
    *,
    counts_by_fold: dict[str, bc.BlindCounts] | None = None,
) -> tuple[dict[str, tuple[om.Masked, ...]], dict[str, bc.BlindCounts]]:
    actual_counts = counts_by_fold or {f"fold-{index}": _counts() for index in range(8)}
    masked_by_fold = {
        fold_id: (_masked(counts=actual_counts[fold_id], fold_id=fold_id)[0],)
        for fold_id in (f"fold-{index}" for index in range(8))
    }
    return masked_by_fold, actual_counts


def test_matching_real_dry_count_pass_unmasks_once():
    masked, _counts_value, evidence = _aggregate_pass()
    assert om.unmask(masked, evidence) == _SECRET_PNL_VALUE
    with pytest.raises(om.OOSMaskBypassError, match="already"):
        om.unmask(masked, evidence)


def test_report_reproduction_closure_and_inspect_paths_have_no_raw_value():
    masked, _counts_value = _masked()
    with pytest.raises(AttributeError):
        object.__getattribute__(masked, "_reveal")
    closure_vars = inspect.getclosurevars(om.mask)
    assert "raw_value" not in closure_vars.nonlocals
    assert "raw_value" not in closure_vars.globals
    assert all(value is not _SECRET_PNL_VALUE for value in gc.get_referents(masked))


def test_report_reproduction_external_pass_constructor_is_blocked():
    masked, _counts_value = _masked()
    with pytest.raises(om.OOSMaskBypassError, match="cannot be constructed"):
        om.DryCountPassEvidence(
            fold_id="fold-0",
            family="AP-A1",
            config_id="AP-A1-00",
            modeled_entries=0,
            min_modeled_entries_per_fold=0,
            passed=True,
        )
    with pytest.raises(TypeError):
        om.unmask(masked, object())


def test_private_token_import_bypass_no_longer_exists():
    assert not hasattr(om, "_AUTHORIZED_UNMASK_TOKEN")
    masked, _counts_value = _masked()
    assert not hasattr(masked, "_reveal")


def test_fake_counts_cannot_issue_pass_for_an_existing_mask():
    masked_by_fold, counts_by_fold = _aggregate_inputs()
    fake_counts = dict(counts_by_fold)
    fake_counts["fold-0"] = _counts(modeled_entries=999)
    assert fake_counts["fold-0"] is not counts_by_fold["fold-0"]
    with pytest.raises(om.OOSMaskBypassError, match="actual object"):
        om.issue_all_folds_dry_count_pass(
            masked_by_fold=masked_by_fold,
            dry_counts_by_fold=fake_counts,
        )


def test_verifier_reproduction_fold_local_pass_is_forbidden():
    masked, counts = _masked()
    with pytest.raises(om.OOSMaskBypassError, match="all 8 folds"):
        om.issue_dry_count_pass(masked, counts)


def test_one_below_minimum_fold_blocks_every_fold_and_issues_no_pass():
    counts_by_fold = {f"fold-{index}": _counts() for index in range(8)}
    counts_by_fold["fold-1"] = _counts(modeled_entries=4)
    masked_by_fold, counts_by_fold = _aggregate_inputs(counts_by_fold=counts_by_fold)
    with pytest.raises(om.OOSMaskBypassError, match="fold-1.*sealed minimum"):
        om.issue_all_folds_dry_count_pass(
            masked_by_fold=masked_by_fold,
            dry_counts_by_fold=counts_by_fold,
        )
    forged = object.__new__(om.DryCountPassEvidence)
    with pytest.raises(om.OOSMaskBypassError, match="reconstructed"):
        om.unmask(masked_by_fold["fold-0"][0], forged)


@pytest.mark.parametrize(
    "counts",
    [
        # Verifier reproduction: five modeled entries plus one incomplete
        # fill window must not become PASS.
        bc.BlindCounts(
            total_decision_records=5,
            modeled_entries_count=5,
            closed_trades_count=5,
            open_positions_count=0,
            entry_unfilled_count=0,
            exit_unfilled_count=0,
            fill_window_incomplete_count=1,
            holding_days=(3, 3, 3, 3, 3),
            reason_code_histogram={"ENTRY_ACCEPTED": 5},
        ),
        # Self-devised bypass 1: modeled entries with no scheduled records.
        bc.BlindCounts(
            total_decision_records=0,
            modeled_entries_count=5,
            closed_trades_count=5,
            open_positions_count=0,
            entry_unfilled_count=0,
            exit_unfilled_count=0,
            fill_window_incomplete_count=0,
            holding_days=(3, 3, 3, 3, 3),
            reason_code_histogram={},
        ),
        # Self-devised bypass 2: a non-empty but under-counted histogram.
        bc.BlindCounts(
            total_decision_records=5,
            modeled_entries_count=5,
            closed_trades_count=5,
            open_positions_count=0,
            entry_unfilled_count=0,
            exit_unfilled_count=0,
            fill_window_incomplete_count=0,
            holding_days=(3, 3, 3, 3, 3),
            reason_code_histogram={"ENTRY_ACCEPTED": 4},
        ),
        # Self-devised bypass 3: entries do not reconcile to closed + open.
        bc.BlindCounts(
            total_decision_records=5,
            modeled_entries_count=5,
            closed_trades_count=4,
            open_positions_count=0,
            entry_unfilled_count=0,
            exit_unfilled_count=0,
            fill_window_incomplete_count=0,
            holding_days=(3, 3, 3, 3),
            reason_code_histogram={"ENTRY_ACCEPTED": 5},
        ),
    ],
)
def test_any_structurally_incomplete_counts_cannot_issue_or_unmask(counts):
    counts_by_fold = {f"fold-{index}": _counts() for index in range(8)}
    counts_by_fold["fold-3"] = counts
    masked_by_fold, counts_by_fold = _aggregate_inputs(counts_by_fold=counts_by_fold)
    with pytest.raises(om.OOSMaskBypassError, match="fold-3.*incomplete"):
        om.issue_all_folds_dry_count_pass(
            masked_by_fold=masked_by_fold,
            dry_counts_by_fold=counts_by_fold,
        )
    forged = object.__new__(om.DryCountPassEvidence)
    for name, value in (
        ("_family", "AP-A1"),
        ("_config_id", "AP-A1-00"),
        ("_fold_ids", tuple(f"fold-{index}" for index in range(8))),
        (
            "_modeled_entries_by_fold",
            tuple((f"fold-{index}", 5) for index in range(8)),
        ),
        ("_min_modeled_entries_per_fold", 5),
    ):
        object.__setattr__(forged, name, value)
    with pytest.raises(om.OOSMaskBypassError, match="reconstructed"):
        om.unmask(masked_by_fold["fold-0"][0], forged)


def test_reconstructed_evidence_object_is_not_authority():
    masked, _counts_value = _masked()
    forged = object.__new__(om.DryCountPassEvidence)
    for name, value in (
        ("_family", "AP-A1"),
        ("_config_id", "AP-A1-00"),
        ("_fold_ids", tuple(f"fold-{index}" for index in range(8))),
        (
            "_modeled_entries_by_fold",
            tuple((f"fold-{index}", 999) for index in range(8)),
        ),
        ("_min_modeled_entries_per_fold", 0),
    ):
        object.__setattr__(forged, name, value)
    with pytest.raises(om.OOSMaskBypassError, match="reconstructed"):
        om.unmask(masked, forged)


def test_reconstructed_mask_handle_is_not_in_the_issuance_registry():
    forged = object.__new__(om.Masked)
    object.__setattr__(forged, "_fold_id", "fold-0")
    object.__setattr__(forged, "_family", "AP-A1")
    object.__setattr__(forged, "_config_id", "AP-A1-00")
    with pytest.raises(om.OOSMaskBypassError, match="reconstructed"):
        om.issue_dry_count_pass(forged, _counts())


def test_object_setattr_tampering_fails_integrity_check():
    masked_by_fold, counts_by_fold = _aggregate_inputs()
    masked = masked_by_fold["fold-0"][0]
    object.__setattr__(masked, "_config_id", "AP-A1-07")
    with pytest.raises(om.OOSMaskBypassError, match="integrity"):
        om.issue_all_folds_dry_count_pass(
            masked_by_fold=masked_by_fold,
            dry_counts_by_fold=counts_by_fold,
        )


def test_masked_and_evidence_subclassing_is_blocked():
    with pytest.raises(TypeError, match="cannot be subclassed"):

        class EvilMasked(om.Masked):
            pass

    with pytest.raises(TypeError, match="cannot be subclassed"):

        class EvilEvidence(om.DryCountPassEvidence):
            pass


def test_copy_reconstruction_pickle_and_deepcopy_are_blocked():
    masked, _counts_value, evidence = _aggregate_pass()
    for value in (masked, evidence):
        with pytest.raises((om.OOSMaskBypassError, TypeError, pickle.PicklingError)):
            copy.copy(value)
        with pytest.raises((om.OOSMaskBypassError, TypeError, pickle.PicklingError)):
            copy.deepcopy(value)
        with pytest.raises((om.OOSMaskBypassError, TypeError, pickle.PicklingError)):
            pickle.dumps(value)


def test_nested_raw_values_are_not_object_referents_or_string_representations():
    secret = {"outer": [{"pnl": _SECRET_PNL_VALUE}], "other": ("x", 3)}
    masked, _counts_value = _masked(secret=secret)
    assert secret not in gc.get_referents(masked)
    assert str(_SECRET_PNL_VALUE) not in repr(masked)
    assert str(_SECRET_PNL_VALUE) not in str(masked)
    with pytest.raises(TypeError):
        json.dumps(masked)
    redacted_json = json.dumps({"pnl": masked}, default=str)
    assert str(_SECRET_PNL_VALUE) not in redacted_json


def test_source_object_is_not_retained_in_the_caller_process_object_graph():
    secret = _SecretBox()
    secret_ref = weakref.ref(secret)
    masked, _counts_value, evidence = _aggregate_pass(secret=secret)
    del secret
    gc.collect()
    assert secret_ref() is None
    assert isinstance(om.unmask(masked, evidence), _SecretBox)


def test_exception_messages_never_contain_raw_value():
    _masked_value, _counts_value, evidence = _aggregate_pass()
    other_masked, _other_counts = _masked(config_id="AP-A1-01")
    with pytest.raises(om.OOSMaskBypassError) as exc_info:
        om.unmask(other_masked, evidence)
    assert str(_SECRET_PNL_VALUE) not in str(exc_info.value)


def test_equality_and_hash_oracles_are_blocked():
    masked_a, _counts_a = _masked()
    masked_b, _counts_b = _masked()
    with pytest.raises(om.OOSMaskBypassError):
        _ = masked_a == masked_b
    with pytest.raises(TypeError):
        hash(masked_a)


def test_public_api_requires_issuance_not_public_evidence_construction():
    assert set(om.__all__) == {
        "DryCountPassEvidence",
        "Masked",
        "OOSMaskBypassError",
        "issue_all_folds_dry_count_pass",
        "issue_dry_count_pass",
        "mask",
        "unmask",
    }
