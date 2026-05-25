"""Deterministic portfolio+journal stage (ROB-279)."""

from __future__ import annotations

from app.schemas.investment_stages import (
    StageArtifactPayload,
    StageCitation,
    StageVerdict,
)
from app.services.investment_stages.stages.base import (
    StageContext,
    UnavailableStageError,
)


def _nested_krw(value: object) -> float | None:
    """Return the KRW amount from a nested ``{"krw": x, "usd": y}`` block.

    The production portfolio collector emits cash / buying power as nested
    per-currency dicts (``payload["cash"] = {"krw": ..., "usd": ...}``). Returns
    ``None`` when the block is absent or carries no KRW figure so callers can
    fall back to the legacy flat key.
    """
    if isinstance(value, dict):
        krw = value.get("krw")
        if krw is not None:
            return float(krw)
    return None


def _portfolio_totals(payload: dict) -> tuple[float, float, float]:
    """Derive ``(nav_krw, buying_power_krw, cash_krw)`` from a portfolio payload.

    ROB-314: the production collector's payload is nested (``cash.krw``,
    ``buying_power.krw``, ``holdings[].value_krw``) and carries no canonical
    total, so NAV is derived as ``sum(holdings value_krw) + cash.krw``. Legacy
    flat keys (``nav_krw`` / ``buying_power_krw`` / ``cash_krw``) are honoured as
    a fallback so older fixtures and manual snapshots keep working.
    """
    cash_krw = _nested_krw(payload.get("cash"))
    if cash_krw is None:
        cash_krw = float(payload.get("cash_krw") or 0.0)

    buying_power_krw = _nested_krw(payload.get("buying_power"))
    if buying_power_krw is None:
        buying_power_krw = float(payload.get("buying_power_krw") or 0.0)

    # Prefer an explicit total when present (legacy/flat), otherwise derive it
    # from the holdings evaluation sum plus cash.
    explicit_nav = payload.get("nav_krw")
    if explicit_nav is None:
        explicit_nav = payload.get("total_evaluation_krw")
    if explicit_nav is not None:
        nav_krw = float(explicit_nav)
    else:
        holdings = payload.get("holdings") or []
        holdings_value_krw = sum(
            float(h.get("value_krw") or 0.0) for h in holdings if isinstance(h, dict)
        )
        nav_krw = holdings_value_krw + cash_krw

    return nav_krw, buying_power_krw, cash_krw


class PortfolioJournalStage:
    stage_type = "portfolio_journal"

    async def run(self, context: StageContext) -> StageArtifactPayload:
        portfolio_snaps = context.snapshots_for("portfolio")
        if not portfolio_snaps:
            raise UnavailableStageError("portfolio snapshot missing — required")
        portfolio = portfolio_snaps[0]
        journal_snaps = context.snapshots_for("journal")

        nav, buying_power, _cash_krw = _portfolio_totals(portfolio.payload_json or {})
        bp_ratio = (buying_power / nav) if nav > 0 else 0.0

        entries = []
        for snap in journal_snaps:
            entries.extend((snap.payload_json or {}).get("entries", []))

        citations = [
            StageCitation(
                snapshot_uuid=portfolio.snapshot_uuid,
                snapshot_kind="portfolio",
                payload_path="$.buying_power.krw",
            )
        ]
        for snap in journal_snaps:
            citations.append(
                StageCitation(
                    snapshot_uuid=snap.snapshot_uuid,
                    snapshot_kind="journal",
                    payload_path="$.entries",
                )
            )

        symbols = ", ".join(e.get("symbol", "?") for e in entries[:5])
        summary = (
            f"NAV={nav:,.0f}, buying_power_krw={buying_power:,.0f} "
            f"({bp_ratio:.1%}), open journal: {symbols or 'none'}"
        )

        return StageArtifactPayload(
            stage_type=self.stage_type,
            verdict=StageVerdict.NEUTRAL,
            confidence=60 if bp_ratio >= 0.05 else 40,
            summary=summary,
            key_points=[e.get("thesis", "") for e in entries[:5] if e.get("thesis")],
            risk_evidence=[] if bp_ratio >= 0.05 else ["buying_power < 5% NAV"],
            cited_snapshots=citations,
            missing_data=[] if journal_snaps else ["journal"],
        )
