"""Deterministic market stage (ROB-279)."""

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

_BULL_THRESHOLD = 0.5
_BEAR_THRESHOLD = -0.5

# Per-market primary-index selection order. The stage drives its verdict off the
# first index present with a usable change_percent. KR mirrors the legacy
# single-KOSPI behaviour; US uses S&P 500 with NASDAQ/Dow fallback (ROB-366 B5);
# crypto uses the CoinGecko total-market-cap regime index (ROB-377).
_PRIMARY_INDEX_BY_MARKET: dict[str, tuple[str, ...]] = {
    "kr": ("KOSPI",),
    "us": ("SPX", "NASDAQ", "DJI"),
    "crypto": ("CRYPTO",),
}


def _select_index(indices: dict, market: str) -> tuple[str, float] | None:
    """Return ``(symbol, change_percent)`` for the first usable primary index.

    "Usable" means present with a non-``None`` numeric ``change_percent`` — a
    missing index or a ``None`` change (e.g. yfinance previous_close absent) is
    skipped so the stage fails closed rather than fabricating a flat 0.0%.
    """
    for symbol in _PRIMARY_INDEX_BY_MARKET.get(market, ("KOSPI",)):
        entry = indices.get(symbol)
        if not isinstance(entry, dict):
            continue
        change = entry.get("change_percent")
        if change is None:
            continue
        try:
            return symbol, float(change)
        except (TypeError, ValueError):
            continue
    return None


class MarketStage:
    stage_type = "market"

    async def run(self, context: StageContext) -> StageArtifactPayload:
        snapshots = context.snapshots_for("market")
        if not snapshots:
            raise UnavailableStageError("market snapshot missing from bundle")

        market = (
            (context.market or context.bundle_metadata.get("market") or "kr")
            .strip()
            .lower()
        )

        snapshot = snapshots[0]
        indices = (snapshot.payload_json or {}).get("indices") or {}
        selected = _select_index(indices, market)
        if selected is None:
            raise UnavailableStageError(f"market index unavailable for {market}")
        symbol, change = selected

        if change >= _BULL_THRESHOLD:
            verdict = StageVerdict.BULL
        elif change <= _BEAR_THRESHOLD:
            verdict = StageVerdict.BEAR
        else:
            verdict = StageVerdict.NEUTRAL

        confidence = min(int(abs(change) * 30), 90)

        return StageArtifactPayload(
            stage_type=self.stage_type,
            verdict=verdict,
            confidence=max(confidence, 30 if verdict != StageVerdict.NEUTRAL else 20),
            summary=f"{symbol} change_percent={change:+.2f}%",
            key_points=[f"{symbol} {change:+.2f}%"],
            buy_evidence=[f"{symbol} 상승 {change:+.2f}%"]
            if verdict == StageVerdict.BULL
            else [],
            sell_evidence=[f"{symbol} 하락 {change:+.2f}%"]
            if verdict == StageVerdict.BEAR
            else [],
            cited_snapshots=[
                StageCitation(
                    snapshot_uuid=snapshot.snapshot_uuid,
                    snapshot_kind="market",
                    payload_path=f"$.indices.{symbol}.change_percent",
                )
            ],
        )
