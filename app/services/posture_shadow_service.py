"""Manual, record-only posture-v1 shadow orchestration (ROB-1106).

The default-off branch returns before loading holdings/quotes and before opening
an artifact. The enabled branch can only call the pure generator and an
injected/local JSONL recorder; it has no order/proposal/broker integration.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict

from app.schemas.trading_policy import PosturePolicy
from app.services.posture_generator import (
    PostureGenerationResult,
    PostureGeneratorInput,
    generate_posture,
)

SnapshotLoader = Callable[[], PostureGeneratorInput]
ShadowRecorder = Callable[[PostureGenerationResult], None]


class PostureShadowRunOutcome(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["disabled", "recorded"]
    reason: str
    result: PostureGenerationResult | None = None


def append_shadow_jsonl(path: Path, result: PostureGenerationResult) -> None:
    """Append one immutable coverage record to an operator-selected local path."""

    path.parent.mkdir(parents=True, exist_ok=True)
    payload = result.model_dump(mode="json")
    with path.open("a", encoding="utf-8") as artifact:
        artifact.write(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        artifact.write("\n")


def run_posture_shadow(
    *,
    posture_policy: PosturePolicy,
    policy_version: str,
    policy_content_hash: str,
    snapshot_loader: SnapshotLoader,
    recorder: ShadowRecorder,
) -> PostureShadowRunOutcome:
    """Run one manual shadow rep, or perform a side-effect-free disabled no-op."""

    if not posture_policy.enabled:
        return PostureShadowRunOutcome(
            status="disabled",
            reason="posture.enabled=false",
        )
    if posture_policy.mode != "shadow":
        raise ValueError("ROB-1106 runner supports shadow mode only")

    result = generate_posture(
        snapshot_loader(),
        posture_policy=posture_policy,
        policy_version=policy_version,
        policy_content_hash=policy_content_hash,
    )
    recorder(result)
    return PostureShadowRunOutcome(
        status="recorded",
        reason="shadow_coverage_recorded",
        result=result,
    )
