import logging
from datetime import datetime
from typing import Any

from app.analysis.stages.base import BaseStageAnalyzer, StageContext
from app.mcp_server.tooling.market_data_indicators import (
    _calculate_atr,
    _calculate_rsi,
    _calculate_sma,
    _fetch_ohlcv_for_indicators,
)
from app.schemas.research_pipeline import (
    MarketSignals,
    SourceFreshness,
    StageOutput,
    StageVerdict,
)

logger = logging.getLogger(__name__)


async def _fetch_market_snapshot(symbol: str, instrument_type: str) -> dict[str, Any]:
    """Fetch OHLCV and compute basic indicators for market stage."""
    # market_type in _fetch_ohlcv_for_indicators expects crypto / equity_kr / equity_us
    df = await _fetch_ohlcv_for_indicators(symbol, instrument_type, count=250)
    if df.empty or len(df) < 30:
        raise ValueError(f"Insufficient market data for {symbol}")

    close = df["close"].astype(float)
    high = df["high"].astype(float)
    low = df["low"].astype(float)
    volume = df["volume"].astype(float)

    # Latest values
    last_close = close.iloc[-1]
    prev_close = close.iloc[-2]
    change_pct = (last_close - prev_close) / prev_close * 100

    # Indicators
    rsi_14 = _calculate_rsi(close, period=14)["14"]
    atr_14 = _calculate_atr(high, low, close, period=14)["14"]

    # Volume ratio: last day volume / 20-day avg volume (excluding today)
    avg_vol_20d = volume.iloc[-21:-1].mean()
    volume_ratio_20d = float(volume.iloc[-1] / avg_vol_20d) if avg_vol_20d > 0 else 0.0

    # Trend calculation: use SMA 20 and 60
    smas = _calculate_sma(close, periods=[20, 60])
    sma20 = smas["20"]
    sma60 = smas["60"]

    if sma20 and sma60:
        if last_close > sma20 > sma60:
            trend = "uptrend"
        elif last_close < sma20 < sma60:
            trend = "downtrend"
        else:
            trend = "flat"
    else:
        trend = "unknown"

    return {
        "last_close": float(last_close),
        "change_pct": round(float(change_pct), 2),
        "rsi_14": rsi_14 if rsi_14 is not None else 50.0,
        "atr_14": atr_14 if atr_14 is not None else 0.0,
        "volume_ratio_20d": round(volume_ratio_20d, 2),
        "trend": trend,
        "snapshot_at_iso": datetime.utcnow().isoformat() + "Z",
    }


class MarketStageAnalyzer(BaseStageAnalyzer):
    stage_type = "market"

    async def analyze(self, ctx: StageContext) -> StageOutput:
        try:
            raw = await _fetch_market_snapshot(ctx.symbol, ctx.instrument_type)
        except Exception as exc:
            logger.error(f"Market analysis failed for {ctx.symbol}: {exc}")
            return StageOutput(
                stage_type=self.stage_type,
                verdict=StageVerdict.UNAVAILABLE,
                confidence=0,
                signals=MarketSignals(
                    last_close=0.0,
                    change_pct=0.0,
                    rsi_14=50.0,
                    atr_14=0.0,
                    volume_ratio_20d=0.0,
                    trend="unknown",
                ),
            )

        signals = MarketSignals(
            last_close=raw["last_close"],
            change_pct=raw["change_pct"],
            rsi_14=raw["rsi_14"],
            atr_14=raw["atr_14"],
            volume_ratio_20d=raw["volume_ratio_20d"],
            trend=raw["trend"],
        )

        # Verdict mapping rule:
        # BULL: change_pct > 0.5 and rsi_14 < 75 and trend == 'uptrend'
        # BEAR: change_pct < -0.5 and rsi_14 > 25 and trend == 'downtrend'
        # NEUTRAL: otherwise
        verdict = StageVerdict.NEUTRAL
        if signals.change_pct > 0.5 and signals.rsi_14 < 75 and signals.trend == "uptrend":
            verdict = StageVerdict.BULL
        elif signals.change_pct < -0.5 and signals.rsi_14 > 25 and signals.trend == "downtrend":
            verdict = StageVerdict.BEAR

        return StageOutput(
            stage_type=self.stage_type,
            verdict=verdict,
            confidence=70,  # Static confidence for now
            signals=signals,
            snapshot_at=datetime.fromisoformat(raw["snapshot_at_iso"].replace("Z", "+00:00")),
            source_freshness=SourceFreshness(
                newest_age_minutes=0,
                oldest_age_minutes=0,
                source_count=1,
            ),
        )
