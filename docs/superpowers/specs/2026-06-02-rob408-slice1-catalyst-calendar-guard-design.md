# ROB-408 Slice 1 — catalyst 캘린더 foundation + upcoming-catalyst 가드 설계

- **이슈**: ROB-408 (오케스트레이션 ROB-411 C라인 마지막 묶음) — Slice 1
- **상태**: design (브레인스토밍 승인 완료)
- **날짜**: 2026-06-02
- **선행/관련**: ROB-128(market_events foundation), ROB-398(별칭/news_symbol_link — 후속 news-NER 소스용), ROB-397(SymbolAnalysis 근거).

---

## 1. 배경 — 기존 인프라와 갭

탐색 결과 이벤트 캘린더 인프라가 이미 존재한다 (Slice 패턴 반복):

- `market_events`(ROB-128): `MarketEvent`(`category`[확장가능 frozenset], `market`[us/kr/crypto/global], `symbol`[단일 nullable], `event_date`, `status`, `source`, `raw_payload_json`), `MarketEventValue`, `MarketEventIngestionPartition`. 서비스: `repository`/`ingestion`/`query_service`/`normalizers`/`taxonomy`. CLI `scripts/ingest_market_events.py`. **`MarketEventsSnapshotCollector`(snapshot_kind="market") 이미 등록 + contract 엔트리**(`market_events_db`).
- 현재 taxonomy: `earnings/economic/disclosure/crypto_exchange_notice/crypto_protocol/tokenomics/regulatory`. `get_earnings_calendar`(실적)만 존재.

**갭**: 컨퍼런스(GTC)·기업행사(CEO 방한/사옥방문)·신제품·정책/규제·락업·인덱스 리밸런싱 등 **비실적 촉매가 taxonomy에 없고**, "보유 종목에 임박 호재가 있으면 트림 경고"하는 **upcoming-catalyst 가드 신호가 없다**. **진짜 제약 = catalyst 소스 부재**(깨끗한 피드 없음 — 뉴스-NER/수동/유료 API만).

## 2. 목표·범위

기존 `market_events` 위에 **소스-불가지니스틱 foundation + 가드**를 만든다: taxonomy 확장 + catalyst read-model query_service + 순수 upcoming-catalyst 가드 신호.

- **새 테이블/새 snapshot_kind/migration 없음**: taxonomy는 code-only frozenset; catalyst 이벤트는 `market_events` 행이라 기존 `MarketEventsSnapshotCollector`(snapshot_kind="market")로 자동 노출.
- **새 스크레이퍼/소스 없음**: 실제 적재는 기존 `market_events` ingest 경로 재사용(후속 소스별 슬라이스).
- trim/buy classifier 배선은 **Slice 2**.

**안전 경계** (ROB-411 상속): broker/order/watch/order-intent mutation 없음, production DB backfill/commit ingest 없음, scheduler activation 없음, stale/부재 숨김 없음.

## 3. taxonomy 확장 — `app/services/market_events/taxonomy.py`

`CATEGORIES` frozenset에 catalyst 카테고리 additive 추가(검증 함수 그대로): `conference`, `corporate_event`, `product_launch`, `policy_regulation`, `lockup_expiry`, `index_rebalance`. 코드만, migration 0, 기존 카테고리/검증 무회귀.

## 4. impact 극성 — 신규 `app/services/market_events/catalyst/polarity.py`

```python
CATEGORY_POLARITY: dict[str, str] = {
    "conference": "positive",
    "product_launch": "positive",
    "index_rebalance": "positive",
    "policy_regulation": "negative",
    "lockup_expiry": "negative",
    "earnings": "neutral",
    "corporate_event": "neutral",
}

def resolve_polarity(category: str, raw_payload: dict | None) -> str:
    """raw_payload_json['impact_hint'] ∈ {positive,negative,neutral} 우선,
    없으면 CATEGORY_POLARITY, 미지정 category면 'neutral'."""
```

순수 함수, 결정적. 적재 소스가 raw_payload에 impact_hint를 넣으면 그것이 우선.

## 5. catalyst read-model query_service — `app/services/market_events/catalyst_query_service.py` (신규)

기존 `MarketEventsQueryService`/`MarketEventsRepository` read 위 thin 래퍼.

frozen dataclass:

```python
@dataclass(frozen=True)
class CatalystEvent:
    symbol: str | None
    category: str
    title: str | None
    event_date: date
    days_until: int
    polarity: str          # positive | negative | neutral
    source: str | None
    confidence: float | None

@dataclass(frozen=True)
class Freshness:
    overall: str           # "fresh" | "unavailable"
    stale_reason: str | None

@dataclass(frozen=True)
class UpcomingCatalysts:
    market: str
    within_days: int
    rows: tuple[CatalystEvent, ...]
    freshness: Freshness
```

메서드: `get_upcoming_catalysts(*, symbols=None, market="kr", within_days=7, now) -> UpcomingCatalysts`.
- catalyst 카테고리(§3) AND `event_date`가 `[now.date(), now.date()+within_days]` 범위인 행만.
- `symbols` 주어지면 해당 종목, 없으면 시장 전체.
- `days_until = (event_date - now.date()).days`. `polarity = resolve_polarity(category, raw_payload)`.
- 0행 → `freshness.overall="unavailable"`(`no_upcoming_catalysts`); 그 외 `fresh`.
- 종목 매핑은 `market_events.symbol`(단일) 사용. **ROB-398 alias/news_symbol_link는 후속 news-NER 소스 단계** (catalyst 텍스트→종목)에서 적용; 본 슬라이스는 이미 매핑된 행을 읽기만.

## 6. upcoming-catalyst 가드 (순수) — `app/services/market_events/catalyst/guard.py`

```python
@dataclass(frozen=True)
class CatalystGuard:
    flag: str | None           # "upcoming_positive_catalyst" | "upcoming_negative_catalyst" | None
    nearest_days: int | None
    positive: tuple[CatalystEvent, ...]
    negative: tuple[CatalystEvent, ...]
    reason: str | None

def evaluate_catalyst_guard(
    events: Sequence[CatalystEvent], *, side: str, within_days: int
) -> CatalystGuard: ...
```

- `side ∈ {"trim","sell"}`: `positive` 촉매가 `within_days` 내면 `flag="upcoming_positive_catalyst"`, `reason="이벤트 후 재평가 권고"`.
- `side ∈ {"buy","add"}`: `negative` 촉매가 `within_days` 내면 `flag="upcoming_negative_catalyst"`.
- 해당 없으면 `flag=None`. `nearest_days`=가장 가까운 관련 촉매까지 일수. 순수·결정적(정렬 안정).

## 7. 노출 (슬라이스 경계)

- catalyst 이벤트 = `market_events` 행 → **기존 `market` snapshot collector로 자동 evidence 노출**(snapshot/registry/policy/contract 변경 0).
- 가드는 consumer(Slice 2 action_classifier)가 호출하는 read 헬퍼. 본 슬라이스는 query_service + guard 제공까지.

## 8. 테스트 (TDD)

1. taxonomy: 신규 catalyst 카테고리 6종 검증 통과 + 기존 카테고리 보존(회귀).
2. polarity: 각 category 매핑 + raw_payload impact_hint override + 미지정 category→neutral.
3. query_service: within_days 범위 필터(경계 포함/제외) + days_until 계산 + polarity 부착 + symbols 필터 + 0건 unavailable.
4. guard: trim+positive D-N→flag; buy+negative D-N→flag; trim+negative-only→flag None; 범위 밖→None; nearest_days; 결정성(동일 입력 동일 출력).
5. repository/query는 fake/in-memory로 DB-free.

## 9. 비목표 (YAGNI)

- trim/buy classifier 배선(`upcoming_catalyst`를 action_report 카드에) — **Slice 2**.
- 실제 catalyst 소스 ingestion(news-NER + ROB-398 alias / IR 공시 / 거래소 일정) — 후속 소스별 슬라이스(기존 market_events ingest 경로).
- 새 `catalyst_event` 테이블 / 새 snapshot_kind / migration.
- 다중 종목 이벤트(index_rebalance 다종목) — 현재 단일 symbol/row, 후속.
