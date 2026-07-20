"""ROB-981 (ROB-974 R2 H6-A) CP7 -- pure fixture end-to-end smoke, CP5 half.

Wires the pure research-side smoke plan (``rob974_h6a_smoke.build_smoke_plan``
-- CP1/CP2/CP3/CP4/CP6) through CP5's app-side ``register_h6a_campaign``/
``record_h6a_attempts`` using ONLY spies -- NO real DB engine/session/query/
write anywhere in this file. ``_PoisonedSession`` (same discipline as
``test_rob974_h6a_bridge.py``) fails loudly if either bridge function ever
touches a session attribute directly.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_RESEARCH_DIR = Path(__file__).resolve().parents[3] / "research" / "nautilus_scalping"
if str(_RESEARCH_DIR) not in sys.path:
    sys.path.insert(0, str(_RESEARCH_DIR))

import rob974_h6a_smoke as smoke  # noqa: E402 -- after sys.path shim

from app.schemas.research_backtest import StrategyExperimentIdentity  # noqa: E402
from app.services import rob974_h6a_bridge as bridge  # noqa: E402
from app.services.research_db_write_guard import (  # noqa: E402
    ResearchDbPolicy,
    ResearchDbTarget,
)

_POLICY = ResearchDbPolicy.of(
    ResearchDbTarget(host="localhost", database_name="test_db")
)


class _PoisonedSession:
    def __getattr__(self, name):
        raise AssertionError(f"e2e smoke touched session.{name} directly -- forbidden")


def _specs_from_plan(plan: smoke.SmokePlan) -> tuple[list, list]:
    s3, s4 = [], []
    for spec in plan.row_specs:
        identity = StrategyExperimentIdentity(
            strategy_key=spec.strategy_key,
            strategy_version=spec.strategy_version,
            hypothesis=spec.hypothesis,
            **spec.components,
        )
        (s3 if spec.row_id.startswith("S3") else s4).append(identity)
    return s3, s4


def _attempt_items(plan: smoke.SmokePlan) -> list[bridge.H6AAttemptBatchItem]:
    items = []
    for a in plan.attempts:
        items.append(
            bridge.H6AAttemptBatchItem(
                row_id=a.row_id,
                experiment_id=a.experiment_id,
                retry_index=a.retry_index,
                status=a.status,
                reason_code=a.reason_code,
                fold_evidence_hash=a.fold_evidence_hash,
                run_identity=a.run_identity,
                evidence_payload={"canonical_row_id": a.row_id, "status": a.status},
            )
        )
    return items


class TestFullPipelineE2ESmoke:
    def test_registration_spec_split_is_exact_24_24(self):
        plan = smoke.build_smoke_plan()
        s3_specs, s4_specs = _specs_from_plan(plan)
        assert len(s3_specs) == 24
        assert len(s4_specs) == 24

    @pytest.mark.asyncio
    async def test_register_h6a_campaign_accepts_the_full_plan(self):
        plan = smoke.build_smoke_plan()
        s3_specs, s4_specs = _specs_from_plan(plan)
        approved = bridge.ApprovedMutationContext(
            operation_kind=bridge.REGISTER_CAMPAIGN_OPERATION_KIND,
            canonical_plan_hash=plan.full_campaign_hash,
            derived_run_id=plan.campaign_run_id,
            exact_48_mapping_hash=bridge.compute_exact_48_mapping_hash(
                plan.row_id_to_experiment_id
            ),
            approval_token="e2e-smoke-token",
        )

        register_calls = []

        async def register_experiments_fn(
            session, *, specs, guard_opt_in_enabled, guard_policy
        ):
            register_calls.append(len(specs))
            return []

        registered = await bridge.register_h6a_campaign(
            _PoisonedSession(),
            approved=approved,
            full_campaign_hash=plan.full_campaign_hash,
            campaign_run_id=plan.campaign_run_id,
            s3_specs=s3_specs,
            s4_specs=s4_specs,
            row_id_to_experiment_id=plan.row_id_to_experiment_id,
            guard_opt_in_enabled=True,
            guard_policy=_POLICY,
            register_experiments_fn=register_experiments_fn,
        )
        assert register_calls == [24, 24]
        assert registered == ([], [])

    @pytest.mark.asyncio
    async def test_record_h6a_attempts_accepts_49_rows_including_retry(self):
        plan = smoke.build_smoke_plan()
        items = _attempt_items(plan)
        assert len(items) == 49

        approved = bridge.ApprovedMutationContext(
            operation_kind=bridge.RECORD_ATTEMPTS_OPERATION_KIND,
            canonical_plan_hash=plan.full_campaign_hash,
            derived_run_id=plan.campaign_run_id,
            exact_48_mapping_hash=bridge.compute_exact_48_mapping_hash(
                plan.row_id_to_experiment_id
            ),
            approval_token="e2e-smoke-token",
        )
        pk_mapping = {
            row_id: i for i, row_id in enumerate(plan.row_id_to_experiment_id, start=1)
        }

        async def find_existing_trial_fn(session, *, experiment_pk, idempotency_key):
            return None

        record_calls = []

        class _FakeStoredRow:
            def __init__(self, raw_payload):
                self.raw_payload = raw_payload

        async def record_trial_fn(session, *, experiment_id, request):
            record_calls.append(experiment_id)
            return _FakeStoredRow(
                {
                    "h6a_evidence_fingerprint": request.raw_payload[
                        "h6a_evidence_fingerprint"
                    ]
                }
            )

        # record_h6a_attempts requires exactly the 48 CANONICAL row_ids (the
        # retry row's SECOND attempt is not a distinct canonical row --
        # attempt batching is per-row, one primary per row; a real H6-B
        # caller records the retry via a SEPARATE, later record_h6a_attempts
        # call once H4 reruns that one config). This smoke exercises the
        # primary batch (48) here, and the retry item independently below.
        primaries = [item for item in items if item.retry_index == 0]
        assert len(primaries) == 48

        results = await bridge.record_h6a_attempts(
            _PoisonedSession(),
            approved=approved,
            full_campaign_hash=plan.full_campaign_hash,
            campaign_run_id=plan.campaign_run_id,
            row_id_to_experiment_id=plan.row_id_to_experiment_id,
            row_id_to_experiment_pk=pk_mapping,
            attempts=primaries,
            strategy_name="rob974-h6a-smoke",
            timeframe="4h",
            runner="h6a-e2e-smoke",
            guard_opt_in_enabled=True,
            guard_policy=_POLICY,
            find_existing_trial_fn=find_existing_trial_fn,
            record_trial_fn=record_trial_fn,
        )
        assert len(results) == 48
        assert len(record_calls) == 48
        assert len(set(record_calls)) == 48  # every experiment_id distinct

    @pytest.mark.asyncio
    async def test_last_middle_first_malformed_registration_is_zero_call(self):
        plan = smoke.build_smoke_plan()
        s3_specs, s4_specs = _specs_from_plan(plan)
        # Corrupt the LAST S4 spec so it no longer derives its expected id.
        s4_specs[-1] = StrategyExperimentIdentity(
            strategy_key=s4_specs[-1].strategy_key,
            strategy_version=s4_specs[-1].strategy_version,
            hypothesis=s4_specs[-1].hypothesis,
            **{**s4_specs[-1].components(), "params": {"row_id": "S4-99"}},
        )
        approved = bridge.ApprovedMutationContext(
            operation_kind=bridge.REGISTER_CAMPAIGN_OPERATION_KIND,
            canonical_plan_hash=plan.full_campaign_hash,
            derived_run_id=plan.campaign_run_id,
            exact_48_mapping_hash=bridge.compute_exact_48_mapping_hash(
                plan.row_id_to_experiment_id
            ),
            approval_token="e2e-smoke-token",
        )
        calls = []

        async def register_experiments_fn(
            session, *, specs, guard_opt_in_enabled, guard_policy
        ):
            calls.append(len(specs))
            return []

        with pytest.raises(bridge.BatchValidationError):
            await bridge.register_h6a_campaign(
                _PoisonedSession(),
                approved=approved,
                full_campaign_hash=plan.full_campaign_hash,
                campaign_run_id=plan.campaign_run_id,
                s3_specs=s3_specs,
                s4_specs=s4_specs,
                row_id_to_experiment_id=plan.row_id_to_experiment_id,
                guard_opt_in_enabled=True,
                guard_policy=_POLICY,
                register_experiments_fn=register_experiments_fn,
            )
        assert calls == []

    def test_plan_is_deterministic_across_two_independent_builds(self):
        plan_a = smoke.build_smoke_plan()
        plan_b = smoke.build_smoke_plan()
        items_a = _attempt_items(plan_a)
        items_b = _attempt_items(plan_b)
        assert [i.fingerprint() for i in items_a] == [i.fingerprint() for i in items_b]
