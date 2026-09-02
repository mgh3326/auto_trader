"""ROB-1340 adversarial tests for every approved false-positive mode."""

from __future__ import annotations

import ast
import inspect
from dataclasses import replace
from pathlib import Path

import pytest

from app.services.mock_integration.authority_cessation import (
    AuthorityAttemptStartedV1,
    AuthorityAttemptTerminalState,
    AuthorityAttemptTerminalV1,
    AuthorityCessationKind,
    AuthorityReleaseStatus,
    terminal_receipt_digest,
    verify_authority_cessation,
)
from app.services.mock_integration.coordination import (
    acquire_physical_account_lease,
)

pytestmark = pytest.mark.unit


def _started(
    attempt_id: str = "authority-attempt:a",
    *,
    cycle_id: str = "cycle-current",
    key_count: int = 1,
) -> AuthorityAttemptStartedV1:
    return AuthorityAttemptStartedV1(
        authority_attempt_id=attempt_id,
        lane_id="kr.kiwoom.mock",
        cycle_id=cycle_id,
        order_attempt_id=f"order-{attempt_id}",
        owner_binding_digest="a" * 64,
        keyset_digest="b" * 64,
        key_count=key_count,
        baseline_matching_rows=0,
    )


def _terminal(
    started: AuthorityAttemptStartedV1,
    *,
    kind: AuthorityCessationKind = AuthorityCessationKind.ADVISORY_UNLOCK,
    acquired: int | None = None,
    unlock_true: int | None = None,
    post_rows: int | None = 0,
    termination_true: bool | None = None,
    observer_absent: bool | None = None,
    in_flight_unknown: bool = False,
    committed: bool = True,
    receipt_id: int | None = 1,
) -> AuthorityAttemptTerminalV1:
    acquired = started.key_count if acquired is None else acquired
    unlock_true = acquired if unlock_true is None else unlock_true
    if kind is AuthorityCessationKind.BACKEND_TERMINATION:
        post_rows = None
        termination_true = True if termination_true is None else termination_true
        observer_absent = True if observer_absent is None else observer_absent
    draft = AuthorityAttemptTerminalV1(
        authority_attempt_id=started.authority_attempt_id,
        lane_id=started.lane_id,
        cycle_id=started.cycle_id,
        order_attempt_id=started.order_attempt_id,
        claim_row_id=91,
        owner_binding_digest=started.owner_binding_digest,
        keyset_digest=started.keyset_digest,
        key_count=started.key_count,
        terminal_state=AuthorityAttemptTerminalState.CESSATION_RECEIPT_COMMITTED,
        kind=kind,
        lock_statement_dispatched=True,
        lock_definite_false=False,
        acquired_key_count=acquired,
        in_flight_unknown=in_flight_unknown,
        unlock_true_count=unlock_true,
        post_release_matching_rows=post_rows,
        termination_returned_exact_true=termination_true,
        observer_pid_absent=observer_absent,
        receipt_digest="",
        receipt_id=receipt_id,
        committed=committed,
    )
    return replace(draft, receipt_digest=terminal_receipt_digest(draft))


def _assessment(
    attempts: tuple[AuthorityAttemptStartedV1, ...],
    terminals: tuple[AuthorityAttemptTerminalV1, ...],
    **kwargs,
):  # noqa: ANN003, ANN202
    return verify_authority_cessation(
        cycle_id="cycle-current",
        attempts=attempts,
        terminals=terminals,
        enumeration_complete=True,
        **kwargs,
    )


def _assert_not_verified(assessment, mode: str) -> None:  # noqa: ANN001
    assert assessment.status is not AuthorityReleaseStatus.RELEASE_VERIFIED, mode
    assert assessment.release_verified is False, mode


def test_false_positive_1_single_true_unlock_with_reentrant_row_is_not_verified() -> (
    None
):
    started = _started()
    assessment = _assessment((started,), (_terminal(started, post_rows=1),))
    _assert_not_verified(assessment, "single true unlock left a re-entrant hold")


def test_false_positive_2_multi_key_partial_unlock_is_not_verified() -> None:
    started = _started(key_count=2)
    assessment = _assessment(
        (started,), (_terminal(started, acquired=2, unlock_true=1),)
    )
    _assert_not_verified(assessment, "only one of two acquired keys was released")


def test_false_positive_3_foreign_receipt_is_not_verified() -> None:
    started = _started()
    foreign = _started("authority-attempt:foreign")
    assessment = _assessment((started,), (_terminal(foreign),))
    _assert_not_verified(assessment, "foreign receipt cannot cover this start")


def test_false_positive_4_rollback_hold_omission_is_not_verified() -> None:
    started = _started()
    assessment = _assessment(
        (started,),
        (),
        history_hold_attempt_ids=(started.authority_attempt_id,),
        active_hold_attempt_ids=(started.authority_attempt_id,),
    )
    _assert_not_verified(assessment, "rollback hold lacks a qualifying receipt")


def test_false_positive_5_crash_between_start_and_receipt_is_not_verified() -> None:
    started = _started()
    assessment = _assessment((started,), ())
    _assert_not_verified(assessment, "committed start survived receipt-side crash")


def test_false_positive_6_last_success_cannot_mask_earlier_unreleased_attempt() -> None:
    first = _started("authority-attempt:first")
    last = _started("authority-attempt:last")
    assessment = _assessment((first, last), (_terminal(last),))
    _assert_not_verified(assessment, "last success does not cover the first start")


def test_false_positive_7_pool_close_without_exact_unlock_is_not_verified() -> None:
    started = _started()
    closed_only = _terminal(started, unlock_true=0, post_rows=None)
    assessment = _assessment((started,), (closed_only,))
    _assert_not_verified(assessment, "pool close is not advisory-lock cessation")


def test_false_positive_8_terminate_false_even_when_pid_absent_is_not_verified() -> (
    None
):
    started = _started()
    terminal = _terminal(
        started,
        kind=AuthorityCessationKind.BACKEND_TERMINATION,
        termination_true=False,
        observer_absent=True,
    )
    assessment = _assessment((started,), (terminal,))
    _assert_not_verified(assessment, "terminate=false plus PID absence is not proof")


def test_false_positive_9_uncommitted_receipt_is_not_verified() -> None:
    started = _started()
    terminal = _terminal(started, committed=False, receipt_id=1)
    assessment = _assessment((started,), (terminal,))
    _assert_not_verified(assessment, "uncommitted receipt cannot enter coverage")


def test_complete_exact_current_cycle_coverage_is_verified() -> None:
    started = _started()
    assessment = _assessment((started,), (_terminal(started),))
    assert assessment.status is AuthorityReleaseStatus.RELEASE_VERIFIED
    assert assessment.release_verified is True


def test_complete_receipt_coverage_is_still_vetoed_by_an_active_hold() -> None:
    """S-C: exact coverage cannot substitute for the independent hold veto."""

    started = _started()
    assessment = _assessment(
        (started,),
        (_terminal(started),),
        active_hold_attempt_ids=(started.authority_attempt_id,),
    )

    assert assessment.enumeration_complete is True
    assert assessment.expected_attempt_ids == (started.authority_attempt_id,)
    assert assessment.committed_receipt_attempt_ids == (started.authority_attempt_id,)
    assert "committed_receipt_coverage_mismatch" not in assessment.reasons
    assert "unreleased_authority_hold_veto" in assessment.reasons
    _assert_not_verified(assessment, "an active hold independently vetoes coverage")


def test_exact_termination_resolves_an_in_flight_unknown_key() -> None:
    started = _started()
    terminal = _terminal(
        started,
        kind=AuthorityCessationKind.BACKEND_TERMINATION,
        acquired=0,
        unlock_true=0,
        in_flight_unknown=True,
    )
    assessment = _assessment((started,), (terminal,))
    assert assessment.status is AuthorityReleaseStatus.RELEASE_VERIFIED


def test_old_cycle_orphan_does_not_poison_current_cycle_scope() -> None:
    old = _started("authority-attempt:old", cycle_id="cycle-old")
    current = _started()
    assessment = _assessment((current,), (_terminal(current),))
    assert old.cycle_id != assessment.cycle_id
    assert assessment.release_verified is True


@pytest.mark.parametrize(
    ("relative_path", "called_name"),
    (
        ("app/services/kis_mock_runner/singleton.py", "coordinate_mock_order_mutation"),
        (
            "app/services/brokers/binance/spot_demo/mock_auto_limit.py",
            "coordinate_mock_order_mutation",
        ),
        (
            "app/services/brokers/binance/spot_demo/d2_remediation_single.py",
            "acquire_physical_account_lease",
        ),
    ),
)
def test_s8_existing_production_callers_keep_the_legacy_optional_contract(
    relative_path: str, called_name: str
) -> None:
    """S8: new lineage parameters are optional and old callers do not change."""

    signature = inspect.signature(acquire_physical_account_lease)
    assert signature.parameters["authority_evidence"].default is None
    assert signature.parameters["authority_context"].default is None

    root = Path(__file__).resolve().parents[3]
    tree = ast.parse((root / relative_path).read_text(encoding="utf-8"))
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and (
            (isinstance(node.func, ast.Name) and node.func.id == called_name)
            or (isinstance(node.func, ast.Attribute) and node.func.attr == called_name)
        )
    ]
    assert calls, f"expected production call to {called_name} in {relative_path}"
    for call in calls:
        keyword_names = {keyword.arg for keyword in call.keywords}
        assert "authority_evidence" not in keyword_names
        assert "authority_context" not in keyword_names
