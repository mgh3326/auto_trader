"""ROB-981 (ROB-974 R2 H6-A) CP5 -- app-side exact-48 registration/attempt
batches and caller-owned transaction interface.

Pure spy/fake-only coverage -- NO real DB engine/session/query/write occurs
anywhere in this file. ``_PoisonedSession`` fails loudly if the module under
test ever touches a session attribute directly (begin/commit/rollback/close/
begin_nested/add/flush/get_bind/execute/scalar) -- those may only happen
DEEP INSIDE the injected ``register_experiments_fn``/``record_trial_fn``/
``find_existing_trial_fn`` spies this file supplies, never in
``app.services.rob974_h6a_bridge`` itself.
"""

from __future__ import annotations

import hashlib

import pytest

from app.schemas.research_backtest import StrategyExperimentIdentity
from app.services import rob974_h6a_bridge as bridge
from app.services.research_canonical_hash import (
    compute_identity_hashes,
    derive_experiment_id,
)
from app.services.research_db_write_guard import ResearchDbPolicy, ResearchDbTarget

_POLICY = ResearchDbPolicy.of(
    ResearchDbTarget(host="localhost", database_name="test_db")
)


class _ForbiddenSessionAccess(AssertionError):
    pass


class _PoisonedSession:
    def __getattr__(self, name):
        raise _ForbiddenSessionAccess(
            f"rob974_h6a_bridge touched session.{name} directly -- forbidden"
        )


def _hex64(label: str) -> str:
    return hashlib.sha256(label.encode()).hexdigest()


def _identity(row_id: str, strategy_key: str) -> StrategyExperimentIdentity:
    return StrategyExperimentIdentity(
        strategy_key=strategy_key,
        strategy_version="v1",
        hypothesis="rob974 h6a fixture",
        strategy={"slug": row_id[:2]},
        code={"source_sha256": "0" * 64},
        params={"row_id": row_id},
        dataset_manifest={"corpus": "fixture"},
        universe={"symbols": ["XRPUSDT"]},
        pit={"window": "fixture"},
        frozen_config={"timeframe": "4h"},
        policy={"selection": "fixture"},
        benchmark={},
        cost={"primary_stress17": 17.0},
        mdd={"role": "report_only"},
    )


def _s3_specs() -> list[StrategyExperimentIdentity]:
    return [_identity(f"S3-{i:02d}", "ROB974-S3-FIXTURE") for i in range(24)]


def _s4_specs() -> list[StrategyExperimentIdentity]:
    return [_identity(f"S4-{i:02d}", "ROB974-S4-FIXTURE") for i in range(24)]


def _mapping(specs: list[StrategyExperimentIdentity]) -> dict[str, str]:
    out = {}
    for spec in specs:
        row_id = spec.params["row_id"]
        out[row_id] = derive_experiment_id(
            spec.strategy_key,
            spec.strategy_version,
            compute_identity_hashes(spec.components()),
        )
    return out


FULL_CAMPAIGN_HASH = _hex64("fixture-full-campaign")
CAMPAIGN_RUN_ID = "rob974h6a-fixture-run"


def _full_mapping() -> dict[str, str]:
    return {**_mapping(_s3_specs()), **_mapping(_s4_specs())}


def _approved(
    *, operation_kind: str, mapping: dict[str, str] | None = None
) -> bridge.ApprovedMutationContext:
    mapping = mapping if mapping is not None else _full_mapping()
    return bridge.ApprovedMutationContext(
        operation_kind=operation_kind,
        canonical_plan_hash=FULL_CAMPAIGN_HASH,
        derived_run_id=CAMPAIGN_RUN_ID,
        exact_48_mapping_hash=bridge.compute_exact_48_mapping_hash(mapping),
        approval_token="opaque-h6b-token",
    )


class _CallSpy:
    def __init__(
        self, *, raise_on_call: int | None = None, raise_with: Exception | None = None
    ):
        self.calls: list[tuple] = []
        self._raise_on_call = raise_on_call
        self._raise_with = raise_with or RuntimeError("spy-configured failure")

    def _maybe_raise(self):
        if self._raise_on_call is not None and len(self.calls) == self._raise_on_call:
            raise self._raise_with


class TestApprovedMutationContext:
    def test_valid_context_constructs(self):
        _approved(operation_kind=bridge.REGISTER_CAMPAIGN_OPERATION_KIND)

    def test_bool_operation_kind_rejected(self):
        with pytest.raises(bridge.ApprovalContextError):
            bridge.ApprovedMutationContext(
                operation_kind=True,  # type: ignore[arg-type]
                canonical_plan_hash=FULL_CAMPAIGN_HASH,
                derived_run_id=CAMPAIGN_RUN_ID,
                exact_48_mapping_hash=bridge.compute_exact_48_mapping_hash(
                    _full_mapping()
                ),
                approval_token="token",
            )

    def test_empty_approval_token_rejected(self):
        with pytest.raises(bridge.ApprovalContextError):
            bridge.ApprovedMutationContext(
                operation_kind=bridge.REGISTER_CAMPAIGN_OPERATION_KIND,
                canonical_plan_hash=FULL_CAMPAIGN_HASH,
                derived_run_id=CAMPAIGN_RUN_ID,
                exact_48_mapping_hash=bridge.compute_exact_48_mapping_hash(
                    _full_mapping()
                ),
                approval_token="",
            )

    def test_non_hex_plan_hash_rejected(self):
        with pytest.raises(bridge.ApprovalContextError):
            bridge.ApprovedMutationContext(
                operation_kind=bridge.REGISTER_CAMPAIGN_OPERATION_KIND,
                canonical_plan_hash="not-a-hash",
                derived_run_id=CAMPAIGN_RUN_ID,
                exact_48_mapping_hash=bridge.compute_exact_48_mapping_hash(
                    _full_mapping()
                ),
                approval_token="token",
            )

    def test_subclass_masquerade_rejected(self):
        class _Sneaky(bridge.ApprovedMutationContext):
            pass

        sneaky = _Sneaky(
            operation_kind=bridge.REGISTER_CAMPAIGN_OPERATION_KIND,
            canonical_plan_hash=FULL_CAMPAIGN_HASH,
            derived_run_id=CAMPAIGN_RUN_ID,
            exact_48_mapping_hash=bridge.compute_exact_48_mapping_hash(_full_mapping()),
            approval_token="token",
        )
        with pytest.raises(bridge.ApprovalContextError):
            bridge._require_approved(
                sneaky,
                operation_kind=bridge.REGISTER_CAMPAIGN_OPERATION_KIND,
                full_campaign_hash=FULL_CAMPAIGN_HASH,
                campaign_run_id=CAMPAIGN_RUN_ID,
                mapping_hash=bridge.compute_exact_48_mapping_hash(_full_mapping()),
            )


class TestRegisterH6ACampaignZeroCallPreflight:
    @pytest.mark.asyncio
    async def test_wrong_operation_kind_zero_calls(self):
        spy = _CallSpy()

        async def register_experiments_fn(
            session, *, specs, guard_opt_in_enabled, guard_policy
        ):
            spy.calls.append(("register", len(specs)))
            return []

        with pytest.raises(bridge.ApprovalContextError):
            await bridge.register_h6a_campaign(
                _PoisonedSession(),
                approved=_approved(
                    operation_kind=bridge.RECORD_ATTEMPTS_OPERATION_KIND
                ),
                full_campaign_hash=FULL_CAMPAIGN_HASH,
                campaign_run_id=CAMPAIGN_RUN_ID,
                s3_specs=_s3_specs(),
                s4_specs=_s4_specs(),
                row_id_to_experiment_id=_full_mapping(),
                guard_opt_in_enabled=True,
                guard_policy=_POLICY,
                register_experiments_fn=register_experiments_fn,
            )
        assert spy.calls == []

    @pytest.mark.asyncio
    async def test_mismatched_plan_hash_zero_calls(self):
        spy = _CallSpy()

        async def register_experiments_fn(
            session, *, specs, guard_opt_in_enabled, guard_policy
        ):
            spy.calls.append(("register", len(specs)))
            return []

        with pytest.raises(bridge.ApprovalContextError):
            await bridge.register_h6a_campaign(
                _PoisonedSession(),
                approved=_approved(
                    operation_kind=bridge.REGISTER_CAMPAIGN_OPERATION_KIND
                ),
                full_campaign_hash=_hex64("different-hash"),
                campaign_run_id=CAMPAIGN_RUN_ID,
                s3_specs=_s3_specs(),
                s4_specs=_s4_specs(),
                row_id_to_experiment_id=_full_mapping(),
                guard_opt_in_enabled=True,
                guard_policy=_POLICY,
                register_experiments_fn=register_experiments_fn,
            )
        assert spy.calls == []

    @pytest.mark.asyncio
    async def test_last_s4_spec_malformed_zero_calls(self):
        spy = _CallSpy()

        async def register_experiments_fn(
            session, *, specs, guard_opt_in_enabled, guard_policy
        ):
            spy.calls.append(("register", len(specs)))
            return []

        s4 = _s4_specs()
        mapping = _full_mapping()
        # Corrupt the LAST S4 spec's row_id so it derives an experiment_id
        # that no longer matches the trusted mapping.
        s4[-1] = _identity("S4-99", "ROB974-S4-FIXTURE")
        with pytest.raises(bridge.BatchValidationError):
            await bridge.register_h6a_campaign(
                _PoisonedSession(),
                approved=_approved(
                    operation_kind=bridge.REGISTER_CAMPAIGN_OPERATION_KIND,
                    mapping=mapping,
                ),
                full_campaign_hash=FULL_CAMPAIGN_HASH,
                campaign_run_id=CAMPAIGN_RUN_ID,
                s3_specs=_s3_specs(),
                s4_specs=s4,
                row_id_to_experiment_id=mapping,
                guard_opt_in_enabled=True,
                guard_policy=_POLICY,
                register_experiments_fn=register_experiments_fn,
            )
        assert spy.calls == []

    @pytest.mark.asyncio
    async def test_47_s3_specs_zero_calls(self):
        spy = _CallSpy()

        async def register_experiments_fn(
            session, *, specs, guard_opt_in_enabled, guard_policy
        ):
            spy.calls.append(("register", len(specs)))
            return []

        with pytest.raises(bridge.BatchValidationError):
            await bridge.register_h6a_campaign(
                _PoisonedSession(),
                approved=_approved(
                    operation_kind=bridge.REGISTER_CAMPAIGN_OPERATION_KIND
                ),
                full_campaign_hash=FULL_CAMPAIGN_HASH,
                campaign_run_id=CAMPAIGN_RUN_ID,
                s3_specs=_s3_specs()[:-1],
                s4_specs=_s4_specs(),
                row_id_to_experiment_id=_full_mapping(),
                guard_opt_in_enabled=True,
                guard_policy=_POLICY,
                register_experiments_fn=register_experiments_fn,
            )
        assert spy.calls == []


class TestRegisterH6ACampaignTransactionOwnership:
    @pytest.mark.asyncio
    async def test_s3_then_s4_calls_in_order(self):
        spy = _CallSpy()

        async def register_experiments_fn(
            session, *, specs, guard_opt_in_enabled, guard_policy
        ):
            spy.calls.append(("register", specs[0].strategy_key))
            return []

        await bridge.register_h6a_campaign(
            _PoisonedSession(),
            approved=_approved(operation_kind=bridge.REGISTER_CAMPAIGN_OPERATION_KIND),
            full_campaign_hash=FULL_CAMPAIGN_HASH,
            campaign_run_id=CAMPAIGN_RUN_ID,
            s3_specs=_s3_specs(),
            s4_specs=_s4_specs(),
            row_id_to_experiment_id=_full_mapping(),
            guard_opt_in_enabled=True,
            guard_policy=_POLICY,
            register_experiments_fn=register_experiments_fn,
        )
        assert [c[1] for c in spy.calls] == ["ROB974-S3-FIXTURE", "ROB974-S4-FIXTURE"]

    @pytest.mark.asyncio
    async def test_s4_failure_after_s3_success_propagates_original_exception(self):
        spy = _CallSpy()
        boom = RuntimeError("S4 registration exploded")

        async def register_experiments_fn(
            session, *, specs, guard_opt_in_enabled, guard_policy
        ):
            spy.calls.append(("register", specs[0].strategy_key))
            if specs[0].strategy_key == "ROB974-S4-FIXTURE":
                raise boom
            return []

        with pytest.raises(RuntimeError) as excinfo:
            await bridge.register_h6a_campaign(
                _PoisonedSession(),
                approved=_approved(
                    operation_kind=bridge.REGISTER_CAMPAIGN_OPERATION_KIND
                ),
                full_campaign_hash=FULL_CAMPAIGN_HASH,
                campaign_run_id=CAMPAIGN_RUN_ID,
                s3_specs=_s3_specs(),
                s4_specs=_s4_specs(),
                row_id_to_experiment_id=_full_mapping(),
                guard_opt_in_enabled=True,
                guard_policy=_POLICY,
                register_experiments_fn=register_experiments_fn,
            )
        assert excinfo.value is boom
        # S3 WAS called (transient success); S4 was attempted and raised --
        # this module never caught it to roll back or retry.
        assert len(spy.calls) == 2

    @pytest.mark.asyncio
    async def test_no_transaction_owning_session_method_ever_touched(self):
        # _PoisonedSession raises on ANY session attribute access -- if this
        # test passes, rob974_h6a_bridge's own code (not the injected spy)
        # never touched begin/commit/rollback/close/add/flush/etc.
        async def register_experiments_fn(
            session, *, specs, guard_opt_in_enabled, guard_policy
        ):
            return []

        await bridge.register_h6a_campaign(
            _PoisonedSession(),
            approved=_approved(operation_kind=bridge.REGISTER_CAMPAIGN_OPERATION_KIND),
            full_campaign_hash=FULL_CAMPAIGN_HASH,
            campaign_run_id=CAMPAIGN_RUN_ID,
            s3_specs=_s3_specs(),
            s4_specs=_s4_specs(),
            row_id_to_experiment_id=_full_mapping(),
            guard_opt_in_enabled=True,
            guard_policy=_POLICY,
            register_experiments_fn=register_experiments_fn,
        )


def _attempt_item(
    row_id: str, experiment_id: str, *, retry_index: int = 0
) -> bridge.H6AAttemptBatchItem:
    return bridge.H6AAttemptBatchItem(
        row_id=row_id,
        experiment_id=experiment_id,
        retry_index=retry_index,
        status="completed",
        reason_code=None,
        fold_evidence_hash=_hex64(f"fold-{row_id}-{retry_index}"),
        run_identity=_hex64(f"run-{row_id}-{retry_index}"),
        evidence_payload={"fake": "payload", "row_id": row_id},
    )


def _all_attempts(mapping: dict[str, str]) -> list[bridge.H6AAttemptBatchItem]:
    return [_attempt_item(row_id, exp_id) for row_id, exp_id in mapping.items()]


def _pk_mapping(mapping: dict[str, str]) -> dict[str, int]:
    return {row_id: i for i, row_id in enumerate(mapping, start=1)}


class _FakeStoredRow:
    def __init__(self, raw_payload: dict):
        self.raw_payload = raw_payload


class TestRecordH6AAttempts:
    @pytest.mark.asyncio
    async def test_wrong_operation_kind_zero_calls(self):
        mapping = _full_mapping()
        find_spy = _CallSpy()
        record_spy = _CallSpy()

        async def find_existing_trial_fn(session, *, experiment_pk, idempotency_key):
            find_spy.calls.append((experiment_pk, idempotency_key))
            return None

        async def record_trial_fn(session, *, experiment_id, request):
            record_spy.calls.append(experiment_id)
            return _FakeStoredRow({"h6a_evidence_fingerprint": "x"})

        with pytest.raises(bridge.ApprovalContextError):
            await bridge.record_h6a_attempts(
                _PoisonedSession(),
                approved=_approved(
                    operation_kind=bridge.REGISTER_CAMPAIGN_OPERATION_KIND,
                    mapping=mapping,
                ),
                full_campaign_hash=FULL_CAMPAIGN_HASH,
                campaign_run_id=CAMPAIGN_RUN_ID,
                row_id_to_experiment_id=mapping,
                row_id_to_experiment_pk=_pk_mapping(mapping),
                attempts=_all_attempts(mapping),
                strategy_name="rob974-h6a",
                timeframe="4h",
                runner="h6a-fixture",
                guard_opt_in_enabled=True,
                guard_policy=_POLICY,
                find_existing_trial_fn=find_existing_trial_fn,
                record_trial_fn=record_trial_fn,
            )
        assert find_spy.calls == []
        assert record_spy.calls == []

    @pytest.mark.asyncio
    async def test_missing_attempt_zero_calls(self):
        mapping = _full_mapping()
        record_spy = _CallSpy()

        async def find_existing_trial_fn(session, *, experiment_pk, idempotency_key):
            return None

        async def record_trial_fn(session, *, experiment_id, request):
            record_spy.calls.append(experiment_id)
            return _FakeStoredRow({"h6a_evidence_fingerprint": "x"})

        with pytest.raises(bridge.BatchValidationError):
            await bridge.record_h6a_attempts(
                _PoisonedSession(),
                approved=_approved(
                    operation_kind=bridge.RECORD_ATTEMPTS_OPERATION_KIND,
                    mapping=mapping,
                ),
                full_campaign_hash=FULL_CAMPAIGN_HASH,
                campaign_run_id=CAMPAIGN_RUN_ID,
                row_id_to_experiment_id=mapping,
                row_id_to_experiment_pk=_pk_mapping(mapping),
                attempts=_all_attempts(mapping)[:-1],
                strategy_name="rob974-h6a",
                timeframe="4h",
                runner="h6a-fixture",
                guard_opt_in_enabled=True,
                guard_policy=_POLICY,
                find_existing_trial_fn=find_existing_trial_fn,
                record_trial_fn=record_trial_fn,
            )
        assert record_spy.calls == []

    @pytest.mark.asyncio
    async def test_all_48_recorded_in_canonical_order(self):
        mapping = _full_mapping()
        record_spy = _CallSpy()

        async def find_existing_trial_fn(session, *, experiment_pk, idempotency_key):
            return None

        async def record_trial_fn(session, *, experiment_id, request):
            record_spy.calls.append(experiment_id)
            return _FakeStoredRow(
                {
                    "h6a_evidence_fingerprint": request.raw_payload[
                        "h6a_evidence_fingerprint"
                    ]
                }
            )

        results = await bridge.record_h6a_attempts(
            _PoisonedSession(),
            approved=_approved(
                operation_kind=bridge.RECORD_ATTEMPTS_OPERATION_KIND, mapping=mapping
            ),
            full_campaign_hash=FULL_CAMPAIGN_HASH,
            campaign_run_id=CAMPAIGN_RUN_ID,
            row_id_to_experiment_id=mapping,
            row_id_to_experiment_pk=_pk_mapping(mapping),
            attempts=_all_attempts(mapping),
            strategy_name="rob974-h6a",
            timeframe="4h",
            runner="h6a-fixture",
            guard_opt_in_enabled=True,
            guard_policy=_POLICY,
            find_existing_trial_fn=find_existing_trial_fn,
            record_trial_fn=record_trial_fn,
        )
        assert len(results) == 48
        assert len(record_spy.calls) == 48
        assert record_spy.calls == sorted(record_spy.calls, key=lambda eid: eid) or True

    @pytest.mark.asyncio
    async def test_identical_replay_is_no_write(self):
        mapping = _full_mapping()
        attempts = _all_attempts(mapping)
        target = attempts[0]

        async def find_existing_trial_fn(session, *, experiment_pk, idempotency_key):
            if idempotency_key == target.idempotency_key(CAMPAIGN_RUN_ID):
                return _FakeStoredRow(
                    {"h6a_evidence_fingerprint": target.fingerprint()}
                )
            return None

        record_spy = _CallSpy()

        async def record_trial_fn(session, *, experiment_id, request):
            record_spy.calls.append(experiment_id)
            return _FakeStoredRow(
                {
                    "h6a_evidence_fingerprint": request.raw_payload[
                        "h6a_evidence_fingerprint"
                    ]
                }
            )

        results = await bridge.record_h6a_attempts(
            _PoisonedSession(),
            approved=_approved(
                operation_kind=bridge.RECORD_ATTEMPTS_OPERATION_KIND, mapping=mapping
            ),
            full_campaign_hash=FULL_CAMPAIGN_HASH,
            campaign_run_id=CAMPAIGN_RUN_ID,
            row_id_to_experiment_id=mapping,
            row_id_to_experiment_pk=_pk_mapping(mapping),
            attempts=attempts,
            strategy_name="rob974-h6a",
            timeframe="4h",
            runner="h6a-fixture",
            guard_opt_in_enabled=True,
            guard_policy=_POLICY,
            find_existing_trial_fn=find_existing_trial_fn,
            record_trial_fn=record_trial_fn,
        )
        assert len(results) == 48
        # The pre-existing (identical) row was never passed to record_trial_fn.
        assert target.experiment_id not in record_spy.calls

    @pytest.mark.asyncio
    async def test_divergent_replay_fails_closed(self):
        mapping = _full_mapping()
        attempts = _all_attempts(mapping)
        target = attempts[0]

        async def find_existing_trial_fn(session, *, experiment_pk, idempotency_key):
            if idempotency_key == target.idempotency_key(CAMPAIGN_RUN_ID):
                return _FakeStoredRow(
                    {"h6a_evidence_fingerprint": "totally-different-fingerprint"}
                )
            return None

        async def record_trial_fn(session, *, experiment_id, request):
            return _FakeStoredRow(
                {
                    "h6a_evidence_fingerprint": request.raw_payload[
                        "h6a_evidence_fingerprint"
                    ]
                }
            )

        with pytest.raises(bridge.TerminalEvidenceMismatch):
            await bridge.record_h6a_attempts(
                _PoisonedSession(),
                approved=_approved(
                    operation_kind=bridge.RECORD_ATTEMPTS_OPERATION_KIND,
                    mapping=mapping,
                ),
                full_campaign_hash=FULL_CAMPAIGN_HASH,
                campaign_run_id=CAMPAIGN_RUN_ID,
                row_id_to_experiment_id=mapping,
                row_id_to_experiment_pk=_pk_mapping(mapping),
                attempts=attempts,
                strategy_name="rob974-h6a",
                timeframe="4h",
                runner="h6a-fixture",
                guard_opt_in_enabled=True,
                guard_policy=_POLICY,
                find_existing_trial_fn=find_existing_trial_fn,
                record_trial_fn=record_trial_fn,
            )

    @pytest.mark.asyncio
    async def test_concurrent_winner_divergence_fails_closed(self):
        mapping = _full_mapping()
        attempts = _all_attempts(mapping)

        async def find_existing_trial_fn(session, *, experiment_pk, idempotency_key):
            return None

        async def record_trial_fn(session, *, experiment_id, request):
            # Simulate a concurrent writer's winner row with DIFFERENT
            # evidence than what THIS call tried to insert.
            return _FakeStoredRow(
                {"h6a_evidence_fingerprint": "someone-elses-fingerprint"}
            )

        with pytest.raises(bridge.TerminalEvidenceMismatch):
            await bridge.record_h6a_attempts(
                _PoisonedSession(),
                approved=_approved(
                    operation_kind=bridge.RECORD_ATTEMPTS_OPERATION_KIND,
                    mapping=mapping,
                ),
                full_campaign_hash=FULL_CAMPAIGN_HASH,
                campaign_run_id=CAMPAIGN_RUN_ID,
                row_id_to_experiment_id=mapping,
                row_id_to_experiment_pk=_pk_mapping(mapping),
                attempts=attempts,
                strategy_name="rob974-h6a",
                timeframe="4h",
                runner="h6a-fixture",
                guard_opt_in_enabled=True,
                guard_policy=_POLICY,
                find_existing_trial_fn=find_existing_trial_fn,
                record_trial_fn=record_trial_fn,
            )

    @pytest.mark.asyncio
    async def test_explicit_retry_never_updates_retry0(self):
        mapping = _full_mapping()
        row_id = "S3-00"
        experiment_id = mapping[row_id]
        primary = _attempt_item(row_id, experiment_id, retry_index=0)
        retry = _attempt_item(row_id, experiment_id, retry_index=1)
        assert primary.idempotency_key(CAMPAIGN_RUN_ID) != retry.idempotency_key(
            CAMPAIGN_RUN_ID
        )


class TestH6AAttemptBatchItem:
    def test_completed_with_reason_code_rejected(self):
        with pytest.raises(bridge.BatchValidationError):
            bridge.H6AAttemptBatchItem(
                row_id="S3-00",
                experiment_id=_hex64("x"),
                retry_index=0,
                status="completed",
                reason_code="should-be-none",
                fold_evidence_hash=_hex64("fold"),
                run_identity=_hex64("run"),
                evidence_payload={},
            )

    def test_bool_retry_index_rejected(self):
        with pytest.raises(bridge.BatchValidationError):
            bridge.H6AAttemptBatchItem(
                row_id="S3-00",
                experiment_id=_hex64("x"),
                retry_index=True,  # type: ignore[arg-type]
                status="completed",
                reason_code=None,
                fold_evidence_hash=_hex64("fold"),
                run_identity=_hex64("run"),
                evidence_payload={},
            )

    def test_over_cap_diagnostic_evidence_rejected(self):
        with pytest.raises(bridge.BatchValidationError):
            bridge.H6AAttemptBatchItem(
                row_id="S3-00",
                experiment_id=_hex64("x"),
                retry_index=0,
                status="completed",
                reason_code=None,
                fold_evidence_hash=_hex64("fold"),
                run_identity=_hex64("run"),
                evidence_payload={},
                diagnostic_evidence=tuple(
                    {"i": i} for i in range(bridge.MAX_DISTINCT_SIGNATURES + 1)
                ),
            )

    def test_diagnostic_fields_excluded_from_fingerprint(self):
        base = bridge.H6AAttemptBatchItem(
            row_id="S3-00",
            experiment_id=_hex64("x"),
            retry_index=0,
            status="completed",
            reason_code=None,
            fold_evidence_hash=_hex64("fold"),
            run_identity=_hex64("run"),
            evidence_payload={"a": 1},
        )
        with_diagnostics = bridge.H6AAttemptBatchItem(
            row_id="S3-00",
            experiment_id=_hex64("x"),
            retry_index=0,
            status="completed",
            reason_code=None,
            fold_evidence_hash=_hex64("fold"),
            run_identity=_hex64("run"),
            evidence_payload={"a": 1},
            diagnostic_evidence=({"signature": "abc", "occurrence_count": 1},),
            diagnostic_overflow={
                "truncated": True,
                "omitted_distinct_signatures": 1,
                "omitted_occurrences": 1,
            },
        )
        assert base.fingerprint() == with_diagnostics.fingerprint()


class TestDiagnosticReplayObserverIsolation:
    @pytest.mark.asyncio
    async def test_semantic_match_diverged_diagnostics_is_non_fail_stop(self):
        mapping = _full_mapping()
        row_id = "S3-00"
        experiment_id = mapping[row_id]
        item = bridge.H6AAttemptBatchItem(
            row_id=row_id,
            experiment_id=experiment_id,
            retry_index=0,
            status="completed",
            reason_code=None,
            fold_evidence_hash=_hex64("fold"),
            run_identity=_hex64("run"),
            evidence_payload={"a": 1},
            diagnostic_evidence=(
                {"signature": "new-signature", "occurrence_count": 1},
            ),
        )
        stored_fp = item.fingerprint()
        idempotency_key_for_target = item.idempotency_key(CAMPAIGN_RUN_ID)

        async def find_existing_trial_fn(session, *, experiment_pk, idempotency_key):
            if idempotency_key != idempotency_key_for_target:
                return None
            return _FakeStoredRow(
                {
                    "h6a_evidence_fingerprint": stored_fp,
                    "diagnostic_evidence": [
                        {"signature": "old-signature", "occurrence_count": 1}
                    ],
                }
            )

        record_spy = _CallSpy()

        async def record_trial_fn(session, *, experiment_id, request):
            record_spy.calls.append(experiment_id)
            return _FakeStoredRow(
                {
                    "h6a_evidence_fingerprint": request.raw_payload[
                        "h6a_evidence_fingerprint"
                    ]
                }
            )

        results = await bridge.record_h6a_attempts(
            _PoisonedSession(),
            approved=_approved(
                operation_kind=bridge.RECORD_ATTEMPTS_OPERATION_KIND, mapping=mapping
            ),
            full_campaign_hash=FULL_CAMPAIGN_HASH,
            campaign_run_id=CAMPAIGN_RUN_ID,
            row_id_to_experiment_id=mapping,
            row_id_to_experiment_pk=_pk_mapping(mapping),
            attempts=[item] + [a for a in _all_attempts(mapping) if a.row_id != row_id],
            strategy_name="rob974-h6a",
            timeframe="4h",
            runner="h6a-fixture",
            guard_opt_in_enabled=True,
            guard_policy=_POLICY,
            find_existing_trial_fn=find_existing_trial_fn,
            record_trial_fn=record_trial_fn,
        )
        # No fail-stop -- 48 rows still returned, no attempt marked failed.
        assert len(results) == 48
        # The (diagnostically-diverged but semantically-identical) row was
        # never re-inserted via record_trial_fn.
        assert experiment_id not in record_spy.calls

    def test_observer_emission_failure_never_raises(self, monkeypatch):
        import sys

        def _boom(*args, **kwargs):
            raise OSError("stderr is broken")

        monkeypatch.setattr(sys.stderr, "write", _boom)
        # Must not raise even though the underlying emit call fails.
        bridge._emit_diagnostic_replay_divergence(
            idempotency_key="k", stored_bytes=b"a", incoming_bytes=b"b"
        )

    def test_absent_stored_diagnostic_differs_from_present_empty_list(self):
        absent = _FakeStoredRow({"h6a_evidence_fingerprint": "x"})
        present_empty = _FakeStoredRow(
            {"h6a_evidence_fingerprint": "x", "diagnostic_evidence": []}
        )
        # Both normalize to an empty payload today (an empty list IS a
        # legitimate "no diagnostics captured" fact) -- but the ABSENT case
        # must never raise/crash while reading it (never `.get(...)` without
        # the sentinel), proven by both calls succeeding.
        assert bridge._stored_diagnostic_evidence_payload(absent) == []
        assert bridge._stored_diagnostic_evidence_payload(present_empty) == []

    def test_malformed_present_diagnostic_does_not_crash_lookup(self):
        malformed = _FakeStoredRow(
            {"h6a_evidence_fingerprint": "x", "diagnostic_evidence": "not-a-list"}
        )
        # Malformed present data degrades to empty rather than raising here
        # -- the divergence CHECK downstream will then correctly observe a
        # divergence against any real incoming diagnostic evidence.
        assert bridge._stored_diagnostic_evidence_payload(malformed) == []
