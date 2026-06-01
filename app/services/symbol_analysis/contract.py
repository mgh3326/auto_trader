"""SymbolAnalysis read-model 계약 — 타입드 스키마 + 읽기 도구 Protocol (ROB-397).

읽기 시점 라이브 합성 금지. 모든 데이터 카테고리는 FieldBlock 으로 감싸
source/as_of/is_stale 를 카테고리 단위로 강제한다.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID

# freshness.overall 허용값 (freshness.py 가 파생한다).
FRESHNESS_OVERALL: tuple[str, ...] = ("fresh", "partial", "stale", "unavailable")


@dataclass(frozen=True)
class FieldBlock[T]:
    """카테고리 값 + 출처/신선도. value=None 이면 부재."""

    value: T | None
    source: str
    as_of: datetime | None
    is_stale: bool


@dataclass(frozen=True)
class PriceData:
    last: float


@dataclass(frozen=True)
class ValuationData:
    per: float | None = None
    pbr: float | None = None
    roe: float | None = None


@dataclass(frozen=True)
class TechnicalData:
    rsi14: float | None = None
    atr: float | None = None
    sma: float | None = None
    bb_lower: float | None = None
    supports: tuple[float, ...] = ()
    resistances: tuple[float, ...] = ()


@dataclass(frozen=True)
class ConsensusData:
    buy: int | None = None
    hold: int | None = None
    sell: int | None = None
    strong_buy: int | None = None
    total: int | None = None
    target_avg: float | None = None
    target_median: float | None = None
    target_min: float | None = None
    target_max: float | None = None
    upside_pct: float | None = None


@dataclass(frozen=True)
class FlowData:
    foreign_net: float | None = None
    inst_net: float | None = None
    double_buy: bool = False
    double_sell: bool = False
    consec_days: int | None = None


@dataclass(frozen=True)
class PriceLevel:
    price: float
    kind: str
    reasoning: str


@dataclass(frozen=True)
class DerivedBlock:
    action: str
    confidence: str
    buy_zones: tuple[PriceLevel, ...]
    sell_targets: tuple[PriceLevel, ...]
    stop: float | None
    rule_version: str
    insufficient_inputs: tuple[str, ...] = ()


@dataclass(frozen=True)
class Freshness:
    overall: str
    stale_fields: tuple[str, ...] = ()


@dataclass(frozen=True)
class Provenance:
    snapshot_uuid: UUID | None
    primary_source: str
    freshness: Freshness


@dataclass(frozen=True)
class SymbolAnalysis:
    symbol: str
    name: str | None
    market: str
    price: FieldBlock[PriceData]
    valuation: FieldBlock[ValuationData]
    technicals: FieldBlock[TechnicalData]
    consensus: FieldBlock[ConsensusData]
    flow: FieldBlock[FlowData]
    derived: DerivedBlock
    provenance: Provenance


class GetSymbolAnalysis(Protocol):
    """읽기 전용 read-model 조회 계약 (런타임 구현은 후속 collector 이슈).

    캐시/DB 의 최신 머티리얼라이즈 스냅샷을 반환한다. 없으면 마지막
    스냅샷 + is_stale=True. **라이브 합성 금지.**
    """

    async def __call__(
        self, symbols: list[str], session: str | None = None
    ) -> list[SymbolAnalysis]: ...
