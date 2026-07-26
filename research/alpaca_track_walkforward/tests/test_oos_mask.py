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


def _issued_pass(masked: om.Masked, counts: bc.BlindCounts) -> om.DryCountPassEvidence:
    return om.issue_dry_count_pass(masked, counts)


def test_matching_real_dry_count_pass_unmasks_once():
    masked, counts = _masked()
    evidence = _issued_pass(masked, counts)
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
    masked, actual_counts = _masked()
    fake_counts = _counts(modeled_entries=999)
    assert fake_counts is not actual_counts
    with pytest.raises(om.OOSMaskBypassError, match="actual object"):
        om.issue_dry_count_pass(masked, fake_counts)


def test_below_sealed_minimum_cannot_issue_pass():
    counts = _counts(modeled_entries=4)
    masked, counts = _masked(counts=counts)
    with pytest.raises(om.OOSMaskBypassError, match="sealed minimum"):
        om.issue_dry_count_pass(masked, counts)


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
    masked, actual_counts = _masked(counts=counts)
    with pytest.raises(om.OOSMaskBypassError, match="incomplete"):
        om.issue_dry_count_pass(masked, actual_counts)
    forged = object.__new__(om.DryCountPassEvidence)
    for name, value in (
        ("_fold_id", "fold-0"),
        ("_family", "AP-A1"),
        ("_config_id", "AP-A1-00"),
        ("_modeled_entries", 5),
        ("_min_modeled_entries_per_fold", 5),
    ):
        object.__setattr__(forged, name, value)
    with pytest.raises(om.OOSMaskBypassError, match="reconstructed"):
        om.unmask(masked, forged)


def test_reconstructed_evidence_object_is_not_authority():
    masked, counts = _masked()
    forged = object.__new__(om.DryCountPassEvidence)
    for name, value in (
        ("_fold_id", "fold-0"),
        ("_family", "AP-A1"),
        ("_config_id", "AP-A1-00"),
        ("_modeled_entries", 999),
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
    masked, counts = _masked()
    object.__setattr__(masked, "_config_id", "AP-A1-07")
    with pytest.raises(om.OOSMaskBypassError, match="integrity"):
        om.issue_dry_count_pass(masked, counts)


def test_masked_and_evidence_subclassing_is_blocked():
    with pytest.raises(TypeError, match="cannot be subclassed"):

        class EvilMasked(om.Masked):
            pass

    with pytest.raises(TypeError, match="cannot be subclassed"):

        class EvilEvidence(om.DryCountPassEvidence):
            pass


def test_copy_reconstruction_pickle_and_deepcopy_are_blocked():
    masked, counts = _masked()
    evidence = _issued_pass(masked, counts)
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
    masked, counts = _masked(secret=secret)
    del secret
    gc.collect()
    assert secret_ref() is None
    evidence = _issued_pass(masked, counts)
    assert isinstance(om.unmask(masked, evidence), _SecretBox)


def test_exception_messages_never_contain_raw_value():
    masked, counts = _masked()
    evidence = _issued_pass(masked, counts)
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
        "issue_dry_count_pass",
        "mask",
        "unmask",
    }
