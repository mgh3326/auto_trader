"""ROB-318 Phase 3 — deterministic report diagnostics.

Shared, deterministic helpers that turn a snapshot bundle's freshness/coverage
state into structured, operator-facing diagnostics. **No in-process LLM** lives
here (PR #898 guard): this module only classifies facts. The human-readable
narrative is composed by Hermes; the Korean strings produced here are
deterministic fallback templates, not generated prose.

Two surfaces:

1. ``reason_code`` — a closed enum attached per snapshot kind so the collector,
   the bundle assembler, the report response, and the frontend all switch on the
   same values instead of parsing free text. Collectors emit a specific code
   (e.g. ``user_id_missing``); otherwise the code is derived from the freshness
   status.
2. ``why_no_action`` — a report-level classification of *why* a report concludes
   no-action: genuine no-action vs blocked by missing data vs blocked by stale
   data. This is what lets ``/invest/reports`` stop showing the generic
   "포지션 데이터 확인 불가" for every degraded source.

```
collector.errors_json{reason_code?, reason?}  +  status (fresh|hard_stale|unavailable|...)
                 |
                 v   reason_code_for() / sanitize_reason()
                 |
   freshness_summary[kind]{status, reason_code, reason}            (Slice 1)
                 |
                 v   classify_why_no_action(freshness_summary, bundle_status, items)
                 |
   why_no_action{kind, blocking_sources, reason_ko}                (Slice 2)
```
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any, Literal

from app.services.action_report.common.critical_kinds import (
    CRITICAL_KIND_DEGRADING_STATUSES,
    CRITICAL_SNAPSHOT_KINDS,
)

# Closed set of reason codes. Collectors emit the specific ones; the generic
# ones are derived from a freshness/bundle status. Unknown collector reasons
# map to ``unknown`` — never a free-form code — so the frontend can switch.
ReasonCode = Literal[
    "user_id_missing",  # broker collector got no user_id (ROB-278 lockdown)
    "kis_fetch_failed",  # KIS read-only fetch raised / returned nothing
    "stale",  # data present but past freshness TTL (soft/hard_stale)
    "unavailable",  # source produced no usable data
    "failed",  # bundle/source hard-failed
    "unknown",  # degraded for a reason we didn't classify
]

_VALID_REASON_CODES: frozenset[str] = frozenset(
    (
        "user_id_missing",
        "kis_fetch_failed",
        "stale",
        "unavailable",
        "failed",
        "unknown",
    )
)

WhyNoActionKind = Literal["data_insufficient", "stale_gated", "real_no_action"]

# Per-kind statuses that block action language (mirrors
# critical_kinds.CRITICAL_KIND_DEGRADING_STATUSES, kept local to avoid a
# circular intent: "missing data" vs "stale data" are split here).
_MISSING_STATUSES = frozenset(("unavailable", "failed"))
_STALE_STATUSES = frozenset(("hard_stale",))

_MAX_REASON_LEN = 200

# Redact token/secret-shaped substrings before a free-form collector reason is
# surfaced to the operator. The reason_code (closed enum) is the primary signal;
# this string is secondary context only.
_SECRET_RE = re.compile(
    r"(?i)(token|secret|bearer|api[_-]?key|password|authorization)"
    r"\s*[:=]?\s*\S+"
)
_LONG_OPAQUE_RE = re.compile(r"\b[A-Za-z0-9_\-]{32,}\b")


def sanitize_reason(reason: str | None) -> str | None:
    """Bound + redact a free-form collector reason for operator display.

    Returns ``None`` for empty input. Strips credential/token-shaped tokens and
    long opaque blobs (JWT/keys), collapses whitespace, and caps length so a raw
    upstream error cannot leak account or secret material into the report/UI.
    """
    if not reason:
        return None
    text = _SECRET_RE.sub("[REDACTED]", reason)
    text = _LONG_OPAQUE_RE.sub("[REDACTED]", text)
    text = " ".join(text.split())
    if len(text) > _MAX_REASON_LEN:
        text = text[: _MAX_REASON_LEN - 1].rstrip() + "…"
    return text or None


def reason_code_for(
    status: str | None, errors_json: Mapping[str, Any] | None
) -> ReasonCode:
    """Resolve the closed ``ReasonCode`` for a degraded snapshot kind.

    Prefers a collector-emitted ``errors_json['reason_code']`` when it is a known
    code; otherwise derives a generic code from the freshness/bundle status.
    """
    if errors_json:
        emitted = errors_json.get("reason_code")
        if isinstance(emitted, str) and emitted in _VALID_REASON_CODES:
            return emitted  # type: ignore[return-value]
    if status in ("hard_stale", "soft_stale"):
        return "stale"
    if status == "unavailable":
        return "unavailable"
    if status == "failed":
        return "failed"
    return "unknown"


def build_kind_diagnostic(
    status: str | None, errors_json: Mapping[str, Any] | None
) -> dict[str, Any]:
    """Return the additive diagnostic keys for ``freshness_summary[kind]``.

    Always returns ``reason_code``; includes ``reason`` only when a sanitized
    free-form reason survives. Callers merge this into the existing per-kind
    dict, so unknown keys stay backward compatible.
    """
    diagnostic: dict[str, Any] = {
        "reason_code": reason_code_for(status, errors_json),
    }
    reason = sanitize_reason((errors_json or {}).get("reason") if errors_json else None)
    if reason is not None:
        diagnostic["reason"] = reason
    return diagnostic


_WHY_NO_ACTION_REASON_KO: dict[WhyNoActionKind, str] = {
    "data_insufficient": "데이터 부족 — {sources} 확인 불가로 매수/매도 권고 보류",
    "stale_gated": "스냅샷 stale — {sources} 신선도 부족으로 매수/매도 권고 보류",
    "real_no_action": "데이터 충분 — 현 시점 신규 액션 없음(관망)",
}


def classify_why_no_action(
    *,
    freshness_summary: Mapping[str, Any] | None,
    bundle_status: str | None,
    has_action_items: bool,
) -> dict[str, Any] | None:
    """Classify *why* a report has no actionable recommendation.

    Returns ``None`` when action language is allowed AND the report carries
    action items (i.e. there is an action — nothing to explain). Otherwise:

    * ``data_insufficient`` — bundle failed, or a critical kind is
      unavailable/failed (missing data, not merely stale).
    * ``stale_gated`` — bundle is a stale fallback, or a critical kind is
      hard_stale (data exists but is too old to act on).
    * ``real_no_action`` — data is sufficient and fresh, but no action item was
      produced (genuine "watch / hold" conclusion).

    Precedence: missing > stale > genuine. ``blocking_sources`` lists the
    critical kinds that triggered a gated classification.
    """
    summary = freshness_summary or {}
    missing: list[str] = []
    stale: list[str] = []
    for kind in CRITICAL_SNAPSHOT_KINDS:
        info = summary.get(kind)
        if not isinstance(info, Mapping):
            continue
        status = info.get("status")
        if status in _MISSING_STATUSES:
            missing.append(kind)
        elif status in _STALE_STATUSES:
            stale.append(kind)

    if bundle_status == "failed":
        return _why("data_insufficient", missing or ["bundle"])
    if missing:
        return _why("data_insufficient", missing)
    if bundle_status == "stale_fallback":
        return _why("stale_gated", stale or ["bundle"])
    if stale:
        return _why("stale_gated", stale)
    if not has_action_items:
        return _why("real_no_action", [])
    return None


def _why(kind: WhyNoActionKind, blocking_sources: list[str]) -> dict[str, Any]:
    sources_label = ", ".join(blocking_sources) if blocking_sources else "전체"
    return {
        "kind": kind,
        "blocking_sources": blocking_sources,
        "reason_ko": _WHY_NO_ACTION_REASON_KO[kind].format(sources=sources_label),
    }


# Report-level rollups (PR-B). Both are derived deterministically from the
# bundle's freshness/coverage state — no per-symbol or LLM reasoning.

ReportQualityGrade = Literal["high_confidence", "informational_only", "no_action"]


def build_data_sufficiency_by_source(
    freshness_summary: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Per-source data-sufficiency view: every known kind with its status and
    (when degraded) the reason_code/reason already threaded by Slice 1.

    This is the canonical "structured unavailable" feed (ROB-301 design-doc
    constraint): the operator/Hermes can see exactly which source is missing or
    stale and why, instead of inferring from a generic message.
    """
    summary = freshness_summary or {}
    out: dict[str, Any] = {}
    for kind, info in summary.items():
        if kind == "overall" or not isinstance(info, Mapping):
            continue
        entry: dict[str, Any] = {"status": info.get("status")}
        for key in ("reason_code", "reason", "as_of"):
            if key in info:
                entry[key] = info[key]
        out[kind] = entry
    return out


def build_report_quality_summary(
    *,
    freshness_summary: Mapping[str, Any] | None,
    bundle_status: str | None,
) -> dict[str, Any]:
    """Report-level quality rollup: a grade + per-status counts.

    Grade:
    * ``no_action`` — bundle failed or fell back to stale data.
    * ``informational_only`` — a critical kind is degrading (the stale gate
      forces advisory/no-action language).
    * ``high_confidence`` — all critical kinds usable.
    """
    summary = freshness_summary or {}
    counts: dict[str, int] = {}
    critical_statuses: list[str | None] = []
    for kind, info in summary.items():
        if kind == "overall" or not isinstance(info, Mapping):
            continue
        status = info.get("status")
        counts[str(status)] = counts.get(str(status), 0) + 1
        if kind in CRITICAL_SNAPSHOT_KINDS:
            critical_statuses.append(status)

    total = sum(counts.values())
    fresh = counts.get("fresh", 0)
    fresh_pct = round(100 * fresh / total) if total else 0

    grade: ReportQualityGrade
    if bundle_status in ("failed", "stale_fallback"):
        grade = "no_action"
    elif any(s in CRITICAL_KIND_DEGRADING_STATUSES for s in critical_statuses):
        grade = "informational_only"
    else:
        grade = "high_confidence"

    return {
        "grade": grade,
        "bundle_status": bundle_status,
        "freshness_overall": summary.get("overall"),
        "kind_status_counts": counts,
        "fresh_coverage_pct": fresh_pct,
    }


def build_report_diagnostics(
    *,
    freshness_summary: Mapping[str, Any] | None,
    bundle_status: str | None,
    why_no_action: dict[str, Any] | None,
) -> dict[str, Any]:
    """Assemble the ``snapshot_report_diagnostics`` JSONB payload (PR-B).

    Bundles the three deterministic rollups persisted on the report and exported
    to Hermes. ``why_no_action`` is computed by the caller (it needs to know
    whether action items were produced).
    """
    return {
        "why_no_action": why_no_action,
        "data_sufficiency_by_source": build_data_sufficiency_by_source(
            freshness_summary
        ),
        "report_quality_summary": build_report_quality_summary(
            freshness_summary=freshness_summary,
            bundle_status=bundle_status,
        ),
    }
