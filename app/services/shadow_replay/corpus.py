"""ROB-697 (M1) — read-only replay-corpus selection.

ROB-501 guard: this module is SELECT-only DB access. NO writes, NO LLM,
NO network. It picks which past bundle-backed report items are eligible
to be replayed by the (later) ``claude -p`` A' driver.

The corpus source (``claude_bundle`` / ``hermes_bundle`` / manual
``operator_audit``) is decided by a one-time census against the real
production DB — see ``docs/runbooks/shadow-replay.md`` §"P0 census
(operator)". That census cannot run in this environment (no prod data),
so ``operator_audit`` is intentionally not implemented as a code path
here: if neither profile has enough coverage, ``select_replay_corpus``
raises ``CorpusUnavailable`` and defers to the operator.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.investment_reports import InvestmentReport, InvestmentReportItem
from app.services.shadow_replay.scoring import extract_decision

_HUMAN_PROFILE = "CLAUDE_ADVISOR"
_HERMES_PROFILE = "HERMES_ADVISOR"


@dataclass(frozen=True)
class CorpusItem:
    snapshot_bundle_uuid: str
    report_id: int
    item_uuid: str
    item_kind: str
    intent: str
    reference_decision: dict[str, Any]


@dataclass(frozen=True)
class CorpusSelection:
    source: str  # "claude_bundle" | "hermes_bundle" | "operator_audit"
    items: list[CorpusItem]


def _non_autoemit(item: Any) -> bool:
    ev = item.evidence_snapshot or {}
    if ev.get("source") == "auto_emit":
        return False
    proposer = str(ev.get("proposer", ""))
    return not proposer.startswith("auto_emit/") and proposer != "intraday_floor"


async def _bundle_items_for_profile(
    session: AsyncSession, profile: str, limit: int
) -> list[CorpusItem]:
    stmt = (
        select(InvestmentReportItem, InvestmentReport.snapshot_bundle_uuid)
        .join(InvestmentReport, InvestmentReport.id == InvestmentReportItem.report_id)
        .where(InvestmentReport.snapshot_bundle_uuid.isnot(None))
        .where(InvestmentReport.created_by_profile == profile)
        .order_by(InvestmentReport.id.desc())
        .limit(limit * 4)  # over-fetch; auto_emit filter is in Python
    )
    rows = (await session.execute(stmt)).all()
    out: list[CorpusItem] = []
    for item, bundle_uuid in rows:
        if not _non_autoemit(item):
            continue
        out.append(
            CorpusItem(
                snapshot_bundle_uuid=str(bundle_uuid),
                report_id=item.report_id,
                item_uuid=str(item.item_uuid),
                item_kind=item.item_kind,
                intent=item.intent,
                reference_decision=extract_decision(item),
            )
        )
        if len(out) >= limit:
            break
    return out


def _covers_kinds(items: list[CorpusItem], min_per_kind: int) -> bool:
    c = Counter(i.item_kind for i in items)
    return all(c.get(k, 0) >= min_per_kind for k in ("action", "watch"))


class CorpusUnavailable(RuntimeError):
    pass


async def select_replay_corpus(
    session: AsyncSession, *, min_per_kind: int = 1, limit: int = 40
) -> CorpusSelection:
    claude = await _bundle_items_for_profile(session, _HUMAN_PROFILE, limit)
    if _covers_kinds(claude, min_per_kind):
        return CorpusSelection(source="claude_bundle", items=claude)
    hermes = await _bundle_items_for_profile(session, _HERMES_PROFILE, limit)
    if _covers_kinds(hermes, min_per_kind):
        return CorpusSelection(source="hermes_bundle", items=hermes)
    # operator_audit fallback intentionally raises for the human to decide
    # (see docs/runbooks/shadow-replay.md §"P0 census (operator)" rule 3).
    raise CorpusUnavailable(
        "No bundle-backed non-auto_emit corpus with buy+sell+watch coverage; "
        "run the P0 census and decide the corpus manually."
    )
