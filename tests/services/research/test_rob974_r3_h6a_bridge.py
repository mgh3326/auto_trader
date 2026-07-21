"""ROB-974 R3 app-side literal exact-12 mutation guard tests."""

from __future__ import annotations

import dataclasses
import hashlib

import pytest

from app.schemas.research_backtest import StrategyExperimentIdentity
from app.services import rob974_r3_h6a_bridge as bridge
from app.services.research_canonical_hash import (
    compute_identity_hashes,
    derive_experiment_id,
)
from app.services.research_db_write_guard import ResearchDbPolicy, ResearchDbTarget

_POLICY = ResearchDbPolicy.of(
    ResearchDbTarget(host="localhost", database_name="test_db")
)


def _hex(label: str) -> str:
    return hashlib.sha256(label.encode()).hexdigest()


def _identity(row_id: str, strategy_key: str) -> StrategyExperimentIdentity:
    return StrategyExperimentIdentity(
        strategy_key=strategy_key,
        strategy_version="r3.v1",
        hypothesis="rob974 r3 frozen hypothesis",
        strategy={"slug": row_id[:2], "lineage": "R3"},
        code={"source_sha256": _hex("code")},
        params={"row_id": row_id},
        dataset_manifest={"corpus": "production-shaped-fixture"},
        universe={"symbols": ["XRPUSDT", "DOGEUSDT", "SOLUSDT"]},
        pit={"window": "2025-07-01/2026-07-01"},
        frozen_config={"fold_count": 8},
        policy={"selection": "all_cell_train_and_oos"},
        benchmark={},
        cost={"scenarios": [13, 17, 22]},
        mdd={"role": "report_only"},
    )


def _s3_specs() -> tuple[StrategyExperimentIdentity, ...]:
    return tuple(_identity(f"S3-R3-{index:02d}", "rob974.r3.s3") for index in range(3))


def _s4_specs() -> tuple[StrategyExperimentIdentity, ...]:
    return tuple(_identity(f"S4-R3-{index:02d}", "rob974.r3.s4") for index in range(9))


def _mapping() -> dict[str, str]:
    result: dict[str, str] = {}
    for spec in (*_s3_specs(), *_s4_specs()):
        result[spec.params["row_id"]] = derive_experiment_id(
            spec.strategy_key,
            spec.strategy_version,
            compute_identity_hashes(spec.components()),
        )
    return result


def _attempts() -> tuple[bridge.R3AttemptBatchItem, ...]:
    return tuple(
        bridge.R3AttemptBatchItem(
            row_id=row_id,
            experiment_id=experiment_id,
            retry_index=0,
            status="completed",
            reason_code=None,
            fold_evidence_hash=_hex(f"folds:{row_id}"),
            run_identity=_hex(f"run:{row_id}"),
            evidence_payload={"row_id": row_id, "fold_count": 8},
        )
        for row_id, experiment_id in _mapping().items()
    )


def _pks() -> dict[str, int]:
    return {
        row_id: index
        for index, row_id in enumerate(bridge.R3_CANONICAL_ROW_ORDER, start=1)
    }


_FULL_HASH = _hex("rob974-r3-full-campaign")
_RUN_ID = bridge.derive_r3_campaign_run_id(_FULL_HASH)


def _approved(operation_kind: str) -> bridge.R3ApprovedMutationContext:
    return bridge.R3ApprovedMutationContext(
        operation_kind=operation_kind,
        canonical_plan_hash=_FULL_HASH,
        derived_run_id=_RUN_ID,
        exact_12_mapping_hash=bridge.compute_exact_12_mapping_hash(_mapping()),
        approval_token="opaque-r3-approval",
    )


class _Registered:
    def __init__(self, spec: StrategyExperimentIdentity):
        hashes = compute_identity_hashes(spec.components())
        self.experiment_id = derive_experiment_id(
            spec.strategy_key, spec.strategy_version, hashes
        )
        self.strategy_key = spec.strategy_key
        self.strategy_version = spec.strategy_version
        for name, value in hashes.items():
            setattr(self, name, value)


def test_app_literal_mapping_rejects_11_13_split_order_duplicates_and_r2_rows() -> None:
    mapping = _mapping()
    items = list(mapping.items())
    mutants: tuple[object, ...] = (
        dict(items[:-1]),
        dict((*items, ("S4-R3-09", _hex("extra")))),
        dict((items[1], items[0], *items[2:])),
        dict((*items[:-1], (items[-1][0], items[0][1]))),
        dict((("S3-00", items[0][1]), *items[1:])),
        tuple(items),
    )
    for mutant in mutants:
        with pytest.raises(bridge.Exact12BatchValidationError):
            bridge.compute_exact_12_mapping_hash(mutant)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_complete_12_preflight_finishes_before_first_family_primitive() -> None:
    calls: list[int] = []

    async def registrar(session, *, specs, guard_opt_in_enabled, guard_policy):
        calls.append(len(specs))
        return [_Registered(spec) for spec in specs]

    bad_s4 = list(_s4_specs())
    bad_s4[-1] = bad_s4[-1].model_copy(
        update={"strategy_version": "forged-after-mapping"}
    )
    with pytest.raises(bridge.Exact12BatchValidationError):
        await bridge.register_r3_campaign(
            object(),
            approved=_approved(bridge.REGISTER_R3_CAMPAIGN_OPERATION_KIND),
            full_campaign_hash=_FULL_HASH,
            campaign_run_id=_RUN_ID,
            s3_specs=_s3_specs(),
            s4_specs=tuple(bad_s4),
            row_id_to_experiment_id=_mapping(),
            guard_opt_in_enabled=True,
            guard_policy=_POLICY,
            register_experiments_fn=registrar,
        )
    assert calls == []


@pytest.mark.asyncio
async def test_exact_3_plus_9_registration_calls_reused_primitive_only_after_preflight() -> (
    None
):
    calls: list[int] = []

    async def registrar(session, *, specs, guard_opt_in_enabled, guard_policy):
        calls.append(len(specs))
        return [_Registered(spec) for spec in specs]

    registered = await bridge.register_r3_campaign(
        object(),
        approved=_approved(bridge.REGISTER_R3_CAMPAIGN_OPERATION_KIND),
        full_campaign_hash=_FULL_HASH,
        campaign_run_id=_RUN_ID,
        s3_specs=_s3_specs(),
        s4_specs=_s4_specs(),
        row_id_to_experiment_id=_mapping(),
        guard_opt_in_enabled=True,
        guard_policy=_POLICY,
        register_experiments_fn=registrar,
    )
    assert calls == [3, 9]
    assert tuple(len(group) for group in registered) == (3, 9)


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_surface", ("attempt", "pk"))
async def test_complete_attempt_and_pk_preflight_precedes_first_lookup_or_write(
    bad_surface: str,
) -> None:
    lookups: list[str] = []
    writes: list[str] = []

    async def find_existing(session, *, experiment_pk, idempotency_key):
        lookups.append(idempotency_key)
        return None

    async def record(session, *, experiment_id, request):
        writes.append(experiment_id)
        return type("Recorded", (), {"raw_payload": request.raw_payload})()

    attempts = list(_attempts())
    pks = _pks()
    if bad_surface == "attempt":
        attempts[-1] = dataclasses.replace(
            attempts[-1], experiment_id=_hex("foreign-last-attempt")
        )
    else:
        items = list(pks.items())
        pks = dict((items[1], items[0], *items[2:]))

    with pytest.raises(bridge.Exact12BatchValidationError):
        await bridge.record_r3_attempts(
            object(),
            approved=_approved(bridge.RECORD_R3_ATTEMPTS_OPERATION_KIND),
            full_campaign_hash=_FULL_HASH,
            campaign_run_id=_RUN_ID,
            row_id_to_experiment_id=_mapping(),
            row_id_to_experiment_pk=pks,
            attempts=tuple(attempts),
            strategy_name="rob974-r3",
            timeframe="1m_to_4h_pit",
            runner="rob974-r3-all-cell",
            guard_opt_in_enabled=True,
            guard_policy=_POLICY,
            find_existing_trial_fn=find_existing,
            record_trial_fn=record,
        )
    assert lookups == []
    assert writes == []


@pytest.mark.asyncio
async def test_exact_12_attempts_reuse_append_only_primitive_in_canonical_order() -> (
    None
):
    lookups: list[str] = []
    writes: list[str] = []

    async def find_existing(session, *, experiment_pk, idempotency_key):
        lookups.append(idempotency_key)
        return None

    async def record(session, *, experiment_id, request):
        writes.append(experiment_id)
        return type("Recorded", (), {"raw_payload": request.raw_payload})()

    rows = await bridge.record_r3_attempts(
        object(),
        approved=_approved(bridge.RECORD_R3_ATTEMPTS_OPERATION_KIND),
        full_campaign_hash=_FULL_HASH,
        campaign_run_id=_RUN_ID,
        row_id_to_experiment_id=_mapping(),
        row_id_to_experiment_pk=_pks(),
        attempts=_attempts(),
        strategy_name="rob974-r3",
        timeframe="1m_to_4h_pit",
        runner="rob974-r3-all-cell",
        guard_opt_in_enabled=True,
        guard_policy=_POLICY,
        find_existing_trial_fn=find_existing,
        record_trial_fn=record,
    )
    assert len(rows) == 12
    assert tuple(writes) == tuple(_mapping().values())
    assert len(lookups) == 12


@pytest.mark.asyncio
async def test_divergent_replay_and_bad_returned_fingerprint_are_refused() -> None:
    attempts = _attempts()
    first_fingerprint = attempts[0].fingerprint()

    async def divergent_existing(session, *, experiment_pk, idempotency_key):
        return type(
            "Existing",
            (),
            {"raw_payload": {"r3_h6a_evidence_fingerprint": _hex("divergent")}},
        )()

    async def must_not_record(session, *, experiment_id, request):
        raise AssertionError("divergent replay must not append")

    with pytest.raises(bridge.Exact12TerminalEvidenceMismatch):
        await bridge.record_r3_attempts(
            object(),
            approved=_approved(bridge.RECORD_R3_ATTEMPTS_OPERATION_KIND),
            full_campaign_hash=_FULL_HASH,
            campaign_run_id=_RUN_ID,
            row_id_to_experiment_id=_mapping(),
            row_id_to_experiment_pk=_pks(),
            attempts=attempts,
            strategy_name="rob974-r3",
            timeframe="1m_to_4h_pit",
            runner="rob974-r3-all-cell",
            guard_opt_in_enabled=True,
            guard_policy=_POLICY,
            find_existing_trial_fn=divergent_existing,
            record_trial_fn=must_not_record,
        )

    lookup_count = 0

    async def absent(session, *, experiment_pk, idempotency_key):
        nonlocal lookup_count
        lookup_count += 1
        return None

    async def bad_return(session, *, experiment_id, request):
        assert request.raw_payload["r3_h6a_evidence_fingerprint"] == first_fingerprint
        return type(
            "Returned",
            (),
            {"raw_payload": {"r3_h6a_evidence_fingerprint": _hex("wrong-return")}},
        )()

    with pytest.raises(bridge.Exact12TerminalEvidenceMismatch):
        await bridge.record_r3_attempts(
            object(),
            approved=_approved(bridge.RECORD_R3_ATTEMPTS_OPERATION_KIND),
            full_campaign_hash=_FULL_HASH,
            campaign_run_id=_RUN_ID,
            row_id_to_experiment_id=_mapping(),
            row_id_to_experiment_pk=_pks(),
            attempts=attempts,
            strategy_name="rob974-r3",
            timeframe="1m_to_4h_pit",
            runner="rob974-r3-all-cell",
            guard_opt_in_enabled=True,
            guard_policy=_POLICY,
            find_existing_trial_fn=absent,
            record_trial_fn=bad_return,
        )
    assert lookup_count == 1
