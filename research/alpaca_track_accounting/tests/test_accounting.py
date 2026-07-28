"""ROB-1064 H6 — full-trial accounting and semantic-seal boundaries."""

from __future__ import annotations

from dataclasses import replace

import accounting as a
import pytest

FOLDS = tuple(f"fold-{index}" for index in range(8))
PROVENANCE = a.TrialProvenance(
    corpus_manifest_hash="a" * 64,
    fold_schedule_hash="b" * 64,
    code_hash="c" * 64,
    run_id="offline-run-001",
)


def _expected_configs() -> tuple[a.ExpectedConfig, ...]:
    rows = []
    for index in range(16):
        family = "AP-A1" if index < 8 else "AP-A2"
        family_index = index if index < 8 else index - 8
        rows.append(
            a.ExpectedConfig(
                strategy=family,
                config_id=f"{family}-{family_index:02d}",
                config_hash=f"{index + 1:064x}",
            )
        )
    return tuple(rows)


def _cell(
    config: a.ExpectedConfig,
    fold_id: str,
    *,
    status: str = "executed",
    observation_count: int | None = 1,
    unobserved_reason: str | None = None,
) -> a.FoldCell:
    return a.FoldCell(
        strategy=config.strategy,
        config_id=config.config_id,
        fold_id=fold_id,
        status=status,
        observation_count=observation_count,
        unobserved_reason=unobserved_reason,
    )


def _trial(
    config: a.ExpectedConfig,
    *,
    status: str = "executed",
    retry_count: int = 0,
    primary: bool = True,
    provenance: a.TrialProvenance = PROVENANCE,
    cells: tuple[a.FoldCell, ...] | None = None,
) -> a.TrialRecord:
    reason = "H4 evidence unavailable" if status == "structural_incomplete" else None
    count = None if reason else 1
    if cells is None:
        cells = tuple(
            _cell(
                config,
                fold_id,
                status=status,
                observation_count=count,
                unobserved_reason=reason,
            )
            for fold_id in FOLDS
        )
    events = [a.StatusEvent(sequence=0, status="registered", reason=None)]
    if status != "registered":
        events.append(a.StatusEvent(sequence=1, status=status, reason=reason))
    return a.TrialRecord(
        strategy=config.strategy,
        config_id=config.config_id,
        config_hash=config.config_hash,
        provenance=provenance,
        primary=primary,
        retry_count=retry_count,
        status_events=tuple(events),
        cells=cells,
    )


def _complete_trials() -> tuple[a.TrialRecord, ...]:
    return tuple(_trial(config) for config in _expected_configs())


def _seal(trials: tuple[a.TrialRecord, ...]) -> a.AccountingSeal:
    return a.seal_trial_accounting(
        trials,
        expected_configs=_expected_configs(),
        expected_fold_ids=FOLDS,
    )


@pytest.mark.parametrize(
    ("registered", "usable"),
    [(15, False), (16, True), (17, False)],
)
def test_exact_16_config_boundary(registered: int, usable: bool) -> None:
    trials = list(_complete_trials())
    if registered == 15:
        trials.pop()
    elif registered == 17:
        extra = a.ExpectedConfig(
            strategy="AP-A3",
            config_id="AP-A3-00",
            config_hash="f" * 64,
        )
        trials.append(_trial(extra))

    seal = _seal(tuple(trials))

    assert seal.report.expected == 16
    assert seal.report.registered == registered
    assert seal.report.status_sum == registered
    assert seal.report.performance_usable is usable


@pytest.mark.parametrize(
    ("cells", "usable"),
    [(127, False), (128, True), (129, False)],
)
def test_exact_128_cell_boundary(cells: int, usable: bool) -> None:
    trials = list(_complete_trials())
    if cells == 127:
        trials[0] = replace(trials[0], cells=trials[0].cells[:-1])
    elif cells == 129:
        trials[0] = replace(
            trials[0],
            cells=(
                *trials[0].cells,
                _cell(_expected_configs()[0], "fold-8"),
            ),
        )

    seal = _seal(tuple(trials))

    assert seal.report.cells == cells
    assert seal.report.performance_usable is usable


def test_duplicate_cell_is_rejected_not_merged() -> None:
    trials = list(_complete_trials())
    trials[0] = replace(
        trials[0],
        cells=(*trials[0].cells, trials[0].cells[0]),
    )
    with pytest.raises(a.DuplicateCellError, match="duplicate fold cell"):
        _seal(tuple(trials))


def test_missing_cell_is_reported_and_never_default_filled() -> None:
    trials = list(_complete_trials())
    trials[0] = replace(trials[0], cells=trials[0].cells[:-1])

    seal = _seal(tuple(trials))

    assert seal.report.cells == 127
    assert seal.report.missing_cell_ids == ("AP-A1/AP-A1-00/fold-7",)
    assert len(seal.trials[0].cells) == 7
    assert seal.report.performance_usable is False


def test_failed_config_cannot_be_removed_from_multiple_testing_denominator() -> None:
    configs = _expected_configs()
    trials = tuple(
        _trial(
            config,
            status=(
                "insufficient_sample"
                if config.config_id == "AP-A1-00"
                else "executed"
            ),
        )
        for config in configs
    )
    complete = _seal(trials)
    removed = _seal(tuple(t for t in trials if t.config_id != "AP-A1-00"))

    assert complete.report.status_counts["insufficient_sample"] == 1
    assert complete.report.status_sum == 16
    assert removed.report.registered == 15
    assert removed.report.missing_config_ids == ("AP-A1/AP-A1-00",)
    assert removed.report.performance_usable is False


def test_duplicate_trial_is_rejected_not_merged() -> None:
    trials = _complete_trials()
    with pytest.raises(a.DuplicateTrialError, match="duplicate trial"):
        _seal((*trials, trials[0]))


def test_terminal_status_cannot_be_overwritten() -> None:
    terminal = _trial(_expected_configs()[0], status="cost_cap_reject")
    with pytest.raises(a.StatusTransitionError, match="terminal"):
        a.append_status(terminal, status="executed", reason=None)


def test_status_event_history_rejects_replaced_or_duplicate_sequence() -> None:
    trial = _trial(_expected_configs()[0])
    with pytest.raises(a.StatusTransitionError, match="sequence"):
        replace(
            trial,
            status_events=(
                trial.status_events[0],
                a.StatusEvent(sequence=0, status="cost_cap_reject", reason=None),
            ),
        )


def test_retry_greater_than_zero_seals_failure_without_erasing_attempt() -> None:
    trials = list(_complete_trials())
    trials[0] = replace(trials[0], retry_count=1)

    seal = _seal(tuple(trials))

    assert seal.report.retry == 1
    assert seal.report.registered == 16
    assert seal.report.performance_usable is False
    assert "retry_count_nonzero" in seal.report.violations


def test_structural_incomplete_is_the_performance_usable_false_branch() -> None:
    trials = list(_complete_trials())
    trials[0] = _trial(
        _expected_configs()[0],
        status="structural_incomplete",
    )

    seal = _seal(tuple(trials))

    assert seal.report.structural_incomplete == 1
    assert seal.report.performance_usable is False


def test_all_rejection_statuses_remain_in_status_sum() -> None:
    statuses = (
        "insufficient_sample",
        "turnover_band_reject",
        "cost_cap_reject",
        "no_selected_config",
    )
    trials = list(_complete_trials())
    for index, status in enumerate(statuses):
        trials[index] = _trial(_expected_configs()[index], status=status)

    seal = _seal(tuple(trials))

    assert seal.report.status_sum == 16
    for status in statuses:
        assert seal.report.status_counts[status] == 1
    assert seal.report.performance_usable is True


def test_one_config_status_change_changes_semantic_hash() -> None:
    base_trials = list(_complete_trials())
    changed_trials = list(base_trials)
    base_trials[0] = _trial(_expected_configs()[0], status="cost_cap_reject")
    changed_trials[0] = _trial(
        _expected_configs()[0],
        status="turnover_band_reject",
    )

    assert _seal(tuple(base_trials)).semantic_hash != _seal(
        tuple(changed_trials)
    ).semantic_hash


def test_container_permutation_is_byte_identical() -> None:
    trials = _complete_trials()
    permuted = tuple(
        replace(trial, cells=tuple(reversed(trial.cells)))
        for trial in reversed(trials)
    )

    first = _seal(trials)
    second = _seal(permuted)

    assert first.semantic_hash == second.semantic_hash
    assert first.to_bytes() == second.to_bytes()


def test_different_provenance_is_a_distinct_trial_identity_not_aggregated() -> None:
    trials = list(_complete_trials())
    changed = list(trials)
    changed[0] = replace(
        changed[0],
        provenance=replace(PROVENANCE, code_hash="d" * 64),
    )

    assert trials[0].identity != changed[0].identity
    assert _seal(tuple(trials)).semantic_hash != _seal(tuple(changed)).semantic_hash


@pytest.mark.parametrize(
    "factory",
    [
        lambda: a.StatusEvent(sequence=True, status="registered", reason=None),
        lambda: replace(_complete_trials()[0], retry_count=False),
        lambda: replace(_complete_trials()[0].cells[0], observation_count=True),
    ],
)
def test_counts_are_exact_builtin_int_and_bool_is_rejected(factory) -> None:
    with pytest.raises(TypeError, match="built-in int"):
        factory()


def test_unobserved_value_requires_explicit_null_and_reason() -> None:
    config = _expected_configs()[0]
    with pytest.raises(ValueError, match="reason"):
        _cell(config, "fold-0", observation_count=None, unobserved_reason=None)
    with pytest.raises(ValueError, match="must be null"):
        _cell(
            config,
            "fold-0",
            observation_count=0,
            unobserved_reason="not observed",
        )


def test_report_exposes_h5_gate_fields_explicitly() -> None:
    report = _seal(_complete_trials()).report.to_payload()
    for field in (
        "expected",
        "registered",
        "primary",
        "status_sum",
        "cells",
        "retry",
        "performance_usable",
    ):
        assert field in report
    assert report["expected"] == 16
    assert report["registered"] == 16
    assert report["primary"] == 16
    assert report["status_sum"] == 16
    assert report["cells"] == 128
    assert report["retry"] == 0
    assert report["performance_usable"] is True
