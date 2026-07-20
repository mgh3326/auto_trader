"""ROB-981 (ROB-974 R2 H6-A) CP3 -- attempt, eight-fold trace, dual-evidence
DTOs and semantic seals."""

from __future__ import annotations

import hashlib

import pytest
import rob974_h6a_evidence as ev

FULL_CAMPAIGN_HASH = hashlib.sha256(b"fixture-full-campaign").hexdigest()
CAMPAIGN_RUN_ID = "rob974h6a-fixture-run"
EXPERIMENT_ID = hashlib.sha256(b"fixture-experiment-S3-00").hexdigest()
STRATEGY_KEY = "ROB974-S3-FIXTURE"


def _hex64(label: str) -> str:
    return hashlib.sha256(label.encode()).hexdigest()


def _fold_trace(
    i: int, *, selected: bool = False, rejection_reason: str | None = "lost_arbitration"
) -> ev.FoldSelectionTrace:
    return ev.FoldSelectionTrace(
        fold_id=f"fold-{i:02d}",
        fold_index=i,
        selected=selected,
        eligible_symbols_or_pairs=("XRPUSDT",),
        excluded_symbols_or_pairs=(),
        accepted_input_hash=_hex64(f"accepted-{i}") if selected else None,
        rejection_reason=None if selected else rejection_reason,
        no_trade_reason_counts={},
    )


def _all_fold_traces(*, selected_index: int | None) -> tuple:
    return tuple(
        _fold_trace(i, selected=(i == selected_index)) for i in range(ev.FOLD_COUNT)
    )


def _unique_evidence_row(i: int) -> ev.UniqueGeneratorEvidence:
    kwargs = {
        "fold_id": f"fold-{i:02d}",
        "candidate_identity_hash": _hex64(f"candidate-{i}"),
        "evaluated_decision_units": 10,
        "no_signal": 4,
        "candidate": 6,
        "generator_rejected": 4,
        "generator_accepted": 2,
        "generator_rejection_subtotal_by_reason": {"below_er_min": 3, "vol_gate": 1},
    }
    content_hash = ev._recompute_unique_evidence_hash(_FakeUniqueForHash(**kwargs))
    return ev.UniqueGeneratorEvidence(**kwargs, content_hash=content_hash)


class _FakeUniqueForHash:
    """Bare attribute holder so we can call the module's own recompute
    helper to derive a valid content_hash BEFORE constructing the real
    (validating) dataclass -- avoids duplicating the hash recipe in tests."""

    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


def _all_unique_evidence() -> tuple:
    return tuple(_unique_evidence_row(i) for i in range(ev.FOLD_COUNT))


class _FakePathForHash:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


def _path_scenario_row(
    name: str, *, status: str, trade_count: int = 0, member_trade_keys: tuple = ()
) -> ev.PathScenarioEvidence:
    reason_code = None
    if status not in ("completed", "never_selected"):
        reason_code = next(iter(ev.ALLOWED_REASONS_BY_STATUS[status]))
    kwargs = {
        "path_scenario": name,
        "status": status,
        "reason_code": reason_code,
        "trade_count": trade_count,
        "member_trade_keys": member_trade_keys,
        "no_trade_reason_counts": {},
    }
    artifact_hash = ev._recompute_path_scenario_hash(_FakePathForHash(**kwargs))
    return ev.PathScenarioEvidence(**kwargs, artifact_hash=artifact_hash)


def _never_selected_scenarios() -> tuple:
    return tuple(
        _path_scenario_row(name, status="never_selected") for name in ev.PATH_SCENARIOS
    )


def _completed_scenarios_with_trades() -> tuple:
    rows = []
    for name in ev.PATH_SCENARIOS:
        key = _hex64(f"trade-{name}")
        rows.append(
            _path_scenario_row(
                name, status="completed", trade_count=1, member_trade_keys=(key,)
            )
        )
    return tuple(rows)


def _build_s3_attempt(
    *, selected_index: int | None = 0, **overrides
) -> ev.AttemptRecord:
    kwargs = {
        "row_id": "S3-00",
        "experiment_id": EXPERIMENT_ID,
        "campaign_run_id": CAMPAIGN_RUN_ID,
        "full_campaign_hash": FULL_CAMPAIGN_HASH,
        "strategy_key": STRATEGY_KEY,
        "retry_index": 0,
        "status": "completed",
        "reason_code": None,
        "fold_traces": _all_fold_traces(selected_index=selected_index),
        "unique_evidence": _all_unique_evidence(),
        "path_scenario_evidence": (
            _completed_scenarios_with_trades()
            if selected_index is not None
            else _never_selected_scenarios()
        ),
    }
    kwargs.update(overrides)
    return ev.build_attempt_record(**kwargs)


class TestFoldSelectionTrace:
    def test_valid_trace_constructs(self):
        _fold_trace(0, selected=True)

    def test_fold_index_mismatch_rejected(self):
        with pytest.raises(ev.AttemptEvidenceError):
            ev.FoldSelectionTrace(
                fold_id="fold-00",
                fold_index=1,
                selected=False,
                eligible_symbols_or_pairs=(),
                excluded_symbols_or_pairs=(),
                accepted_input_hash=None,
                rejection_reason="x",
                no_trade_reason_counts={},
            )

    def test_selected_bool_type_enforced(self):
        with pytest.raises(ev.AttemptEvidenceError):
            ev.FoldSelectionTrace(
                fold_id="fold-00",
                fold_index=0,
                selected=1,  # int, not bool
                eligible_symbols_or_pairs=(),
                excluded_symbols_or_pairs=(),
                accepted_input_hash=None,
                rejection_reason="x",
                no_trade_reason_counts={},
            )


class TestUniqueGeneratorEvidence:
    def test_valid_row_constructs(self):
        _unique_evidence_row(0)

    def test_evaluated_decision_units_must_equal_sum(self):
        kwargs = {
            "fold_id": "fold-00",
            "candidate_identity_hash": _hex64("c"),
            "evaluated_decision_units": 99,  # wrong
            "no_signal": 4,
            "candidate": 6,
            "generator_rejected": 4,
            "generator_accepted": 2,
            "generator_rejection_subtotal_by_reason": {"a": 4},
        }
        content_hash = ev._recompute_unique_evidence_hash(_FakeUniqueForHash(**kwargs))
        with pytest.raises(ev.AttemptEvidenceError):
            ev.UniqueGeneratorEvidence(**kwargs, content_hash=content_hash)

    def test_forged_content_hash_rejected(self):
        kwargs = {
            "fold_id": "fold-00",
            "candidate_identity_hash": _hex64("c"),
            "evaluated_decision_units": 10,
            "no_signal": 4,
            "candidate": 6,
            "generator_rejected": 4,
            "generator_accepted": 2,
            "generator_rejection_subtotal_by_reason": {"a": 4},
        }
        with pytest.raises(ev.HashMismatchError):
            ev.UniqueGeneratorEvidence(**kwargs, content_hash=_hex64("forged"))

    def test_generator_rejection_subtotal_must_sum_to_rejected(self):
        kwargs = {
            "fold_id": "fold-00",
            "candidate_identity_hash": _hex64("c"),
            "evaluated_decision_units": 10,
            "no_signal": 4,
            "candidate": 6,
            "generator_rejected": 4,
            "generator_accepted": 2,
            "generator_rejection_subtotal_by_reason": {"a": 1},  # sums to 1, not 4
        }
        content_hash = ev._recompute_unique_evidence_hash(_FakeUniqueForHash(**kwargs))
        with pytest.raises(ev.AttemptEvidenceError):
            ev.UniqueGeneratorEvidence(**kwargs, content_hash=content_hash)


class TestPathScenarioEvidence:
    def test_never_selected_sentinel_valid(self):
        _path_scenario_row("base13", status="never_selected")

    def test_never_selected_with_nonzero_trade_count_rejected(self):
        key = _hex64("t")
        with pytest.raises(ev.AttemptEvidenceError):
            _path_scenario_row(
                "base13",
                status="never_selected",
                trade_count=1,
                member_trade_keys=(key,),
            )

    def test_trade_count_must_equal_member_key_count(self):
        kwargs = {
            "path_scenario": "base13",
            "status": "completed",
            "reason_code": None,
            "trade_count": 3,
            "member_trade_keys": (_hex64("only-one"),),
            "no_trade_reason_counts": {},
        }
        artifact_hash = ev._recompute_path_scenario_hash(_FakePathForHash(**kwargs))
        with pytest.raises(ev.AttemptEvidenceError):
            ev.PathScenarioEvidence(**kwargs, artifact_hash=artifact_hash)

    def test_duplicate_member_trade_keys_rejected(self):
        key = _hex64("dup")
        kwargs = {
            "path_scenario": "base13",
            "status": "completed",
            "reason_code": None,
            "trade_count": 2,
            "member_trade_keys": (key, key),
            "no_trade_reason_counts": {},
        }
        artifact_hash = ev._recompute_path_scenario_hash(_FakePathForHash(**kwargs))
        with pytest.raises(ev.AttemptEvidenceError):
            ev.PathScenarioEvidence(**kwargs, artifact_hash=artifact_hash)

    def test_reason_code_outside_allowlist_rejected(self):
        kwargs = {
            "path_scenario": "base13",
            "status": "rejected",
            "reason_code": "totally_made_up_reason",
            "trade_count": 0,
            "member_trade_keys": (),
            "no_trade_reason_counts": {},
        }
        artifact_hash = ev._recompute_path_scenario_hash(_FakePathForHash(**kwargs))
        with pytest.raises(ev.AttemptEvidenceError):
            ev.PathScenarioEvidence(**kwargs, artifact_hash=artifact_hash)

    def test_forged_artifact_hash_rejected(self):
        with pytest.raises(ev.HashMismatchError):
            ev.PathScenarioEvidence(
                path_scenario="base13",
                status="never_selected",
                reason_code=None,
                trade_count=0,
                member_trade_keys=(),
                no_trade_reason_counts={},
                artifact_hash=_hex64("forged"),
            )

    def test_wrong_path_scenario_membership_not_transferable(self):
        # A row's artifact_hash commits ITS OWN path_scenario -- reusing the
        # exact same trade_count/member_trade_keys under a DIFFERENT
        # path_scenario name must still produce a DIFFERENT hash (never the
        # same evidence silently standing in for another scenario).
        key = _hex64("shared-trade")
        base = _path_scenario_row(
            "base13", status="completed", trade_count=1, member_trade_keys=(key,)
        )
        primary = _path_scenario_row(
            "primary_stress17",
            status="completed",
            trade_count=1,
            member_trade_keys=(key,),
        )
        assert base.artifact_hash != primary.artifact_hash


class TestHistoricalExecutorState:
    def test_default_construction_is_valid(self):
        ev.HistoricalExecutorState()

    def test_order_id_cannot_be_set(self):
        with pytest.raises(ev.AttemptEvidenceError):
            ev.HistoricalExecutorState(order_id="abc123")  # type: ignore[call-arg]

    def test_pair_exec_fail_zero_rejected(self):
        with pytest.raises(ev.AttemptEvidenceError):
            ev.HistoricalExecutorState(pair_exec_fail=0)  # type: ignore[arg-type]

    def test_demo_eligible_true_rejected(self):
        with pytest.raises(ev.AttemptEvidenceError):
            ev.HistoricalExecutorState(demo_eligible=True)


class TestAttemptRecordCore:
    def test_valid_s3_attempt_with_win_constructs(self):
        _build_s3_attempt(selected_index=0)

    def test_valid_s3_attempt_never_selected_constructs(self):
        attempt = _build_s3_attempt(selected_index=None)
        assert all(
            row.status == "never_selected" for row in attempt.path_scenario_evidence
        )

    def test_never_selected_is_not_a_legal_attempt_status(self):
        with pytest.raises(ev.AttemptEvidenceError):
            _build_s3_attempt(status="never_selected", reason_code=None)

    def test_completed_with_reason_code_rejected(self):
        with pytest.raises(ev.AttemptEvidenceError):
            _build_s3_attempt(status="completed", reason_code="should_be_none")

    def test_rejected_without_reason_code_rejected(self):
        with pytest.raises(ev.AttemptEvidenceError):
            _build_s3_attempt(
                status="rejected",
                reason_code=None,
                selected_index=None,
            )

    def test_rejected_with_wrong_reason_code_rejected(self):
        with pytest.raises(ev.AttemptEvidenceError):
            _build_s3_attempt(
                status="rejected",
                reason_code=ev.REASON_CHILD_EXECUTION_TIMEOUT,  # timeout reason under rejected
                selected_index=None,
            )

    def test_caller_extended_reason_allowlist_rejected(self):
        with pytest.raises(ev.AttemptEvidenceError):
            _build_s3_attempt(
                status="crashed",
                reason_code="a_new_reason_the_caller_made_up",
                selected_index=None,
            )

    def test_seven_fold_traces_rejected(self):
        with pytest.raises(ev.AttemptEvidenceError):
            _build_s3_attempt(fold_traces=_all_fold_traces(selected_index=0)[:-1])

    def test_nine_fold_traces_rejected(self):
        traces = list(_all_fold_traces(selected_index=0))
        traces.append(_fold_trace(7))  # duplicate fold-07
        with pytest.raises(ev.AttemptEvidenceError):
            _build_s3_attempt(fold_traces=tuple(traces))

    def test_reordered_fold_traces_rejected(self):
        traces = list(_all_fold_traces(selected_index=0))
        traces[0], traces[1] = traces[1], traces[0]
        with pytest.raises(ev.AttemptEvidenceError):
            _build_s3_attempt(fold_traces=tuple(traces))

    def test_duplicate_fold_index_rejected(self):
        traces = list(_all_fold_traces(selected_index=0))
        traces[7] = _fold_trace(6)  # fold_index=6 duplicated at slot 7
        with pytest.raises(ev.AttemptEvidenceError):
            _build_s3_attempt(fold_traces=tuple(traces))

    def test_unique_evidence_wrong_cardinality_rejected(self):
        with pytest.raises(ev.AttemptEvidenceError):
            _build_s3_attempt(unique_evidence=_all_unique_evidence()[:-1])

    def test_unique_evidence_tripled_across_scenarios_rejected(self):
        # 24 entries (8 folds x 3 scenarios) must never be accepted in place
        # of the required 8 scenario-independent entries.
        tripled = _all_unique_evidence() * 3
        with pytest.raises(ev.AttemptEvidenceError):
            _build_s3_attempt(unique_evidence=tripled)

    def test_path_scenario_wrong_order_rejected(self):
        rows = _completed_scenarios_with_trades()
        reordered = (rows[1], rows[0], rows[2])
        with pytest.raises(ev.AttemptEvidenceError):
            _build_s3_attempt(path_scenario_evidence=reordered)

    def test_path_scenario_wrong_cardinality_rejected(self):
        rows = _completed_scenarios_with_trades()
        with pytest.raises(ev.AttemptEvidenceError):
            _build_s3_attempt(path_scenario_evidence=rows[:2])

    def test_win_with_all_never_selected_scenarios_rejected(self):
        # A config that WON a fold cannot have never_selected in every
        # scenario -- that would fabricate an absence of OOS output for a
        # config that actually won.
        with pytest.raises(ev.AttemptEvidenceError):
            _build_s3_attempt(
                selected_index=0, path_scenario_evidence=_never_selected_scenarios()
            )

    def test_no_win_with_real_trade_scenarios_rejected(self):
        with pytest.raises(ev.AttemptEvidenceError):
            _build_s3_attempt(
                selected_index=None,
                path_scenario_evidence=_completed_scenarios_with_trades(),
            )


class TestS4HistoricalNulls:
    def _s4_attempt(self, **overrides):
        kwargs = {
            "row_id": "S4-00",
            "experiment_id": _hex64("fixture-experiment-S4-00"),
            "campaign_run_id": CAMPAIGN_RUN_ID,
            "full_campaign_hash": FULL_CAMPAIGN_HASH,
            "strategy_key": "ROB974-S4-FIXTURE",
            "retry_index": 0,
            "status": "completed",
            "reason_code": None,
            "fold_traces": _all_fold_traces(selected_index=0),
            "unique_evidence": _all_unique_evidence(),
            "path_scenario_evidence": _completed_scenarios_with_trades(),
            "historical_executor_state": ev.HistoricalExecutorState(),
        }
        kwargs.update(overrides)
        return ev.build_attempt_record(**kwargs)

    def test_completed_s4_requires_historical_executor_state(self):
        with pytest.raises(ev.AttemptEvidenceError):
            self._s4_attempt(historical_executor_state=None)

    def test_s3_attempt_forbids_historical_executor_state(self):
        with pytest.raises(ev.AttemptEvidenceError):
            _build_s3_attempt(historical_executor_state=ev.HistoricalExecutorState())

    def test_valid_s4_completed_attempt_constructs(self):
        self._s4_attempt()

    def test_unknown_strategy_slug_rejected(self):
        with pytest.raises(ev.AttemptEvidenceError):
            self._s4_attempt(row_id="S9-00", experiment_id=_hex64("s9"))


class TestTrustedBoundaryRecompute:
    def test_forged_claimed_fold_evidence_hash_rejected(self):
        with pytest.raises(ev.HashMismatchError):
            _build_s3_attempt(claimed_fold_evidence_hash=_hex64("forged"))

    def test_forged_claimed_run_identity_rejected(self):
        with pytest.raises(ev.HashMismatchError):
            _build_s3_attempt(claimed_run_identity=_hex64("forged"))

    def test_matching_claimed_hashes_accepted(self):
        real = _build_s3_attempt()
        rebuilt = _build_s3_attempt(
            claimed_fold_evidence_hash=real.fold_evidence_hash,
            claimed_run_identity=real.run_identity,
        )
        assert rebuilt.fold_evidence_hash == real.fold_evidence_hash
        assert rebuilt.run_identity == real.run_identity

    def test_direct_construction_with_forged_hashes_rejected(self):
        # AttemptRecord itself (not just build_attempt_record) must
        # independently re-verify -- a caller cannot bypass the boundary by
        # constructing the dataclass directly with a self-consistent-looking
        # but forged pair of hashes.
        with pytest.raises(ev.HashMismatchError):
            ev.AttemptRecord(
                row_id="S3-00",
                experiment_id=EXPERIMENT_ID,
                campaign_run_id=CAMPAIGN_RUN_ID,
                full_campaign_hash=FULL_CAMPAIGN_HASH,
                strategy_key=STRATEGY_KEY,
                retry_index=0,
                status="completed",
                reason_code=None,
                fold_traces=_all_fold_traces(selected_index=0),
                unique_evidence=_all_unique_evidence(),
                path_scenario_evidence=_completed_scenarios_with_trades(),
                fold_evidence_hash=_hex64("forged-fold-hash"),
                run_identity=_hex64("forged-run-identity"),
            )

    def test_one_ulp_style_mutation_changes_run_identity(self):
        base = _build_s3_attempt()
        mutated = _build_s3_attempt(
            experiment_id=_hex64("fixture-experiment-S3-00-mutated")
        )
        assert base.run_identity != mutated.run_identity

    def test_retry_index_changes_run_identity(self):
        base = _build_s3_attempt()
        retry = _build_s3_attempt(retry_index=1)
        assert base.run_identity != retry.run_identity


class TestImmutableSealDeepFreeze:
    """R1 blocker #4: mutable dict fields nested in otherwise-frozen
    dataclasses must be deep-frozen -- `frozen=True` only blocks attribute
    REBINDING, not in-place mutation of a dict the attribute happens to
    hold. A caller mutating `trace.no_trade_reason_counts['x'] = 1` after
    construction must raise, not silently desync the sealed value from the
    hash that was already computed over it."""

    def test_fold_trace_no_trade_reason_counts_rejects_item_assignment(self):
        trace = _fold_trace(0, selected=True)
        with pytest.raises(TypeError):
            trace.no_trade_reason_counts["tampered"] = 1

    def test_unique_evidence_subtotal_rejects_item_assignment(self):
        row = _unique_evidence_row(0)
        with pytest.raises(TypeError):
            row.generator_rejection_subtotal_by_reason["tampered"] = 1

    def test_path_scenario_no_trade_reason_counts_rejects_item_assignment(self):
        row = _path_scenario_row("base13", status="never_selected")
        with pytest.raises(TypeError):
            row.no_trade_reason_counts["tampered"] = 1
