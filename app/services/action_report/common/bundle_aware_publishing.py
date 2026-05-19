"""ROB-269 Phase 3 — Bundle-aware publishing helper.

Combines the layer (ii) ``derive_generator_constraints`` and the layer (iii)
``lint_action_language`` calls into a single decision for the report ingest
path, and provides flag-gated enforcement via
``ACTION_REPORT_BUNDLE_BASED_GENERATION_ENABLED``.

Wiring contract for ``InvestmentReportIngestionService.ingest``:

1. The service always calls ``evaluate_stale_gate_for_ingest(request)`` to
   compute an advisory ``BundleAwarePublishingResult``.
2. The advisory is attached to ``report_metadata`` under the
   ``"stale_gate"`` key for audit (post-fact reconstruction of "what the
   gate said when this report was ingested").
3. If the flag is enabled AND the result rejects, the service raises
   ``StaleGateRejection`` before insert — no row is written.
4. If the flag is disabled, the gate is purely advisory — rows go through
   regardless of the result.
5. Legacy reports (no ``snapshot_freshness_summary``) and informational
   reports (``account_scope is None``) always bypass both layers and the
   gate is a no-op.

The bundle.status field used by the gate layers is approximated from
``snapshot_freshness_summary['overall']`` — Phase 4 generators that hold
the actual bundle row can pass bundle.status more directly via a future
``snapshot_bundle_status`` request field if precision matters.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from app.schemas.investment_reports import IngestReportRequest
from app.services.action_report.common.generator_constraints import (
    GeneratorConstraints,
    derive_generator_constraints,
)
from app.services.action_report.common.stale_gate import (
    StaleLintResult,
    lint_action_language,
)

# ``overall`` → approximated bundle.status. Used only as a coarse signal
# for layer (ii) — per-kind statuses still drive critical-kind degradation.
_OVERALL_TO_BUNDLE_STATUS: dict[str, str] = {
    "fresh": "complete",
    "soft_stale": "partial",
    "partial": "partial",
    "hard_stale": "stale_fallback",
    "failed": "failed",
    "unavailable": "stale_fallback",
}


@dataclass(frozen=True)
class BundleAwarePublishingResult:
    """Combined Decision 4 layer (ii) + (iii) result for an ingest request."""

    constraints: GeneratorConstraints
    lint: StaleLintResult

    @property
    def reject(self) -> bool:
        """True iff EITHER layer reports a blocking decision.

        Both layers must independently agree the action language is allowed
        before we declare the result clean. A False from layer (ii) (gate
        disagrees) or a False from layer (iii) (text leak found) is enough
        to reject.
        """
        return (not self.constraints.allow_action_language) or (not self.lint.ok)

    def to_metadata_summary(self) -> dict[str, Any]:
        """Compact JSON-safe shape for the report's ``metadata.stale_gate``."""
        return {
            "constraints": {
                "allow_action_language": self.constraints.allow_action_language,
                "forced_action_mode": self.constraints.forced_action_mode,
                "reason_ko": self.constraints.reason_ko,
            },
            "lint": {
                "ok": self.lint.ok,
                "violations": [
                    {
                        "snapshot_kind": v.snapshot_kind,
                        "matched_verb": v.matched_verb,
                        "excerpt": v.excerpt,
                    }
                    for v in self.lint.violations
                ],
            },
            "reject": self.reject,
        }


class StaleGateRejection(ValueError):
    """Raised by ``enforce_stale_gate_for_ingest`` when the flag is enabled
    and ``BundleAwarePublishingResult.reject`` is True.

    Carries the full result so callers (and tests) can inspect the layer
    that triggered the rejection without re-running the computation.
    """

    def __init__(self, result: BundleAwarePublishingResult):
        self.result = result
        reason = result.constraints.reason_ko or "stale gate rejected publication"
        if not result.lint.ok:
            verbs = sorted({v.matched_verb for v in result.lint.violations})
            reason = f"{reason}; lint matched verbs: {verbs}"
        super().__init__(reason)


def evaluate_stale_gate_for_ingest(
    request: IngestReportRequest,
) -> BundleAwarePublishingResult:
    """Pure evaluation — never raises. Returns advisory result for any
    request (legacy / informational / bundle-aware).
    """
    bundle_status = _infer_bundle_status(request.snapshot_freshness_summary)
    freshness_summary = request.snapshot_freshness_summary
    account_scope = request.account_scope
    report_text = _assemble_report_text(request)

    constraints = derive_generator_constraints(
        bundle_status=bundle_status,
        freshness_summary=freshness_summary,
        account_scope=account_scope,
    )
    lint = lint_action_language(
        report_text=report_text,
        bundle_status=bundle_status,
        freshness_summary=freshness_summary,
        account_scope=account_scope,
    )
    return BundleAwarePublishingResult(constraints=constraints, lint=lint)


def enforce_stale_gate_for_ingest(
    request: IngestReportRequest,
    *,
    flag_enabled: bool,
) -> BundleAwarePublishingResult:
    """Evaluate and (when flag is enabled) raise ``StaleGateRejection`` on
    a blocking result. When flag is disabled the result is advisory only.
    """
    result = evaluate_stale_gate_for_ingest(request)
    if flag_enabled and result.reject:
        raise StaleGateRejection(result)
    return result


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------
def _infer_bundle_status(
    freshness_summary: Mapping[str, Any] | None,
) -> str | None:
    """Approximate bundle.status from ``snapshot_freshness_summary['overall']``.

    Returns ``None`` for legacy reports (no freshness summary at all). The
    gate layers also treat ``None`` as a legacy/pass-through.
    """
    if not freshness_summary:
        return None
    overall = freshness_summary.get("overall")
    if not isinstance(overall, str):
        # Missing key or JSON null — caller's freshness summary is malformed
        # for Phase 3 purposes; treat as legacy/pass-through here. The DB
        # CHECK (layer i) will still reject if the row tries to publish.
        return None
    return _OVERALL_TO_BUNDLE_STATUS.get(overall)


def _assemble_report_text(request: IngestReportRequest) -> str:
    """Concatenate the user-facing copy fields for layer (iii) lint scan."""
    parts = [
        request.title,
        request.summary,
        request.thesis_text or "",
        request.no_action_note or "",
        request.risk_summary or "",
    ]
    return " ".join(p for p in parts if p)
