# app/services/action_report/snapshot_backed/intraday_floor.py
"""ROB-335 — intraday non-empty floor guard.

Last-resort guarantee that an ``intraday_action`` report never succeeds with
``items=[]`` (spec §3.1). When the deterministic emitter + classifier produced
nothing actionable (e.g. no holdings, no candidates, portfolio unavailable),
synthesize ONE structural review item that carries the report's no-action
reason as an explicit ActionPacket entry. Never fabricates a buy/sell call.
"""

from __future__ import annotations

from typing import Any

from app.schemas.investment_reports import IngestReportItem
from app.services.action_report.snapshot_backed.action_verdict import VERDICT_TO_BUCKET

_INTRADAY_POLICY_PREFIX = "intraday_action"


def is_intraday_action(policy_version: str | None) -> bool:
    return bool(policy_version) and policy_version.startswith(_INTRADAY_POLICY_PREFIX)


def ensure_action_floor(
    items: list[IngestReportItem],
    *,
    why_no_action: dict[str, Any] | None,
) -> list[IngestReportItem]:
    """Return ``items`` unchanged when non-empty; else a one-item floor."""
    if items:
        return items

    kind = (why_no_action or {}).get("kind")
    # real_no_action -> genuine hold (keep); data/stale-blocked -> data_gap.
    verdict = "keep" if kind == "real_no_action" else "data_gap"
    reason = (why_no_action or {}).get("reason_ko") or (
        "장중 액션 없음 — 데이터/후보 부족"
    )
    return [
        IngestReportItem(
            client_item_key="intraday-floor",
            item_kind="risk",
            symbol=None,
            intent="risk_review",
            rationale=reason,
            operation="review",
            apply_policy="requires_user_approval",
            evidence_snapshot={
                "action_verdict": verdict,
                "proposer": "intraday_floor",
                "why_no_action": why_no_action,
            },
            decision_bucket=VERDICT_TO_BUCKET[verdict],
        )
    ]
