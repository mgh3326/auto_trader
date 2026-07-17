"""ROB-946 (H6) — campaign trial-attempt evidence contracts.

New schemas alongside (not modifying) ``app.schemas.research_backtest``. An
``AttemptEvidence`` is the COMPLETE terminal evidence for one logical attempt
— a config's full walk-forward invocation. ``completed`` records evidence
generation success, NOT a strategy PASS verdict (ROB-946 §5): pass/fail
judgement belongs to a separate downstream consumer (H5), never this schema.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.schemas.research_backtest import TrialStatus

ScenarioName = Literal["base", "primary_stress", "upward_stress"]
_EXPECTED_SCENARIO_NAMES = frozenset({"base", "primary_stress", "upward_stress"})

__all__ = [
    "AttemptEvidence",
    "AttemptKey",
    "CampaignCompletenessReport",
    "ScenarioEvidence",
    "ScenarioName",
]


class ScenarioEvidence(BaseModel):
    """One independent 13/17/22bp cost-scenario simulation ledger's evidence.

    ROB-942 R1: each cost scenario is its OWN independent ``run_symbol_stream``
    invocation, never a net-only revaluation of a shared path — this schema
    stores each scenario's own trade_count/artifact_hash so that divergence
    (e.g. a higher-cost scenario halting sooner) is preserved, never collapsed
    into a single reference count.
    """

    scenario_name: ScenarioName
    trade_count: int = Field(ge=0)
    artifact_hash: str | None = None

    model_config = ConfigDict(extra="forbid")


class AttemptKey(BaseModel):
    """Deterministic invocation identity for one logical attempt.

    A logical attempt is one config's full walk-forward invocation. An
    explicit retry uses the SAME ``campaign_run_id``/``experiment_id`` but a
    higher ``retry_index`` — a genuinely new invocation key, never a mutation
    of the original.
    """

    campaign_run_id: str = Field(min_length=1)
    experiment_id: str = Field(min_length=1)
    retry_index: int = Field(default=0, ge=0)

    model_config = ConfigDict(extra="forbid", frozen=True)

    def idempotency_key(self) -> str:
        return f"{self.campaign_run_id}:{self.experiment_id}:{self.retry_index}"


class AttemptEvidence(BaseModel):
    """Complete terminal evidence for one logical attempt (ROB-946 §5/§6).

    ``status`` is one of the 4 ROB-846 terminal outcomes. A non-``completed``
    status requires a stable ``reason_code`` (e.g. ``insufficient_symbol_evidence``,
    ``rejected:insufficient_train_evidence``, ``rejected:data_gap_in_position``,
    or a crash/timeout description) so the reason is never silently dropped.
    """

    attempt_key: AttemptKey
    status: TrialStatus
    reason_code: str | None = None
    fold_evidence_hash: str | None = None
    run_identity: str = Field(min_length=1)
    scenario_evidence: tuple[ScenarioEvidence, ScenarioEvidence, ScenarioEvidence]

    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="after")
    def _validate(self) -> AttemptEvidence:
        names = {s.scenario_name for s in self.scenario_evidence}
        if names != _EXPECTED_SCENARIO_NAMES:
            raise ValueError(
                "scenario_evidence must cover exactly "
                f"{sorted(_EXPECTED_SCENARIO_NAMES)}, got {sorted(names)}"
            )
        if self.status != "completed" and self.reason_code is None:
            raise ValueError("reason_code is required for non-completed statuses")
        return self


class CampaignCompletenessReport(BaseModel):
    """ROB-946 §9 completeness DTO — no winner-only filter.

    R1 Important-3/4 remediation: completeness is now derived from a
    bidirectional diff between the 24 EXPECTED identities (re-derived from
    caller-supplied ``StrategyExperimentIdentity`` specs via the SAME
    canonical-hash authority used at registration, never trusted as a bare
    caller-supplied id string) and the ACTUAL registered rows found for those
    strategies:

    * ``missing_experiment_ids`` — an expected identity with no registration
      at all, OR one that is registered but has no ``retry_index=0`` (primary)
      terminal attempt recorded — a campaign with only a ``retry_index=1``
      attempt and no primary is "missing", not "complete" (R1 Important-3).
    * ``extra_experiment_ids`` — a registered row (within the expected
      strategy_key scope) that does not correspond to ANY expected identity.
    * ``mismatch_experiment_ids`` — an expected identity's ``params`` slot
      (matched by ``params_hash``) IS registered, but under a DIFFERENT overall
      ``experiment_id`` — i.e. some OTHER component drifted from what was
      expected (R1 Important-4).
    * ``duplicate_or_gap_experiment_ids`` — the recorded retry-index sequence
      for an otherwise-correctly-registered experiment is non-contiguous from
      0 (a gap) or contains a raw-row duplicate. A duplicate retry INDEX for
      one experiment cannot occur while ROB-846's own
      ``uq_research_backtest_runs_experiment_idempotency`` constraint holds
      (the idempotency key embeds the retry index) — this branch is kept as
      defense-in-depth against that invariant ever being weakened, while the
      gap check is independently reachable today.

    ``verdict`` is ``"complete"`` iff ALL four of the above lists are empty —
    every expected identity has a primary attempt, nothing extra or drifted
    was found, and no gap/duplicate was observed. A campaign where every
    attempt is ``rejected``/``crashed``/``timeout`` is still "complete"
    evidence — ``completed`` is not a pass filter here.
    """

    campaign_run_id: str
    expected_total: int
    actual_registrations: int
    primary_attempts: int
    total_attempts: int
    retry_attempts: int
    status_counts: dict[str, int]
    missing_experiment_ids: list[str]
    extra_experiment_ids: list[str]
    mismatch_experiment_ids: list[str]
    duplicate_or_gap_experiment_ids: list[str]
    verdict: Literal["complete", "incomplete"]

    model_config = ConfigDict(extra="forbid")
