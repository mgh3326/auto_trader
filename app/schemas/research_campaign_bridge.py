"""ROB-946 (H6) — campaign trial-attempt evidence contracts.

New schemas alongside (not modifying) ``app.schemas.research_backtest``. An
``AttemptEvidence`` is the COMPLETE terminal evidence for one logical attempt
— a config's full walk-forward invocation. ``completed`` records evidence
generation success, NOT a strategy PASS verdict (ROB-946 §5): pass/fail
judgement belongs to a separate downstream consumer (H5), never this schema.
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.schemas.research_backtest import TrialStatus

ScenarioName = Literal["base", "primary_stress", "upward_stress"]
_EXPECTED_SCENARIO_NAMES = frozenset({"base", "primary_stress", "upward_stress"})

DiagnosticTransport = Literal["in_process"]
DiagnosticStage = Literal["generator", "funding_gate", "engine"]

# ROB-970 R1 (Q1=A, cap=32): the ONE production cap policy -- imported
# nowhere else in this schema, redeclared here as the schema's own
# authority so `app/schemas` never depends on `research/nautilus_scalping`
# (the reverse-only import boundary this bridge already keeps).
MAX_DISTINCT_SIGNATURES = 32

# R2 Critical: an independent, app-side re-implementation of the same
# residual-unsafe-content check the research capture module applies BEFORE
# building a ChildFailureEvidence (rob944_diagnostic_evidence._looks_unsafe_
# residual) -- this schema must never simply TRUST that a caller already
# sanitized message/traceback_text, since a directly (hostile or buggy)
# constructed ChildFailureDiagnostic bypasses that capture path entirely.
# Deliberately duplicated rather than imported: app/schemas must not import
# research/nautilus_scalping (the reverse already holds the other way).
_RESIDUAL_KV_SECRET_RE = re.compile(
    r"(?i)\b\w*(?:secret|token|passwd|password|api[_-]?key|access[_-]?key|dsn|"
    r"credential)\w*\b['\"]?\s*[:=]\s*(?!<redacted)['\"]?\S"
)
_RESIDUAL_DICT_LITERAL_RE = re.compile(
    r"\{(?:[^{}]|\n){0,2000}?['\"]\s*:\s*['\"][^{}]*?\}"
)
_RESIDUAL_DATACLASS_REPR_RE = re.compile(
    r"\b[A-Z][A-Za-z0-9_]*\((?:[a-zA-Z_][a-zA-Z0-9_]*=[^(){}]*?,\s*){1,}"
    r"[a-zA-Z_][a-zA-Z0-9_]*=[^(){}]*?\)"
)
_RESIDUAL_SHOUTY_SECRET_RE = re.compile(
    r"\b(?:SECRET|CREDENTIAL|PRIVATE[_-]?KEY|CLIENT[_-]?SECRET|PASSWORD|"
    r"API[_-]?KEY|ACCESS[_-]?KEY)[A-Z_-]*\b"
)
_DSN_URL_RE = re.compile(r"(?i)\b\w+://[^\s\"']*:[^\s\"'@]*@[^\s\"']+")
_BEARER_JWT_RE = re.compile(r"\beyJ[A-Za-z0-9_\-.]{10,}\b")
_ABS_PATH_RE = re.compile(r"(?<![\w./])/(?:[\w.\-]+/)+[\w.\-]+")


def _looks_unsafe(text: str) -> bool:
    return bool(
        _RESIDUAL_KV_SECRET_RE.search(text)
        or _RESIDUAL_DICT_LITERAL_RE.search(text)
        or _RESIDUAL_DATACLASS_REPR_RE.search(text)
        or _RESIDUAL_SHOUTY_SECRET_RE.search(text)
        or _DSN_URL_RE.search(text)
        or _BEARER_JWT_RE.search(text)
        or _ABS_PATH_RE.search(text)
    )


__all__ = [
    "AttemptEvidence",
    "AttemptKey",
    "CampaignCompletenessReport",
    "ChildFailureDiagnostic",
    "ChildFailureDiagnosticOverflow",
    "ScenarioEvidence",
    "ScenarioName",
]


class ChildFailureDiagnosticOverflow(BaseModel):
    """ROB-970 R1 (Q1=A, cap=32, Fable-approved
    ``orch-fable-answer-rob970-r1-20260719.md``) -- closed-shape,
    diagnostics-only overflow accounting for anything beyond the first 32
    DISTINCT signatures (in canonical/first-seen execution order).
    Deliberately carries NO hash/identity role -- excluded from every
    semantic hash/fingerprint/verdict, exactly like ``ChildFailureDiagnostic``
    itself. Exactly these three fields, nothing more."""

    truncated: bool
    omitted_distinct_signatures: int = Field(ge=0)
    omitted_occurrences: int = Field(ge=0)

    model_config = ConfigDict(extra="forbid", frozen=True)

    @model_validator(mode="after")
    def _validate(self) -> ChildFailureDiagnosticOverflow:
        if self.omitted_distinct_signatures > self.omitted_occurrences:
            raise ValueError(
                "omitted_distinct_signatures cannot exceed omitted_occurrences"
            )
        # R2 audit: truncated is a DERIVED fact, never an independent
        # caller-asserted boolean.
        if self.truncated != (self.omitted_occurrences > 0):
            raise ValueError("truncated must be exactly (omitted_occurrences > 0)")
        return self


class ChildFailureDiagnostic(BaseModel):
    """ROB-970 (Q2/Q3, Fable-approved
    ``orch-fable-answer-rob970-20260719.md``) -- one deduped, sanitized,
    bounded child-failure diagnostic record, carried from the research H4
    walk-forward layer into H6's persisted ``raw_payload``.

    Deliberately carries NO hash/identity role of its own: excluded from
    ``terminal_evidence_fingerprint`` and every other semantic seal (fixed
    ``reason_code``, ``fold_evidence_hash``, ``run_identity``) -- additive,
    persistence-only evidence.

    Q3: the only transport with a real capture path today is
    ``"in_process"`` (same-process signal-generator/funding-gate/engine
    callbacks) -- for that transport ``stderr`` must be ``None`` (no real
    child stderr stream exists; never fabricated by duplicating
    ``traceback_text`` into it).
    """

    transport: DiagnosticTransport
    stage: DiagnosticStage
    exception_type: str = Field(min_length=1)
    message: str
    traceback_text: str = Field(min_length=1)
    stderr: str | None = None
    strategy: str = Field(min_length=1)
    config_id: str = Field(min_length=1)
    symbol: str | None = None
    fold_id: str | None = None
    scenario_name: str | None = None
    signature: str = Field(min_length=1)
    occurrence_count: int = Field(ge=1)
    truncated: bool

    model_config = ConfigDict(extra="forbid", frozen=True)

    @model_validator(mode="after")
    def _validate(self) -> ChildFailureDiagnostic:
        if self.transport == "in_process" and self.stderr is not None:
            raise ValueError("in_process transport must never fabricate a stderr value")
        # R2 Critical: close the persistence-boundary bypass -- a directly
        # (hostile or buggy) constructed ChildFailureDiagnostic must still
        # fail closed if message/traceback_text carries secret/env-dump/
        # DSN/JWT/absolute-path/raw-record content, never merely trusting
        # that the caller already ran it through the research sanitizer.
        if _looks_unsafe(self.message) or _looks_unsafe(self.traceback_text):
            raise ValueError(
                "message/traceback_text still looks unsafe (secret/env-dump/DSN/"
                "JWT/path/raw-record shaped) -- refusing to construct"
            )
        return self


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
    # ROB-970 (Q2, Fable-approved): additive, persistence-only child-failure
    # evidence -- never an input to run_identity/fold_evidence_hash/any
    # semantic seal (see terminal_evidence_fingerprint, which deliberately
    # never reads this field).
    diagnostic_evidence: tuple[ChildFailureDiagnostic, ...] = ()
    # ROB-970 R1 (Q1=A, cap=32): honest overflow accounting -- equally
    # additive/persistence-only, equally excluded from every semantic seal.
    diagnostic_overflow: ChildFailureDiagnosticOverflow = Field(
        default_factory=lambda: ChildFailureDiagnosticOverflow(
            truncated=False, omitted_distinct_signatures=0, omitted_occurrences=0
        )
    )

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
        # R2 audit: the app schema itself must independently enforce the
        # single production cap -- never trust that only the research H6/CLI
        # producer already bounded it.
        if len(self.diagnostic_evidence) > MAX_DISTINCT_SIGNATURES:
            raise ValueError(
                "diagnostic_evidence must have at most "
                f"{MAX_DISTINCT_SIGNATURES} entries, got "
                f"{len(self.diagnostic_evidence)}"
            )
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
