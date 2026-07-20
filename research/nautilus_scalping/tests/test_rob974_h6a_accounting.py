"""ROB-981 (ROB-974 R2 H6-A) CP4 -- independent exact-48 accounting, retry
semantics, and trial seal."""

from __future__ import annotations

import hashlib

import pytest
import rob974_h6a_accounting as acc

_ROW_IDS = tuple(
    [f"S3-{i:02d}" for i in range(24)] + [f"S4-{i:02d}" for i in range(24)]
)


def _hex64(label: str) -> str:
    return hashlib.sha256(label.encode()).hexdigest()


def _mapping() -> dict[str, str]:
    return {row_id: _hex64(row_id) for row_id in _ROW_IDS}


def _all_primary_completed_attempts(mapping) -> list[acc.AttemptAccountingRow]:
    return [
        acc.AttemptAccountingRow(
            row_id=row_id,
            experiment_id=mapping[row_id],
            retry_index=0,
            status="completed",
            reason_code=None,
            fold_evidence_hash=_hex64("fold"),
            run_identity=_hex64("run"),
        )
        for row_id in _ROW_IDS
    ]


def _build(**overrides) -> acc.CombinedAccountingReport:
    mapping = overrides.pop("row_id_to_experiment_id", None) or _mapping()
    kwargs = {
        "campaign_run_id": "rob974h6a-fixture-run",
        "canonical_row_ids": _ROW_IDS,
        "row_id_to_experiment_id": mapping,
        "registered_total": 48,
        "attempts": _all_primary_completed_attempts(mapping),
    }
    kwargs.update(overrides)
    return acc.build_combined_accounting(**kwargs)


class TestNormalPrimaryRun:
    def test_all_48_completed_is_complete_and_usable(self):
        report = _build()
        assert report.expected_total == 48
        assert report.primary_attempts == 48
        assert report.total_attempts == 48
        assert report.retry_attempts == 0
        assert (
            report.status_counts["completed"]
            + report.status_counts["rejected"]
            + report.status_counts["crashed"]
            + report.status_counts["timeout"]
            == 48
        )
        assert report.accounting_complete is True
        assert report.all_primary_completed is True
        assert report.performance_usable is True

    def test_all_48_crashed_is_complete_but_not_usable(self):
        mapping = _mapping()
        attempts = [
            acc.AttemptAccountingRow(
                row_id=row_id,
                experiment_id=mapping[row_id],
                retry_index=0,
                status="crashed",
                reason_code=acc.REASON_CHILD_EXECUTION_CRASHED,
                fold_evidence_hash=_hex64("fold"),
                run_identity=_hex64("run"),
            )
            for row_id in _ROW_IDS
        ]
        report = _build(row_id_to_experiment_id=mapping, attempts=attempts)
        assert report.accounting_complete is True
        assert report.all_primary_completed is False
        assert report.performance_usable is False
        assert report.status_counts["crashed"] == 48


class TestAccountingCompleteIsNotPerformancePass:
    def test_accounting_complete_never_implies_all_completed(self):
        mapping = _mapping()
        attempts = _all_primary_completed_attempts(mapping)
        attempts[0] = acc.AttemptAccountingRow(
            row_id="S3-00",
            experiment_id=mapping["S3-00"],
            retry_index=0,
            status="rejected",
            reason_code=acc.REASON_DATA_GAP_IN_POSITION,
            fold_evidence_hash=_hex64("fold"),
            run_identity=_hex64("run"),
        )
        report = _build(row_id_to_experiment_id=mapping, attempts=attempts)
        assert report.accounting_complete is True
        assert report.all_primary_completed is False
        assert report.performance_usable is False


class TestRetryAppendOnly:
    def test_valid_retry_is_complete_but_not_usable(self):
        mapping = _mapping()
        attempts = _all_primary_completed_attempts(mapping)
        attempts.append(
            acc.AttemptAccountingRow(
                row_id="S3-00",
                experiment_id=mapping["S3-00"],
                retry_index=1,
                status="completed",
                reason_code=None,
                fold_evidence_hash=_hex64("fold"),
                run_identity=_hex64("run"),
            )
        )
        report = _build(row_id_to_experiment_id=mapping, attempts=attempts)
        assert report.accounting_complete is True
        assert report.total_attempts == 49
        assert report.primary_attempts == 48
        assert report.retry_attempts == 1
        assert report.performance_usable is False

    def test_retry_never_updates_primary_status_counts(self):
        mapping = _mapping()
        attempts = _all_primary_completed_attempts(mapping)
        attempts.append(
            acc.AttemptAccountingRow(
                row_id="S3-00",
                experiment_id=mapping["S3-00"],
                retry_index=1,
                status="crashed",
                reason_code=acc.REASON_CHILD_EXECUTION_CRASHED,
                fold_evidence_hash=_hex64("fold"),
                run_identity=_hex64("run"),
            )
        )
        report = _build(row_id_to_experiment_id=mapping, attempts=attempts)
        # primary retry0 is still "completed"; the retry's crashed status is
        # additive in status_counts, never overwriting the primary's own.
        assert report.status_counts["completed"] == 48
        assert report.status_counts["crashed"] == 1

    def test_retry_gap_0_and_2_is_incomplete(self):
        mapping = _mapping()
        attempts = _all_primary_completed_attempts(mapping)
        attempts.append(
            acc.AttemptAccountingRow(
                row_id="S3-00",
                experiment_id=mapping["S3-00"],
                retry_index=2,
                status="completed",
                reason_code=None,
                fold_evidence_hash=_hex64("fold"),
                run_identity=_hex64("run"),
            )
        )
        report = _build(row_id_to_experiment_id=mapping, attempts=attempts)
        assert report.accounting_complete is False
        assert "S3-00" in report.duplicate_or_gap_row_ids

    def test_duplicate_retry_index_is_incomplete(self):
        mapping = _mapping()
        attempts = _all_primary_completed_attempts(mapping)
        attempts.append(
            acc.AttemptAccountingRow(
                row_id="S3-00",
                experiment_id=mapping["S3-00"],
                retry_index=0,
                status="completed",
                reason_code=None,
                fold_evidence_hash=_hex64("fold"),
                run_identity=_hex64("run"),
            )
        )
        report = _build(row_id_to_experiment_id=mapping, attempts=attempts)
        assert report.accounting_complete is False
        assert "S3-00" in report.duplicate_or_gap_row_ids

    def test_retry_without_primary_is_missing_not_gap(self):
        mapping = _mapping()
        attempts = _all_primary_completed_attempts(mapping)
        attempts[0] = acc.AttemptAccountingRow(
            row_id="S3-00",
            experiment_id=mapping["S3-00"],
            retry_index=1,
            status="completed",
            reason_code=None,
            fold_evidence_hash=_hex64("fold"),
            run_identity=_hex64("run"),
        )
        report = _build(row_id_to_experiment_id=mapping, attempts=attempts)
        assert "S3-00" in report.missing_row_ids
        assert "S3-00" not in report.duplicate_or_gap_row_ids


class TestMissingPrimaries:
    def test_47_primaries_missing_one(self):
        mapping = _mapping()
        attempts = _all_primary_completed_attempts(mapping)[:-1]
        report = _build(row_id_to_experiment_id=mapping, attempts=attempts)
        assert report.accounting_complete is False
        assert report.missing_row_ids == ("S4-23",)

    def test_duplicate_plus_missing_together(self):
        mapping = _mapping()
        attempts = _all_primary_completed_attempts(mapping)
        # Remove S4-23's primary (now missing) and duplicate S3-00's.
        attempts = [a for a in attempts if a.row_id != "S4-23"]
        attempts.append(
            acc.AttemptAccountingRow(
                row_id="S3-00",
                experiment_id=mapping["S3-00"],
                retry_index=0,
                status="completed",
                reason_code=None,
                fold_evidence_hash=_hex64("fold"),
                run_identity=_hex64("run"),
            )
        )
        report = _build(row_id_to_experiment_id=mapping, attempts=attempts)
        assert report.accounting_complete is False
        assert "S4-23" in report.missing_row_ids
        assert "S3-00" in report.duplicate_or_gap_row_ids


class TestCrossCampaignRejection:
    def test_out_of_plan_row_id_raises(self):
        mapping = _mapping()
        attempts = _all_primary_completed_attempts(mapping)
        attempts.append(
            acc.AttemptAccountingRow(
                row_id="S9-00",
                experiment_id=_hex64("rogue"),
                retry_index=0,
                status="completed",
                reason_code=None,
                fold_evidence_hash=_hex64("fold"),
                run_identity=_hex64("run"),
            )
        )
        with pytest.raises(acc.AccountingInputError):
            _build(row_id_to_experiment_id=mapping, attempts=attempts)

    def test_experiment_id_not_matching_trusted_mapping_raises(self):
        mapping = _mapping()
        attempts = _all_primary_completed_attempts(mapping)
        attempts[0] = acc.AttemptAccountingRow(
            row_id="S3-00",
            experiment_id=_hex64("forged"),
            retry_index=0,
            status="completed",
            reason_code=None,
            fold_evidence_hash=_hex64("fold"),
            run_identity=_hex64("run"),
        )
        with pytest.raises(acc.AccountingInputError):
            _build(row_id_to_experiment_id=mapping, attempts=attempts)

    def test_47_row_mapping_raises(self):
        mapping = _mapping()
        del mapping["S4-23"]
        with pytest.raises(acc.AccountingInputError):
            acc.build_combined_accounting(
                campaign_run_id="rob974h6a-fixture-run",
                canonical_row_ids=_ROW_IDS[:-1],
                row_id_to_experiment_id=mapping,
                registered_total=47,
                attempts=(),
            )


class TestMismatchExtraDomainValidation:
    def test_mismatch_row_id_outside_canonical_set_rejected(self):
        with pytest.raises(acc.AccountingInputError):
            _build(mismatch_row_ids=("S9-99",))

    def test_extra_experiment_id_inside_canonical_set_rejected(self):
        mapping = _mapping()
        with pytest.raises(acc.AccountingInputError):
            _build(
                row_id_to_experiment_id=mapping,
                extra_experiment_ids=(mapping["S3-00"],),
            )

    def test_mismatch_row_with_terminal_evidence_is_contradiction(self):
        mapping = _mapping()
        attempts = _all_primary_completed_attempts(mapping)
        with pytest.raises(acc.AccountingInputError):
            _build(
                row_id_to_experiment_id=mapping,
                attempts=attempts,
                mismatch_row_ids=("S3-00",),
            )

    def test_valid_mismatch_makes_accounting_incomplete(self):
        mapping = _mapping()
        attempts = [
            a for a in _all_primary_completed_attempts(mapping) if a.row_id != "S3-00"
        ]
        report = _build(
            row_id_to_experiment_id=mapping,
            attempts=attempts,
            mismatch_row_ids=("S3-00",),
        )
        assert report.accounting_complete is False
        assert "S3-00" in report.mismatch_row_ids
        assert "S3-00" not in report.missing_row_ids


class TestTrialAccountingHash:
    def test_deterministic_same_input(self):
        a = _build()
        b = _build()
        assert a.trial_accounting_hash == b.trial_accounting_hash

    def test_status_mutation_changes_hash(self):
        mapping = _mapping()
        attempts = _all_primary_completed_attempts(mapping)
        base = _build(row_id_to_experiment_id=mapping, attempts=attempts)
        attempts[0] = acc.AttemptAccountingRow(
            row_id="S3-00",
            experiment_id=mapping["S3-00"],
            retry_index=0,
            status="rejected",
            reason_code=acc.REASON_DATA_GAP_IN_POSITION,
            fold_evidence_hash=_hex64("fold"),
            run_identity=_hex64("run"),
        )
        mutated = _build(row_id_to_experiment_id=mapping, attempts=attempts)
        assert base.trial_accounting_hash != mutated.trial_accounting_hash

    def test_retry_is_included_in_hash_never_hidden_behind_48_seal(self):
        mapping = _mapping()
        base = _build(row_id_to_experiment_id=mapping)
        attempts = _all_primary_completed_attempts(mapping)
        attempts.append(
            acc.AttemptAccountingRow(
                row_id="S3-00",
                experiment_id=mapping["S3-00"],
                retry_index=1,
                status="completed",
                reason_code=None,
                fold_evidence_hash=_hex64("fold"),
                run_identity=_hex64("run"),
            )
        )
        with_retry = _build(row_id_to_experiment_id=mapping, attempts=attempts)
        assert base.trial_accounting_hash != with_retry.trial_accounting_hash

    def test_attempt_order_permutation_does_not_change_hash(self):
        mapping = _mapping()
        attempts_a = _all_primary_completed_attempts(mapping)
        attempts_b = list(reversed(attempts_a))
        a = _build(row_id_to_experiment_id=mapping, attempts=attempts_a)
        b = _build(row_id_to_experiment_id=mapping, attempts=attempts_b)
        assert a.trial_accounting_hash == b.trial_accounting_hash


class TestAttemptAccountingRowTypes:
    def test_bool_retry_index_rejected(self):
        with pytest.raises(acc.AccountingInputError):
            acc.AttemptAccountingRow(
                row_id="S3-00",
                experiment_id=_hex64("x"),
                retry_index=True,
                status="completed",
                reason_code=None,
                fold_evidence_hash=_hex64("fold"),
                run_identity=_hex64("run"),
            )

    def test_unknown_status_rejected(self):
        with pytest.raises(acc.AccountingInputError):
            acc.AttemptAccountingRow(
                row_id="S3-00",
                experiment_id=_hex64("x"),
                retry_index=0,
                status="never_selected",
                reason_code=None,
                fold_evidence_hash=_hex64("fold"),
                run_identity=_hex64("run"),
            )

    def test_completed_with_reason_code_rejected(self):
        with pytest.raises(acc.AccountingInputError):
            acc.AttemptAccountingRow(
                row_id="S3-00",
                experiment_id=_hex64("x"),
                retry_index=0,
                status="completed",
                reason_code="should-be-none",
                fold_evidence_hash=_hex64("fold"),
                run_identity=_hex64("run"),
            )

    def test_reason_code_outside_allowlist_rejected(self):
        with pytest.raises(acc.AccountingInputError):
            acc.AttemptAccountingRow(
                row_id="S3-00",
                experiment_id=_hex64("x"),
                retry_index=0,
                status="crashed",
                reason_code="totally_made_up_reason",
                fold_evidence_hash=_hex64("fold"),
                run_identity=_hex64("run"),
            )

    def test_non_hex64_fold_evidence_hash_rejected(self):
        with pytest.raises(acc.AccountingInputError):
            acc.AttemptAccountingRow(
                row_id="S3-00",
                experiment_id=_hex64("x"),
                retry_index=0,
                status="completed",
                reason_code=None,
                fold_evidence_hash="not-a-hash",
                run_identity=_hex64("run"),
            )

    def test_non_hex64_run_identity_rejected(self):
        with pytest.raises(acc.AccountingInputError):
            acc.AttemptAccountingRow(
                row_id="S3-00",
                experiment_id=_hex64("x"),
                retry_index=0,
                status="completed",
                reason_code=None,
                fold_evidence_hash=_hex64("fold"),
                run_identity="not-a-hash",
            )


class TestRegisteredTotalGate:
    """R1 blocker #3a: registered_total must participate in
    accounting_complete -- 48 completed primary attempts alone must NOT be
    "complete" if the campaign was never actually registered."""

    def test_zero_registered_total_is_incomplete_even_with_48_completed(self):
        mapping = _mapping()
        report = _build(row_id_to_experiment_id=mapping, registered_total=0)
        assert report.accounting_complete is False

    def test_47_registered_total_is_incomplete(self):
        mapping = _mapping()
        report = _build(row_id_to_experiment_id=mapping, registered_total=47)
        assert report.accounting_complete is False

    def test_48_registered_total_with_no_other_defects_is_complete(self):
        mapping = _mapping()
        report = _build(row_id_to_experiment_id=mapping, registered_total=48)
        assert report.accounting_complete is True


class TestFullSemanticAttemptSeal:
    """R1 blocker #3b: trial_accounting_hash must commit every semantic
    attempt field (reason_code/fold_evidence_hash/run_identity), not just
    row/experiment/retry/status -- so an AC18 status/reason/evidence
    mismatch is reconstructible and detectable."""

    def test_reason_code_mutation_changes_trial_hash(self):
        mapping = _mapping()
        attempts = _all_primary_completed_attempts(mapping)
        base = _build(row_id_to_experiment_id=mapping, attempts=attempts)
        mutated_attempts = list(attempts)
        mutated_attempts[0] = acc.AttemptAccountingRow(
            row_id=attempts[0].row_id,
            experiment_id=attempts[0].experiment_id,
            retry_index=attempts[0].retry_index,
            status="crashed",
            reason_code=acc.REASON_CHILD_EXECUTION_CRASHED,
            fold_evidence_hash=attempts[0].fold_evidence_hash,
            run_identity=attempts[0].run_identity,
        )
        mutated = _build(row_id_to_experiment_id=mapping, attempts=mutated_attempts)
        assert base.trial_accounting_hash != mutated.trial_accounting_hash

    def test_fold_evidence_hash_mutation_changes_trial_hash_even_with_same_status(self):
        mapping = _mapping()
        attempts = _all_primary_completed_attempts(mapping)
        base = _build(row_id_to_experiment_id=mapping, attempts=attempts)
        mutated_attempts = list(attempts)
        mutated_attempts[0] = acc.AttemptAccountingRow(
            row_id=attempts[0].row_id,
            experiment_id=attempts[0].experiment_id,
            retry_index=attempts[0].retry_index,
            status=attempts[0].status,
            reason_code=attempts[0].reason_code,
            fold_evidence_hash=_hex64("a-completely-different-fold-evidence"),
            run_identity=attempts[0].run_identity,
        )
        mutated = _build(row_id_to_experiment_id=mapping, attempts=mutated_attempts)
        assert base.trial_accounting_hash != mutated.trial_accounting_hash

    def test_run_identity_mutation_changes_trial_hash_even_with_same_status(self):
        mapping = _mapping()
        attempts = _all_primary_completed_attempts(mapping)
        base = _build(row_id_to_experiment_id=mapping, attempts=attempts)
        mutated_attempts = list(attempts)
        mutated_attempts[0] = acc.AttemptAccountingRow(
            row_id=attempts[0].row_id,
            experiment_id=attempts[0].experiment_id,
            retry_index=attempts[0].retry_index,
            status=attempts[0].status,
            reason_code=attempts[0].reason_code,
            fold_evidence_hash=attempts[0].fold_evidence_hash,
            run_identity=_hex64("a-completely-different-run-identity"),
        )
        mutated = _build(row_id_to_experiment_id=mapping, attempts=mutated_attempts)
        assert base.trial_accounting_hash != mutated.trial_accounting_hash
