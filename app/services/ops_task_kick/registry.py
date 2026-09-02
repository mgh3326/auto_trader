"""Static allowlist and parameter contracts for ops TaskIQ kicks.

This module is deliberately the sole mapping from an externally supplied task
name to a registered TaskIQ task. Do not derive this registry from broker
registration: doing so would turn every newly registered task into an HTTP
operation, including broker mutation tasks.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

# Importing the task package registers the existing, explicitly selected tasks
# with the same broker the API uses. It does not start a scheduler or add a
# schedule; worker execution remains an operator-owned process.
from app import tasks as _registered_tasks  # noqa: F401
from app.core.taskiq_broker import broker

FORBIDDEN_TASK_NAME_FRAGMENTS = (
    "place_order",
    "cancel",
    "modify",
    "watch_",
    "order_proposal",
    "telegram",
    "record_",
)


class KickParameters(BaseModel):
    """Closed input base for every externally kickable task."""

    model_config = ConfigDict(extra="forbid")


class SnapshotBuildParameters(KickParameters):
    market: Literal["kr", "us"]
    symbols: list[str] | None = Field(default=None, max_length=500)
    limit: int | None = Field(default=20, ge=1, le=5_000)
    all_symbols: bool = False
    batch_size: int = Field(default=200, ge=1, le=500)
    concurrency: int = Field(default=4, ge=1, le=16)
    common_stocks_only: bool = False
    commit: bool = False


class InvestorFlowParameters(KickParameters):
    market: Literal["kr"] = "kr"
    symbols: list[str] | None = Field(default=None, max_length=500)
    limit: int | None = Field(default=20, ge=1, le=5_000)
    all_symbols: bool = False
    batch_size: int = Field(default=100, ge=1, le=500)
    concurrency: int = Field(default=4, ge=1, le=16)
    days: int = Field(default=20, ge=1, le=90)
    commit: bool = False


class MarketValuationParameters(KickParameters):
    market: Literal["kr", "us"] = "kr"
    symbols: list[str] | None = Field(default=None, max_length=500)
    limit: int | None = Field(default=20, ge=1, le=5_000)
    all_symbols: bool = False
    batch_size: int = Field(default=100, ge=1, le=500)
    concurrency: int = Field(default=4, ge=1, le=16)
    commit: bool = False


class SupportProximityParameters(KickParameters):
    candidate_pool_limit: int = Field(default=30, ge=1, le=500)
    concurrency: int = Field(default=4, ge=1, le=16)
    min_market_cap: float = Field(default=300_000_000_000.0, gt=0)
    min_turnover: float = Field(default=1_000_000_000.0, gt=0)
    commit: bool = False


class ResearchReportsIngestParameters(KickParameters):
    payload_file: str | None = Field(default=None, max_length=512)
    commit: bool = False


class NoParameters(KickParameters):
    pass


@dataclass(frozen=True)
class TaskKickSpec:
    parameters_model: type[KickParameters]
    fixed_kwargs: dict[str, Any] = field(default_factory=dict)


TASK_KICK_REGISTRY: dict[str, TaskKickSpec] = {
    "build_invest_screener_snapshots": TaskKickSpec(SnapshotBuildParameters),
    "invest_screener_snapshots.kr_pre_market_repair": TaskKickSpec(NoParameters),
    "invest_screener_snapshots.kr_krx_preliminary": TaskKickSpec(NoParameters),
    "invest_screener_snapshots.kr_nxt_final": TaskKickSpec(NoParameters),
    "invest_screener_snapshots.us_post_close": TaskKickSpec(NoParameters),
    "build_investor_flow_snapshots": TaskKickSpec(InvestorFlowParameters),
    "investor_flow_snapshots.kr_scheduled": TaskKickSpec(NoParameters),
    "build_market_valuation_snapshots": TaskKickSpec(MarketValuationParameters),
    "market_valuation_snapshots.kr_scheduled": TaskKickSpec(NoParameters),
    "build_support_proximity_snapshots": TaskKickSpec(SupportProximityParameters),
    "warnings.toss.sync": TaskKickSpec(NoParameters),
    # The existing task's no-argument behaviour remains unchanged. This HTTP
    # registry never exposes an apply switch and always supplies dry_run=True.
    "kis_live.reconcile_periodic": TaskKickSpec(
        NoParameters, fixed_kwargs={"dry_run": True}
    ),
    "research_reports.ingest_bulk_smoke": TaskKickSpec(ResearchReportsIngestParameters),
}


def validate_registry(registry: dict[str, TaskKickSpec]) -> None:
    """Reject unsafe task names even if a future edit bypasses code review."""

    for task_name in registry:
        normalized = task_name.lower()
        if any(fragment in normalized for fragment in FORBIDDEN_TASK_NAME_FRAGMENTS):
            raise ValueError(f"unsafe task name in ops task-kick registry: {task_name}")


def assert_registry_tasks_registered() -> None:
    """Fail closed if a static entry drifts from TaskIQ registration."""

    missing = sorted(set(TASK_KICK_REGISTRY) - set(broker.get_all_tasks()))
    if missing:
        raise RuntimeError(
            f"ops task-kick registry has unregistered task(s): {missing}"
        )


validate_registry(TASK_KICK_REGISTRY)
