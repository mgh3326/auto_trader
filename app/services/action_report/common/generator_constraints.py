"""ROB-269 Phase 3 — Pre-LLM generator constraints (Decision 4 layer (ii)).

Translates a snapshot bundle's freshness/coverage state into directives the
report generator (LLM prompt + decision-tree) must honor. This is the
**pre-generation** half of the stale gate; ``stale_gate.lint_action_language``
is the **post-generation** safety net.

The output is intentionally small and Korean-facing — the ``reason_ko`` is
designed to be passed through directly to the user-facing report copy when
``allow_action_language=False``.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

from app.services.action_report.common.critical_kinds import (
    CRITICAL_KIND_DEGRADING_STATUSES,
    CRITICAL_SNAPSHOT_KINDS,
)

ForcedActionMode = Literal["no_action", "informational_only", "default"]


@dataclass(frozen=True)
class GeneratorConstraints:
    """Pre-LLM constraints derived from the bundle state.

    ``allow_action_language`` is the binary gate; when ``False``, the
    generator must avoid matching the verbs in
    ``app.services.action_report.common.stale_gate``. ``forced_action_mode``
    is a finer hint for downstream UI / wording. ``reason_ko`` is a
    user-facing Korean explanation suitable for embedding in the report's
    no-action note.
    """

    allow_action_language: bool
    forced_action_mode: ForcedActionMode
    reason_ko: str


def derive_generator_constraints(
    *,
    bundle_status: str | None,
    freshness_summary: Mapping[str, Any] | None,
    account_scope: str | None,
) -> GeneratorConstraints:
    """Map bundle state → generator constraints.

    Parameters mirror the report's persisted snapshot metadata so callers
    can pass either a freshly-built bundle view or a re-read
    ``InvestmentReport.snapshot_freshness_summary`` directly.
    """
    # Informational reports — no account context, action language is moot.
    if account_scope is None:
        return GeneratorConstraints(
            allow_action_language=True,
            forced_action_mode="default",
            reason_ko="",
        )

    # Bundle failed entirely — no data, no action language.
    if bundle_status == "failed":
        return GeneratorConstraints(
            allow_action_language=False,
            forced_action_mode="no_action",
            reason_ko="스냅샷 수집 실패 — 매수/매도 권고 불가",
        )

    # Bundle reused stale fallback — fresh data wasn't available; only
    # informational language allowed.
    if bundle_status == "stale_fallback":
        return GeneratorConstraints(
            allow_action_language=False,
            forced_action_mode="no_action",
            reason_ko="스냅샷 hard-stale fallback — 매수/매도 권고 불가",
        )

    # Per-kind checks. Even ``complete`` / ``partial`` bundles must degrade
    # if a critical kind is missing.
    if freshness_summary:
        for kind in CRITICAL_SNAPSHOT_KINDS:
            info = freshness_summary.get(kind)
            if not isinstance(info, Mapping):
                continue
            kind_status = info.get("status")
            if kind_status in CRITICAL_KIND_DEGRADING_STATUSES:
                reason_ko = _critical_kind_reason(kind, kind_status)
                return GeneratorConstraints(
                    allow_action_language=False,
                    forced_action_mode="informational_only",
                    reason_ko=reason_ko,
                )

    # Bundle missing entirely (legacy or pre-Phase-3) — pass through; the
    # post-gen lint also bypasses in this state.
    if bundle_status is None:
        return GeneratorConstraints(
            allow_action_language=True,
            forced_action_mode="default",
            reason_ko="",
        )

    # complete / partial / reused with all critical kinds OK.
    return GeneratorConstraints(
        allow_action_language=True,
        forced_action_mode="default",
        reason_ko="",
    )


def _critical_kind_reason(kind: str, status: str) -> str:
    if kind == "portfolio":
        return "포지션 데이터 확인 불가 — 매수/매도 권고 불가"
    if kind == "journal":
        return "거래일지 데이터 확인 불가 — 매수/매도 권고 불가"
    if kind == "watch_context":
        return "감시 컨텍스트 확인 불가 — 매수/매도 권고 불가"
    if kind == "market":
        return "시장 스냅샷 확인 불가 — 매수/매도 권고 불가"
    return f"{kind} 스냅샷 {status} — 매수/매도 권고 불가"
