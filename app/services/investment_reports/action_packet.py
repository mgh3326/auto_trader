# app/services/investment_reports/action_packet.py
"""ROB-335 — deterministic intraday ActionPacket projection.

Read-time *view-layer* projection (same pattern as ROB-322
``review_sections.py``): groups the flat report-item list by the
``evidence_snapshot["action_verdict"]`` sub-label into the four-question
intraday surface, and folds ROB-318 diagnostics into the no-action /
data-gap answers.

Pure + read-only: no new persisted classification, DB CHECK, or migration.
Items without an ``action_verdict`` (legacy / Hermes-not-yet) are not
projected; they remain available via the bundle's ``items``.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from app.schemas.investment_reports import (
    ActionPacket,
    ActionPacketEntry,
    DataGapEntry,
    InvestmentReportItemResponse,
    NoActionSummary,
)
from app.services.action_report.snapshot_backed.action_verdict import (
    VERDICT_TO_BUCKET,
)

_HELD_VERDICTS = {"sell_review", "trim_review", "add_review", "keep", "no_add"}
_NEW_BUY_VERDICTS = {"buy_review", "limit_wait"}
_RISK_VERDICTS = {"watch_only"}
_DATA_GAP_VERDICTS = {"data_gap"}
_DEGRADED_STATUSES = {"unavailable", "failed", "hard_stale", "soft_stale", "partial"}


def _verdict(item: InvestmentReportItemResponse) -> str | None:
    evidence = item.evidence_snapshot or {}
    verdict = evidence.get("action_verdict") if isinstance(evidence, Mapping) else None
    if isinstance(verdict, str) and verdict in VERDICT_TO_BUCKET:
        return verdict
    return None


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _entry_rank(item: InvestmentReportItemResponse) -> int | None:
    evidence = item.evidence_snapshot or {}
    if isinstance(evidence, Mapping):
        rank = _int_or_none(evidence.get("candidate_rank") or evidence.get("rank"))
        if rank is not None:
            return rank
    return item.priority if item.priority > 0 else None


def _entry(item: InvestmentReportItemResponse, verdict: str) -> ActionPacketEntry:
    rank = _entry_rank(item)
    evidence = item.evidence_snapshot or {}
    reason = (
        evidence.get("reject_or_wait_reason") if isinstance(evidence, Mapping) else None
    )
    return ActionPacketEntry(
        verdict=verdict,  # type: ignore[arg-type]
        symbol=item.symbol,
        side=item.side,
        rationale=item.rationale,
        item_uuid=item.item_uuid,
        priority=item.priority if item.priority > 0 else None,
        rank=rank,
        reject_or_wait_reason=str(reason) if isinstance(reason, str) else None,
        evidence_snapshot=dict(item.evidence_snapshot or {}),
    )


def build_action_packet(
    items: Sequence[InvestmentReportItemResponse],
    diagnostics: Mapping[str, Any] | None,
) -> ActionPacket:
    held: list[ActionPacketEntry] = []
    new_buy: list[ActionPacketEntry] = []
    risk: list[ActionPacketEntry] = []
    data_gaps: list[DataGapEntry] = []
    no_new_buy_reason: str | None = None

    for item in items:
        verdict = _verdict(item)
        if verdict is None:
            continue
        if verdict == "no_new_buy_candidates":
            no_new_buy_reason = item.rationale
            continue
        if verdict in _HELD_VERDICTS:
            held.append(_entry(item, verdict))
        elif verdict in _NEW_BUY_VERDICTS:
            new_buy.append(_entry(item, verdict))
        elif verdict in _RISK_VERDICTS:
            risk.append(_entry(item, verdict))
        elif verdict in _DATA_GAP_VERDICTS:
            data_gaps.append(
                DataGapEntry(source=item.symbol or "unknown", reason=item.rationale)
            )

    no_action_reason = _no_action_summary(diagnostics)
    new_buy.sort(key=lambda entry: entry.rank or entry.priority or 1_000_000)
    risk.sort(key=lambda entry: entry.rank or entry.priority or 1_000_000)
    data_gaps.extend(_diagnostics_gaps(diagnostics))

    return ActionPacket(
        held_actions=held,
        new_buy_candidates=new_buy,
        no_new_buy_reason=no_new_buy_reason,
        risk_reviews=risk,
        no_action_reason=no_action_reason,
        data_gaps_for_next_cycle=data_gaps,
    )


def _no_action_summary(
    diagnostics: Mapping[str, Any] | None,
) -> NoActionSummary | None:
    if not isinstance(diagnostics, Mapping):
        return None
    why = diagnostics.get("why_no_action")
    if not isinstance(why, Mapping):
        return None
    blocking = why.get("blocking_sources") or []
    return NoActionSummary(
        kind=why.get("kind"),
        reason_ko=why.get("reason_ko"),
        blocking_sources=[str(s) for s in blocking],
    )


def _diagnostics_gaps(diagnostics: Mapping[str, Any] | None) -> list[DataGapEntry]:
    if not isinstance(diagnostics, Mapping):
        return []
    by_source = diagnostics.get("data_sufficiency_by_source")
    if not isinstance(by_source, Mapping):
        return []
    out: list[DataGapEntry] = []
    for source, info in by_source.items():
        if not isinstance(info, Mapping):
            continue
        status = info.get("status")
        if status in _DEGRADED_STATUSES:
            out.append(
                DataGapEntry(
                    source=str(source),
                    status=str(status) if status is not None else None,
                    reason=info.get("reason") or info.get("reason_code"),
                )
            )
    return out
