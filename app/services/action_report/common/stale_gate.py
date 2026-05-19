"""ROB-269 Phase 3 — Post-generation stale gate (Decision 4 layer (iii)).

Deterministic regex/substring scan over the generated report text. Catches
executable action language ("매수", "buy", "trim", ...) when the underlying
snapshot bundle is too stale to justify it.

Layered with:
* Layer (i) — DB CHECK ck_investment_reports_no_published_on_hard_stale on
  the row itself.
* Layer (ii) — pre-LLM ``derive_generator_constraints`` (next module) that
  steers the generator before it produces text.
* Layer (iii) — this module, applied post-generation as a safety net for
  cases where the LLM ignored layer (ii).

Phase 3 plan §2 + pre-plan Decision 4.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from app.services.action_report.common.critical_kinds import (
    CRITICAL_KIND_DEGRADING_STATUSES,
    CRITICAL_SNAPSHOT_KINDS,
)

# Korean forbidden verbs — substring-matched because Korean particles attach
# without word boundaries.
_KR_FORBIDDEN: tuple[str, ...] = (
    "분할매수",
    "분할매도",
    "매수",
    "매도",
    "사세요",
    "파세요",
    "추격",
    "익절",
    "손절",
)

# English forbidden verbs — stem-matched with optional inflection suffix
# (-s/-ed/-ing) so "buying" / "added" / "trimming" all trigger but "address"
# does not.
_EN_PATTERN = re.compile(
    r"\b(buy|sell|long|short|add|trim|stop)(s|ed|ing)?\b",
    re.IGNORECASE,
)

# Bundle statuses that always block executable action language.
_BUNDLE_STATUS_BLOCKS: frozenset[str] = frozenset({"stale_fallback", "failed"})

_EXCERPT_RADIUS = 20


@dataclass(frozen=True)
class StaleLintViolation:
    snapshot_kind: str | None
    """Source kind that triggered the block, or ``None`` if bundle-level."""
    matched_verb: str
    excerpt: str


@dataclass(frozen=True)
class StaleLintResult:
    ok: bool
    violations: list[StaleLintViolation]


def lint_action_language(
    *,
    report_text: str,
    bundle_status: str | None,
    freshness_summary: Mapping[str, Any] | None,
    account_scope: str | None,
) -> StaleLintResult:
    """Return a lint result for the generated report text.

    Inputs:
    * ``report_text`` — the LLM-generated markdown body.
    * ``bundle_status`` — ``investment_snapshot_bundles.status`` of the bundle
      this report was generated against. ``None`` means legacy report (pre
      Phase 3, no bundle linkage) — bypassed.
    * ``freshness_summary`` — the same JSON shape stored on
      ``investment_reports.snapshot_freshness_summary``. ``None`` means no
      summary available; bypassed when ``bundle_status`` is also None.
    * ``account_scope`` — the report's account_scope. ``None`` means
      informational (no broker context) and bypasses the gate; only reports
      tied to an actual account get their action language blocked.

    Output: a ``StaleLintResult``. ``ok=True`` means safe to publish.
    ``ok=False`` carries a non-empty ``violations`` list.
    """
    if not _is_blocking_state(
        bundle_status=bundle_status,
        freshness_summary=freshness_summary,
        account_scope=account_scope,
    ):
        return StaleLintResult(ok=True, violations=[])

    violations = _find_action_verbs(report_text)
    return StaleLintResult(ok=not violations, violations=violations)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------
def _is_blocking_state(
    *,
    bundle_status: str | None,
    freshness_summary: Mapping[str, Any] | None,
    account_scope: str | None,
) -> bool:
    # Legacy report — Phase 3 does not retroactively gate.
    if bundle_status is None:
        return False
    # Informational report — no broker context, executable language is moot.
    if account_scope is None:
        return False
    # Bundle-level stale states always block.
    if bundle_status in _BUNDLE_STATUS_BLOCKS:
        return True
    # Per-kind: only the CRITICAL kinds (portfolio / journal / watch_context
    # / market — see ``critical_kinds.py``) block executable action language.
    # Optional kinds (news / naver / toss / browser / invest_page /
    # candidate_universe / symbol) being unavailable degrades the bundle to
    # ``partial`` but must NOT block — this aligns the post-gen linter with
    # ``generator_constraints.derive_generator_constraints`` so the two
    # stale-gate layers agree on the contract.
    if freshness_summary:
        for kind in CRITICAL_SNAPSHOT_KINDS:
            info = freshness_summary.get(kind)
            if isinstance(info, Mapping):
                kind_status = info.get("status")
                if kind_status in CRITICAL_KIND_DEGRADING_STATUSES:
                    return True
    return False


def _find_action_verbs(text: str) -> list[StaleLintViolation]:
    violations: list[StaleLintViolation] = []
    seen_positions: set[int] = set()

    # Korean — substring match. Longest first so 분할매수 wins over 매수 at
    # the same offset (we record the longer one and skip duplicates at the
    # same start index).
    for verb in _KR_FORBIDDEN:
        start = 0
        while True:
            idx = text.find(verb, start)
            if idx == -1:
                break
            if idx not in seen_positions:
                seen_positions.add(idx)
                excerpt = _excerpt(text, idx, len(verb))
                violations.append(
                    StaleLintViolation(
                        snapshot_kind=None, matched_verb=verb, excerpt=excerpt
                    )
                )
            start = idx + len(verb)

    # English — word-boundary regex with optional inflection.
    for match in _EN_PATTERN.finditer(text):
        verb = match.group(0)
        idx = match.start()
        if idx in seen_positions:
            continue
        seen_positions.add(idx)
        excerpt = _excerpt(text, idx, len(verb))
        violations.append(
            StaleLintViolation(snapshot_kind=None, matched_verb=verb, excerpt=excerpt)
        )

    return violations


def _excerpt(text: str, idx: int, length: int) -> str:
    start = max(0, idx - _EXCERPT_RADIUS)
    end = min(len(text), idx + length + _EXCERPT_RADIUS)
    snippet = text[start:end]
    if start > 0:
        snippet = "…" + snippet
    if end < len(text):
        snippet = snippet + "…"
    return snippet
