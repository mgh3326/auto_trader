"""ROB-697 (M1) — read-only replay-corpus selection.

ROB-501 guard: this module is SELECT-only DB access. NO writes, NO LLM,
NO network. It picks which past bundle-backed report items are eligible
to be replayed by the (later) ``claude -p`` A' driver.

Corpus source is decided by the P0 census against the real production DB
(see ``docs/runbooks/shadow-replay.md`` §"P0 census (operator)"). The
2026-07-04 census found the literal ``CLAUDE_ADVISOR`` profile is EMPTY:
the operator's actual Claude-in-the-loop decisions live under a *family*
of profile names (``claude_code``, ``claude_code_kislive``,
``claude_us_open_advisory``, ``CLAUDE_REGULAR_ADVISOR``,
``CLAUDE_NXT_ADVISOR``, ...) plus custom session labels (e.g. ``파이리``).
So the primary source ``claude_family`` matches case-insensitive
``%claude%`` plus an explicit, env-extensible set of custom labels.
``hermes_bundle`` (fully-automated Hermes composition) is kept only as a
last-resort fallback; if neither has coverage, ``select_replay_corpus``
raises ``CorpusUnavailable`` and defers to the operator.
"""

from __future__ import annotations

import os
from collections import Counter
from dataclasses import dataclass
from typing import Any

from sqlalchemy import ColumnElement, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.investment_reports import InvestmentReport, InvestmentReportItem
from app.services.shadow_replay.scoring import extract_decision

_HERMES_PROFILE = "HERMES_ADVISOR"

# Claude-family matching (ROB-697 P0 census). Case-insensitive "%claude%"
# catches claude_code / CLAUDE_REGULAR_ADVISOR / etc.; custom session labels
# that don't contain "claude" (e.g. the Korean label "파이리") are listed
# explicitly and can be extended via the env var below (comma-separated),
# mirroring the INVESTMENT_ADVISORY_DRAFT_PROFILES convention.
_CLAUDE_FAMILY_LIKE = "%claude%"
_CLAUDE_FAMILY_EXTRA = frozenset({"파이리"})


def _claude_family_extra() -> frozenset[str]:
    raw = os.getenv("SHADOW_REPLAY_CLAUDE_PROFILES", "")
    return _CLAUDE_FAMILY_EXTRA | {p.strip() for p in raw.split(",") if p.strip()}


def _claude_family_where() -> ColumnElement[bool]:
    return or_(
        func.lower(InvestmentReport.created_by_profile).like(_CLAUDE_FAMILY_LIKE),
        InvestmentReport.created_by_profile.in_(_claude_family_extra()),
    )


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
    source: str  # "claude_family" | "hermes_bundle" | "operator_audit"
    items: list[CorpusItem]


def _non_autoemit(item: Any) -> bool:
    ev = item.evidence_snapshot or {}
    if ev.get("source") == "auto_emit":
        return False
    proposer = str(ev.get("proposer", ""))
    return not proposer.startswith("auto_emit/") and proposer != "intraday_floor"


async def _bundle_items(
    session: AsyncSession, where: ColumnElement[bool], limit: int
) -> list[CorpusItem]:
    """Bundle-backed, non-auto_emit report items matching ``where``, newest first."""
    stmt = (
        select(InvestmentReportItem, InvestmentReport.snapshot_bundle_uuid)
        .join(InvestmentReport, InvestmentReport.id == InvestmentReportItem.report_id)
        .where(InvestmentReport.snapshot_bundle_uuid.isnot(None))
        .where(where)
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


async def _bundle_items_for_profile(
    session: AsyncSession, profile: str, limit: int
) -> list[CorpusItem]:
    return await _bundle_items(
        session, InvestmentReport.created_by_profile == profile, limit
    )


async def _claude_family_items(session: AsyncSession, limit: int) -> list[CorpusItem]:
    return await _bundle_items(session, _claude_family_where(), limit)


def _covers_kinds(items: list[CorpusItem], min_per_kind: int) -> bool:
    c = Counter(i.item_kind for i in items)
    return all(c.get(k, 0) >= min_per_kind for k in ("action", "watch"))


class CorpusUnavailable(RuntimeError):
    pass


async def select_replay_corpus(
    session: AsyncSession, *, min_per_kind: int = 1, limit: int = 40
) -> CorpusSelection:
    claude = await _claude_family_items(session, limit)
    if _covers_kinds(claude, min_per_kind):
        return CorpusSelection(source="claude_family", items=claude)
    hermes = await _bundle_items_for_profile(session, _HERMES_PROFILE, limit)
    if _covers_kinds(hermes, min_per_kind):
        return CorpusSelection(source="hermes_bundle", items=hermes)
    # operator_audit fallback intentionally raises for the human to decide
    # (see docs/runbooks/shadow-replay.md §"P0 census (operator)" rule 3).
    raise CorpusUnavailable(
        "No bundle-backed non-auto_emit Claude-family (or Hermes) corpus with "
        "action+watch coverage; run the P0 census and decide the corpus manually."
    )
