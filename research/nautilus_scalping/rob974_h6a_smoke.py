"""ROB-981 (ROB-974 R2 H6-A) CP7 -- pure fixture end-to-end smoke plan.

Wires CP1 (identity) -> CP2 (payload/envelope) -> CP3 (attempt/evidence) ->
CP4 (accounting) -> CP6 (diagnostics) into ONE deterministic, fully
fixture-marked 48-row campaign plan -- exercised by
``tests/test_rob974_h6a_smoke.py`` (pure) and
``tests/services/research/test_rob974_h6a_e2e_smoke.py`` (adds CP5's
app-side registration/attempt-batch spies). This module never imports
``app.*`` and performs no DB/network/corpus/process/current-time/random
access -- see ``test_rob974_h6a_import_guard.py``.

The fixture deliberately covers three distinct row shapes so the smoke
exercises real branch diversity, not just the all-completed happy path:

  * most rows: a normal single-fold win, ``completed``, 3 real
    ``path_scenario_evidence`` rows;
  * ``S3-01``: TRAIN-eligible but wins NO fold -- ``completed`` with
    ``never_selected`` in all three scenarios;
  * ``S4-00``: has an explicit, valid, append-only retry -- two
    ``AttemptRecord``s at ``retry_index=0`` (``crashed``) and
    ``retry_index=1`` (``completed``).
"""

from __future__ import annotations

import hashlib

import rob974_h6a_accounting as h6a_accounting
import rob974_h6a_diagnostics as h6a_diagnostics
import rob974_h6a_evidence as h6a_evidence
import rob974_h6a_identity as h6a_identity
import rob974_h6a_payload as h6a_payload

__all__ = [
    "NEVER_SELECTED_ROW_ID",
    "RETRY_ROW_ID",
    "SmokePlan",
    "build_smoke_plan",
]

NEVER_SELECTED_ROW_ID = "S3-01"
RETRY_ROW_ID = "S4-00"


def _hex64(label: str) -> str:
    return hashlib.sha256(label.encode()).hexdigest()


def _row(row_id: str, *, hypothesis: str, **params) -> h6a_identity.CampaignConfigRow:
    return h6a_identity.CampaignConfigRow(
        row_id=row_id,
        params=params,
        hypothesis=hypothesis,
        authority_label="baseline",
        provenance="fixture_identity",
    )


_S3_HYPOTHESIS = "smoke fixture S3 hypothesis\n"
_S4_HYPOTHESIS = "smoke fixture S4 hypothesis\n"


def _rows() -> list[h6a_identity.CampaignConfigRow]:
    return [
        _row(f"S3-{i:02d}", hypothesis=_S3_HYPOTHESIS, L=12, q_min=0.35, idx=i)
        for i in range(24)
    ] + [
        _row(f"S4-{i:02d}", hypothesis=_S4_HYPOTHESIS, W=180, z_entry=1.8, idx=i)
        for i in range(24)
    ]


def _contracts() -> dict[str, h6a_identity.StrategyContractProvenance]:
    return {
        slug: h6a_identity.StrategyContractProvenance(
            strategy_slug=slug,
            strategy_key=f"ROB974-{slug}-SMOKE",
            strategy_version=f"{slug.lower()}-smoke-v1",
            contract_hash=_hex64(f"{slug}-smoke-contract"),
            contract_key=f"{slug}-smoke-key",
            provenance="fixture_identity",
        )
        for slug in ("S3", "S4")
    }


def _row_specs() -> tuple[h6a_identity.H6ARowSpec, ...]:
    shared = {
        "dataset_manifest": {"h1_lineage_hash": _hex64("smoke-h1-lineage")},
        "universe": {"symbols": ["XRPUSDT", "DOGEUSDT", "SOLUSDT"]},
        "benchmark": {"kind": "none_explicit_sentinel"},
        "mdd": {"h2_engine_contract_hash": _hex64("smoke-h2-engine")},
    }
    by_slug = lambda key: {  # noqa: E731
        "S3": {key: f"S3-smoke-{key}"},
        "S4": {key: f"S4-smoke-{key}"},
    }
    return h6a_identity.build_campaign_row_specs(
        _rows(),
        contracts=_contracts(),
        shared_components=shared,
        pit_component_by_slug=by_slug("pit"),
        frozen_config_component_by_slug=by_slug("frozen_config"),
        policy_component_by_slug=by_slug("policy"),
        cost_component_by_slug=by_slug("cost"),
    )


def _campaign_policy() -> h6a_payload.CampaignPolicy:
    return h6a_payload.CampaignPolicy(
        folds=tuple({"fold_id": f"fold-{i:02d}"} for i in range(8)),
        embargo_hours=3,
        horizons={"s3_max_hold_bars": 12, "s4_max_hold_bars": 9},
        selection_authority="smoke_selection_authority",
        path_membership={
            "base13": {"cost_bps": 13},
            "primary_stress17": {"cost_bps": 17},
            "upward_stress22": {"cost_bps": 22},
        },
        funding_policy={"gate": "post_arbitration_pre_entry"},
        gates_bins={"vol_percentile": [20, 90]},
        pbo_contract={"primary_stress_bps": 17, "window": "24x365", "slices": 4},
        pair_order=("XRP-DOGE", "XRP-SOL", "DOGE-SOL"),
        s4_tri_state_policy="historical_only_pair_exec_not_evaluated",
    )


def _envelope(
    row_specs: tuple[h6a_identity.H6ARowSpec, ...],
) -> h6a_payload.H6ACampaignEnvelope:
    return h6a_payload.build_campaign_envelope(
        row_specs=row_specs,
        parent_corpus={
            "content_hash": _hex64("smoke-parent-corpus"),
            "universe": ["XRPUSDT", "DOGEUSDT", "SOLUSDT"],
        },
        campaign_policy=_campaign_policy(),
        source_pins=h6a_payload.EMPTY_SOURCE_PINS,
        mode="fixture_plan",
    )


def _fold_traces(
    *, selected_index: int | None
) -> tuple[h6a_evidence.FoldSelectionTrace, ...]:
    rows = []
    for i in range(h6a_evidence.FOLD_COUNT):
        selected = i == selected_index
        rows.append(
            h6a_evidence.FoldSelectionTrace(
                fold_id=f"fold-{i:02d}",
                fold_index=i,
                selected=selected,
                eligible_symbols_or_pairs=("XRPUSDT",),
                excluded_symbols_or_pairs=(),
                accepted_input_hash=_hex64(f"smoke-accepted-{i}") if selected else None,
                rejection_reason=None if selected else "lost_arbitration",
                no_trade_reason_counts={},
            )
        )
    return tuple(rows)


def _unique_evidence() -> tuple[h6a_evidence.UniqueGeneratorEvidence, ...]:
    rows = []
    for i in range(h6a_evidence.FOLD_COUNT):
        kwargs = {
            "fold_id": f"fold-{i:02d}",
            "candidate_identity_hash": _hex64(f"smoke-candidate-{i}"),
            "evaluated_decision_units": 10,
            "no_signal": 4,
            "candidate": 6,
            "generator_rejected": 4,
            "generator_accepted": 2,
            "generator_rejection_subtotal_by_reason": {
                "below_er_min": 3,
                "vol_gate": 1,
            },
        }
        content_hash = h6a_evidence._recompute_unique_evidence_hash(_Holder(**kwargs))
        rows.append(
            h6a_evidence.UniqueGeneratorEvidence(**kwargs, content_hash=content_hash)
        )
    return tuple(rows)


class _Holder:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


def _path_scenario_row(
    name: str, *, status: str, trade_count: int = 0, member_trade_keys: tuple = ()
) -> h6a_evidence.PathScenarioEvidence:
    reason_code = None
    if status not in ("completed", "never_selected"):
        reason_code = next(iter(h6a_evidence.ALLOWED_REASONS_BY_STATUS[status]))
    kwargs = {
        "path_scenario": name,
        "status": status,
        "reason_code": reason_code,
        "trade_count": trade_count,
        "member_trade_keys": member_trade_keys,
        "no_trade_reason_counts": {},
    }
    artifact_hash = h6a_evidence._recompute_path_scenario_hash(_Holder(**kwargs))
    return h6a_evidence.PathScenarioEvidence(**kwargs, artifact_hash=artifact_hash)


def _win_scenarios(row_id: str) -> tuple[h6a_evidence.PathScenarioEvidence, ...]:
    rows = []
    for name in h6a_evidence.PATH_SCENARIOS:
        key = _hex64(f"smoke-trade-{row_id}-{name}")
        rows.append(
            _path_scenario_row(
                name, status="completed", trade_count=1, member_trade_keys=(key,)
            )
        )
    return tuple(rows)


def _never_selected_scenarios() -> tuple[h6a_evidence.PathScenarioEvidence, ...]:
    return tuple(
        _path_scenario_row(name, status="never_selected")
        for name in h6a_evidence.PATH_SCENARIOS
    )


def _build_attempts(
    row_specs: tuple[h6a_identity.H6ARowSpec, ...],
    envelope: h6a_payload.H6ACampaignEnvelope,
    campaign_run_id: str,
) -> tuple[h6a_evidence.AttemptRecord, ...]:
    full_campaign_hash = envelope.full_campaign_hash()
    attempts: list[h6a_evidence.AttemptRecord] = []
    for spec in row_specs:
        row_id = spec.row_id
        strategy_slug = row_id.split("-", 1)[0]
        historical_state = (
            h6a_evidence.HistoricalExecutorState() if strategy_slug == "S4" else None
        )
        if row_id == NEVER_SELECTED_ROW_ID:
            attempts.append(
                h6a_evidence.build_attempt_record(
                    row_id=row_id,
                    experiment_id=spec.experiment_id,
                    campaign_run_id=campaign_run_id,
                    full_campaign_hash=full_campaign_hash,
                    strategy_key=spec.strategy_key,
                    retry_index=0,
                    status="completed",
                    reason_code=None,
                    fold_traces=_fold_traces(selected_index=None),
                    unique_evidence=_unique_evidence(),
                    path_scenario_evidence=_never_selected_scenarios(),
                    historical_executor_state=None,
                )
            )
        elif row_id == RETRY_ROW_ID:
            attempts.append(
                h6a_evidence.build_attempt_record(
                    row_id=row_id,
                    experiment_id=spec.experiment_id,
                    campaign_run_id=campaign_run_id,
                    full_campaign_hash=full_campaign_hash,
                    strategy_key=spec.strategy_key,
                    retry_index=0,
                    status="crashed",
                    reason_code=h6a_evidence.REASON_CHILD_EXECUTION_CRASHED,
                    fold_traces=_fold_traces(selected_index=None),
                    unique_evidence=_unique_evidence(),
                    path_scenario_evidence=_never_selected_scenarios(),
                    historical_executor_state=None,
                )
            )
            attempts.append(
                h6a_evidence.build_attempt_record(
                    row_id=row_id,
                    experiment_id=spec.experiment_id,
                    campaign_run_id=campaign_run_id,
                    full_campaign_hash=full_campaign_hash,
                    strategy_key=spec.strategy_key,
                    retry_index=1,
                    status="completed",
                    reason_code=None,
                    fold_traces=_fold_traces(selected_index=0),
                    unique_evidence=_unique_evidence(),
                    path_scenario_evidence=_win_scenarios(row_id),
                    historical_executor_state=historical_state,
                )
            )
        else:
            attempts.append(
                h6a_evidence.build_attempt_record(
                    row_id=row_id,
                    experiment_id=spec.experiment_id,
                    campaign_run_id=campaign_run_id,
                    full_campaign_hash=full_campaign_hash,
                    strategy_key=spec.strategy_key,
                    retry_index=0,
                    status="completed",
                    reason_code=None,
                    fold_traces=_fold_traces(selected_index=0),
                    unique_evidence=_unique_evidence(),
                    path_scenario_evidence=_win_scenarios(row_id),
                    historical_executor_state=historical_state,
                )
            )
    return tuple(attempts)


def _accounting(
    row_specs: tuple[h6a_identity.H6ARowSpec, ...],
    attempts: tuple[h6a_evidence.AttemptRecord, ...],
    campaign_run_id: str,
) -> h6a_accounting.CombinedAccountingReport:
    canonical_row_ids = tuple(spec.row_id for spec in row_specs)
    row_id_to_experiment_id = {spec.row_id: spec.experiment_id for spec in row_specs}
    accounting_rows = tuple(
        h6a_accounting.AttemptAccountingRow(
            row_id=a.row_id,
            experiment_id=a.experiment_id,
            retry_index=a.retry_index,
            status=a.status,
            reason_code=a.reason_code,
            fold_evidence_hash=a.fold_evidence_hash,
            run_identity=a.run_identity,
        )
        for a in attempts
    )
    return h6a_accounting.build_combined_accounting(
        campaign_run_id=campaign_run_id,
        canonical_row_ids=canonical_row_ids,
        row_id_to_experiment_id=row_id_to_experiment_id,
        registered_total=len(canonical_row_ids),
        attempts=accounting_rows,
    )


def _diagnostic_variants() -> dict[str, h6a_diagnostics.DiagnosticCarrier]:
    """Absent/present/reworded/overflow diagnostic carriers, all built via
    the SAME reused ROB-970 capture path."""

    def _capture(message: str) -> h6a_diagnostics.ChildFailureEvidence:
        try:
            raise ValueError(message)
        except ValueError as exc:
            return h6a_diagnostics.capture_child_failure_evidence(
                exc,
                transport="in_process",
                stage="engine",
                strategy="S4",
                config_id=RETRY_ROW_ID,
            )

    empty_overflow = h6a_diagnostics.DiagnosticOverflowMetadata(
        truncated=False, omitted_distinct_signatures=0, omitted_occurrences=0
    )
    absent = h6a_diagnostics.DiagnosticCarrier(evidence=(), overflow=empty_overflow)
    present = h6a_diagnostics.DiagnosticCarrier(
        evidence=(_capture("smoke root cause v1"),), overflow=empty_overflow
    )
    reworded = h6a_diagnostics.DiagnosticCarrier(
        evidence=(_capture("smoke root cause v2 (reworded)"),), overflow=empty_overflow
    )
    events = [_capture(f"smoke overflow candidate {i}") for i in range(40)]
    overflow_evidence, overflow_meta = h6a_diagnostics.accumulate_diagnostic_evidence(
        events
    )
    overflow = h6a_diagnostics.DiagnosticCarrier(
        evidence=overflow_evidence, overflow=overflow_meta
    )
    return {
        "absent": absent,
        "present": present,
        "reworded": reworded,
        "overflow": overflow,
    }


class SmokePlan:
    """The fully assembled, deterministic, fixture-marked plan."""

    def __init__(self) -> None:
        self.row_specs = _row_specs()
        self.envelope = _envelope(self.row_specs)
        self.full_campaign_hash = self.envelope.full_campaign_hash()
        self.campaign_run_id = h6a_payload.derive_primary_run_id(
            self.full_campaign_hash
        )
        self.attempts = _build_attempts(
            self.row_specs, self.envelope, self.campaign_run_id
        )
        self.accounting = _accounting(
            self.row_specs, self.attempts, self.campaign_run_id
        )
        self.diagnostics = _diagnostic_variants()
        self.row_id_to_experiment_id = {
            spec.row_id: spec.experiment_id for spec in self.row_specs
        }


def build_smoke_plan() -> SmokePlan:
    return SmokePlan()
