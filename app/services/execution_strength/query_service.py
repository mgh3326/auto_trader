"""ROB-462: KR 주식 체결강도 (execution strength) read model.

체결강도 = 매수체결량 / 매도체결량 × 100. KIS FHKST01010100 (주식현재가 시세)
returns the official value directly as ``cttr`` — we trust the broker's value
rather than recomputing. Pure transform; the broker fetch + freshness tagging
live in the MCP tool.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ExecutionStrengthData:
    symbol: str
    as_of: str | None
    execution_strength_pct: float | None
    buy_volume: float | None
    sell_volume: float | None
    trend: str | None


def _to_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def _classify_trend(cttr: float | None) -> str | None:
    """체결강도 100 = 매수/매도 균형. >100 매수우위, <100 매도우위."""
    if cttr is None:
        return None
    if cttr > 100.0:
        return "buy_dominant"
    if cttr < 100.0:
        return "sell_dominant"
    return "neutral"


def compute_execution_strength(
    raw: dict[str, Any], *, symbol: str, as_of: str | None
) -> ExecutionStrengthData:
    """Build the read model from KIS FHKST01010100 raw output fields.

    ``cttr`` is the authoritative 체결강도. Buy/sell contracted volumes are
    surfaced when present, else None (missing != a fabricated 0).
    """
    cttr = _to_float(raw.get("cttr"))
    return ExecutionStrengthData(
        symbol=symbol,
        as_of=as_of,
        execution_strength_pct=cttr,
        buy_volume=_to_float(raw.get("shnu_cntg_qty")),
        sell_volume=_to_float(raw.get("seln_cntg_qty")),
        trend=_classify_trend(cttr),
    )
