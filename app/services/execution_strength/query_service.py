"""ROB-462/ROB-485: KR 주식 체결강도 (execution strength) read model.

체결강도 = 매수체결량 / 매도체결량 × 100 (당일 누적). KIS REST 에서는
FHKST01010300 (주식현재가 체결, inquire-ccnl) tick row 의 ``tday_rltv`` 가
공식 체결강도다 — broker 레이어가 최신 row 를 골라 전달하고, 여기서는
파싱/분류만 한다 (재계산 금지). FHKST01010100 (주식현재가 시세) 에는
체결강도 필드가 없다 (2026-06-10 라이브 검증, ROB-485).

매수/매도 체결량 분리(buy_volume/sell_volume)는 KIS REST 미제공 — WebSocket
H0STCNT0 전용이므로 항상 None (cntg_vol/prdy_vrss_sign 으로 추정 조작 금지;
WS 소스 연동은 follow-up). Pure transform; broker fetch + freshness tagging
은 MCP tool 이 담당한다.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ExecutionStrengthData:
    symbol: str
    as_of: str | None
    tick_time: str | None
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


def _to_tick_time(value: Any) -> str | None:
    """KIS ``stck_cntg_hour`` (HHMMSS KST) 문자열을 그대로 보존한다."""
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _classify_trend(strength: float | None) -> str | None:
    """체결강도 100 = 매수/매도 균형. >100 매수우위, <100 매도우위."""
    if strength is None:
        return None
    if strength > 100.0:
        return "buy_dominant"
    if strength < 100.0:
        return "sell_dominant"
    return "neutral"


def compute_execution_strength(
    raw: dict[str, Any], *, symbol: str, as_of: str | None
) -> ExecutionStrengthData:
    """Build the read model from a KIS FHKST01010300 tick row dict.

    ``raw`` 는 broker 가 고른 최신 tick row 의 실 키
    (``tday_rltv``/``stck_cntg_hour``/``stck_prpr``) + ``acml_vol`` 을 담은
    dict 다. ``tday_rltv`` 가 공식 체결강도 (당일 누적). 결측 필드는 None
    유지 — 0 으로 날조하지 않는다. buy/sell volume 은 KIS REST 미제공이라
    항상 None.
    """
    strength = _to_float(raw.get("tday_rltv"))
    return ExecutionStrengthData(
        symbol=symbol,
        as_of=as_of,
        tick_time=_to_tick_time(raw.get("stck_cntg_hour")),
        execution_strength_pct=strength,
        # KIS REST 에 per-side 체결량 없음 (WebSocket H0STCNT0 전용).
        buy_volume=None,
        sell_volume=None,
        trend=_classify_trend(strength),
    )
