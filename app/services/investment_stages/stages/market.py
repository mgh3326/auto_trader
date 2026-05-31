"""Deterministic market stage (ROB-279)."""

from __future__ import annotations

from typing import Any

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


def _coerce_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _altseason_points(altseason: Any) -> tuple[list[str], list[str]]:
    """Return compact Upbit altseason summary/key-point strings.

    The market snapshot stores Upbit altseason as best-effort optional data.
    Missing/non-numeric fields are skipped rather than fabricated, so the
    CRYPTO index remains the only hard gate for the market stage.
    """
    if not isinstance(altseason, dict):
        return [], []

    summary_parts: list[str] = []
    key_points: list[str] = []

    ratio = _coerce_float(altseason.get("ubai_ubmi_ratio"))
    if ratio is not None:
        ratio_text = f"{ratio:.3f}"
        summary_parts.append(f"UBAI/UBMI={ratio_text}")
        key_points.append(f"Upbit altseason UBAI/UBMI={ratio_text}")

    breadth = altseason.get("breadth")
    breadth_pct = None
    if isinstance(breadth, dict):
        breadth_pct = _coerce_float(breadth.get("alts_beating_btc_pct"))
    if breadth_pct is not None:
        breadth_text = f"{breadth_pct * 100:.1f}%"
        summary_parts.append(f"alts_beating_btc={breadth_text}")
        key_points.append(f"Upbit breadth alts beating BTC {breadth_text}")

    return summary_parts, key_points


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

        summary = f"{symbol} change_percent={change:+.2f}%"
        key_points = [f"{symbol} {change:+.2f}%"]
        cited_snapshots = [
            StageCitation(
                snapshot_uuid=snapshot.snapshot_uuid,
                snapshot_kind="market",
                payload_path=f"$.indices.{symbol}.change_percent",
            )
        ]
        if market == "crypto":
            altseason = (snapshot.payload_json or {}).get("altseason")
            altseason_summary_parts, altseason_key_points = _altseason_points(altseason)
            if altseason_summary_parts:
                summary = (
                    f"{summary}; Upbit altseason {', '.join(altseason_summary_parts)}"
                )
                key_points.extend(altseason_key_points)
                if (
                    isinstance(altseason, dict)
                    and _coerce_float(altseason.get("ubai_ubmi_ratio")) is not None
                ):
                    cited_snapshots.append(
                        StageCitation(
                            snapshot_uuid=snapshot.snapshot_uuid,
                            snapshot_kind="market",
                            payload_path="$.altseason.ubai_ubmi_ratio",
                        )
                    )
                breadth = (
                    altseason.get("breadth") if isinstance(altseason, dict) else None
                )
                if (
                    isinstance(breadth, dict)
                    and _coerce_float(breadth.get("alts_beating_btc_pct")) is not None
                ):
                    cited_snapshots.append(
                        StageCitation(
                            snapshot_uuid=snapshot.snapshot_uuid,
                            snapshot_kind="market",
                            payload_path="$.altseason.breadth.alts_beating_btc_pct",
                        )
                    )

        return StageArtifactPayload(
            stage_type=self.stage_type,
            verdict=verdict,
            confidence=max(confidence, 30 if verdict != StageVerdict.NEUTRAL else 20),
            summary=summary,
            key_points=key_points,
            buy_evidence=[f"{symbol} 상승 {change:+.2f}%"]
            if verdict == StageVerdict.BULL
            else [],
            sell_evidence=[f"{symbol} 하락 {change:+.2f}%"]
            if verdict == StageVerdict.BEAR
            else [],
            cited_snapshots=cited_snapshots,
        )
