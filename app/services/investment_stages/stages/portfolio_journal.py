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


class PortfolioJournalStage:
    stage_type = "portfolio_journal"

    async def run(self, context: StageContext) -> StageArtifactPayload:
        portfolio_snaps = context.snapshots_for("portfolio")
        if not portfolio_snaps:
            raise UnavailableStageError("portfolio snapshot missing — required")
        portfolio = portfolio_snaps[0]
        journal_snaps = context.snapshots_for("journal")

        nav = float((portfolio.payload_json or {}).get("nav_krw", 0.0))
        buying_power = float(
            (portfolio.payload_json or {}).get("buying_power_krw", 0.0)
        )
        bp_ratio = (buying_power / nav) if nav > 0 else 0.0

        entries = []
        for snap in journal_snaps:
            entries.extend((snap.payload_json or {}).get("entries", []))

        citations = [
            StageCitation(
                snapshot_uuid=portfolio.snapshot_uuid,
                snapshot_kind="portfolio",
                payload_path="$.buying_power_krw",
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
