from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.services.research_canonical_hash import IDENTITY_COMPONENTS, encode_canonical

# ROB-846 — terminal trial outcomes; every invocation records exactly one.
TrialStatus = Literal["completed", "rejected", "crashed", "timeout"]


class StrategyExperimentIdentity(BaseModel):
    """Immutable identity of a strategy version to register (ROB-846).

    Each identity component is hashed into a canonical SHA-256 digest; the
    combination derives the experiment_id. Unknown keys are rejected so the
    contract cannot silently drift.

    Reproducibility contract: every component is **required and non-null** — an
    all-null identity cannot be registered. A component that a strategy
    genuinely does not use must be stated explicitly with an empty sentinel
    (e.g. ``benchmark={}`` or ``cost=[]``), not omitted or set to ``None``, so
    "no benchmark" is a deliberate, hashable fact rather than missing data.
    ``hypothesis`` and ``supersedes_experiment_id`` are the only optional fields.
    """

    strategy_key: str = Field(min_length=1)
    strategy_version: str = Field(min_length=1)
    hypothesis: str | None = None

    strategy: Any
    code: Any
    params: Any
    dataset_manifest: Any
    universe: Any
    pit: Any
    frozen_config: Any
    policy: Any
    benchmark: Any
    cost: Any
    mdd: Any

    supersedes_experiment_id: str | None = None

    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="after")
    def _validate_components(self) -> StrategyExperimentIdentity:
        missing = [name for name in IDENTITY_COMPONENTS if getattr(self, name) is None]
        if missing:
            raise ValueError(
                "identity components must be non-null (use an explicit empty "
                f"sentinel like {{}} for unused ones): {missing}"
            )
        # Each component must reduce to a closed, collision-free typed canonical
        # AST (no NaN/Inf, str-only dict keys, unambiguous sets, no unsupported
        # types) so hashing and JSONB persistence cannot diverge. Reject invalid
        # identities here, with the offending component named, before any DB work.
        for name in IDENTITY_COMPONENTS:
            try:
                encode_canonical(getattr(self, name))
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    f"identity component {name!r} is not canonical/JSON-safe: {exc}"
                ) from exc
        return self

    def components(self) -> dict[str, Any]:
        """Ordered identity components fed to ``compute_identity_hashes``."""
        return {name: getattr(self, name) for name in IDENTITY_COMPONENTS}


class StrategyExperimentRecord(BaseModel):
    """Read-model of a registered immutable experiment (ROB-846)."""

    experiment_id: str
    strategy_key: str
    strategy_version: str
    hypothesis: str | None = None
    strategy_hash: str
    code_hash: str
    params_hash: str
    dataset_manifest_hash: str
    universe_hash: str
    pit_hash: str
    frozen_config_hash: str
    policy_hash: str
    benchmark_hash: str
    cost_hash: str
    mdd_hash: str
    supersedes_experiment_id: str | None = None
    created_at: datetime | None = None

    model_config = ConfigDict(from_attributes=True)


class BacktestTrialRequest(BaseModel):
    """One append-only trial invocation under an experiment (ROB-846).

    ``status`` may be any terminal outcome — completed/rejected/crashed/timeout
    all record a trial and occupy a monotonic ``trial_index``.
    """

    status: TrialStatus
    strategy_name: str = Field(min_length=1)
    timeframe: str = Field(min_length=1)
    runner: str = Field(min_length=1)
    run_id: str | None = None
    seed: int | None = None
    information_cutoff: datetime | None = None
    gate_artifact_hash: str | None = None
    idempotency_key: str | None = None
    exchange: str = "binance"
    market: str = "spot"
    timerange: str | None = None
    started_at: datetime | None = None
    ended_at: datetime | None = None
    total_trades: int = Field(default=0, ge=0)
    profit_factor: Decimal = Field(default=Decimal("0"))
    max_drawdown: Decimal = Field(default=Decimal("0"))
    win_rate: Decimal | None = None
    expectancy: Decimal | None = None
    total_return: Decimal | None = None
    artifact_path: str | None = None
    artifact_hash: str | None = None
    raw_payload: dict | None = None

    model_config = ConfigDict(extra="forbid")


class TrialAccounting(BaseModel):
    """Complete trial accounting for an experiment (ROB-846 AC#4).

    ``outcome_counts`` is always zero-filled for every terminal status — there
    is no winner-only filtering.
    """

    experiment_id: str
    total_trials: int
    outcome_counts: dict[str, int]


class PromotionLinkRequest(BaseModel):
    """Link a promotion candidate to an EXACT run/config/data identity.

    The registry rejects the link unless the experiment behind the run matches
    all three expected hashes (ROB-846 AC#5).
    """

    expected_experiment_id: str = Field(min_length=1)
    expected_config_hash: str = Field(min_length=1)
    expected_data_hash: str = Field(min_length=1)
    status: str = Field(min_length=1)
    reason_code: str = Field(min_length=1)
    thresholds: dict | None = None
    metrics: dict | None = None

    model_config = ConfigDict(extra="forbid")


class BacktestPairSummary(BaseModel):
    pair: str = Field(min_length=1)
    total_trades: int = Field(ge=0)
    profit_factor: Decimal | None = None
    max_drawdown: Decimal | None = None
    total_return: Decimal | None = None


class BacktestRunSummary(BaseModel):
    run_id: str = Field(min_length=1)
    strategy_name: str = Field(min_length=1)
    strategy_version: str | None = None
    exchange: str = "binance"
    market: str = "spot"
    timeframe: str = Field(min_length=1)
    timerange: str | None = None
    runner: str = Field(min_length=1)
    started_at: datetime | None = None
    ended_at: datetime | None = None
    total_trades: int = Field(ge=0)
    profit_factor: Decimal = Field(default=Decimal("0"))
    max_drawdown: Decimal = Field(default=Decimal("0"))
    win_rate: Decimal | None = None
    expectancy: Decimal | None = None
    total_return: Decimal | None = None
    artifact_path: str | None = None
    artifact_hash: str | None = None
    pairs: list[BacktestPairSummary] = Field(default_factory=list)
    raw_payload: dict | None = None

    model_config = ConfigDict(extra="ignore")
