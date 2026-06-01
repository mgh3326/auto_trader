# ROB-398 Slice 2 — 모멘텀(Naver 랭킹) read-model query_service + kr_market_ranking collector 설계

- **이슈**: ROB-398 (오케스트레이션 ROB-411 C라인) — Slice 2 of 4
- **상태**: design (브레인스토밍 승인 완료)
- **날짜**: 2026-06-02
- **선행/관련**: ROB-398 Slice 1(뉴스매핑, main 44790460), ROB-388(screen_stocks KR 복구), ROB-389(stale→fresh 라벨 차단), ROB-222(momentum 스냅샷).

---

## 1. 배경 — 기존 인프라와 갭

탐색 결과 "Naver sise 랭킹 적재"는 이미 존재한다:

- `InvestMomentumEventSnapshot`(`app/models/invest_momentum_event_snapshot.py`) — rank/symbol/name/price/change_amount/change_rate/volume/trade_value/market_cap + `order_type ∈ {up(상승), quantTop(거래량), priceTop(가격), searchTop(검색)}`, trading_date/snapshot_at/surface. source="naver_stock", market="kr".
- 적재: `app/services/invest_momentum_events/`(builder/repository/coverage) + `NaverStockClient.fetch_domestic_stock_default(order_type=...)`, 실행은 `app/jobs/invest_momentum_events.py`(commit/scheduler 게이트 default-off).
- repository에 read 메서드 `list_momentum_events(...)`, `list_candidate_signals(...)` 이미 존재.

**갭**: 모멘텀(Naver 랭킹)이 적재·저장되지만 (a) **freshness를 명시하는 read-model query_service가 없고**(repository 직접 read), (b) **번들/리포트 read 경로에 evidence로 연결돼 있지 않다**(`candidate_universe` collector는 `InvestScreenerSnapshot`만 읽음). ROB-388/389는 freshness 정직성·screen_stocks 복구였고, 모멘텀→번들 evidence 배선은 아님.

## 2. 목표·범위

기존 모멘텀 스냅샷 위에 **read-only read-model query_service** + **`kr_market_ranking` 번들 collector**를 추가해 신선한 Naver 랭킹을 evidence로 제공하는 seam을 놓는다.

- **새 테이블/적재 경로 없음** (기존 job 재사용).
- **additive CHECK migration** (snapshot_kind에 `kr_market_ranking` 추가; operator가 `alembic upgrade head` 별도 실행 — CLAUDE.md).
- collector는 **optional/non-blocking** 등록.

**안전 경계** (ROB-411 상속): broker/order/watch/order-intent mutation 없음, production DB backfill/commit ingest 없음, scheduler activation 없음, Toss/Naver는 reference/calibration, stale/부재는 숨기지 않고 노출. (번들 snapshot write는 정상 evidence-freeze 메커니즘.)

## 3. read-model query_service — `app/services/invest_momentum_events/query_service.py` (신규)

기존 repository(`list_momentum_events`/`list_candidate_signals`) 위 thin read-only 래퍼. freshness를 명시(388/389 정직성 계승).

frozen dataclass:

```python
@dataclass(frozen=True)
class RankingRow:
    rank: int
    symbol: str
    name: str | None
    price: float | None
    change_rate: float | None
    volume: int | None
    trade_value: float | None
    market_cap: float | None

@dataclass(frozen=True)
class Freshness:
    overall: str               # "fresh" | "stale" | "unavailable"
    latest_snapshot_at: datetime | None
    stale_reason: str | None

@dataclass(frozen=True)
class MomentumRanking:
    market: str                # "kr"
    order_type: str            # "up" | "quantTop" | ...
    trading_date: date | None
    rows: tuple[RankingRow, ...]
    freshness: Freshness
```

메서드(안): `get_ranking(*, order_type, market="kr", limit=N, now) -> MomentumRanking`.

freshness 규칙 (결정적):
- 0행 → `unavailable`, `stale_reason="no_ranking_rows"`.
- `trading_date != now(KST).date()` → `stale`, `stale_reason="older_trading_date"`.
- 최신 `snapshot_at`이 TTL(기본 15분, 모멘텀 job 주기 `*/10`) 초과 → `stale`, `stale_reason="older_than_ttl"`.
- 그 외 → `fresh`.

(`now`는 주입 가능 — 테스트 결정성.)

## 4. collector — `app/services/action_report/snapshot_backed/collectors/kr_market_ranking.py` (신규)

- `snapshot_kind = "kr_market_ranking"`.
- `async def collect(request) -> list[SnapshotCollectResult]`: query_service로 KR 랭킹을 읽어 `build_result(snapshot_kind=..., market=..., account_scope=..., origin="auto_trader_db", payload=..., as_of=...)`. 조회 예외/0행은 `unavailable_result(...)`로 degrade(크래시 금지).
- payload: `{market, order_types: {<order_type>: {trading_date, freshness, rows[]}}}`.
- 기본 수집 order_type = `up`(상승) + `quantTop`(거래량) (의사결정 핵심). priceTop/searchTop은 query_service 파라미터로 가용(collector 기본 미수집).
- `registry.py` 등록 + `investment_snapshots/policy.py`에 **optional/non-blocking** 항목(soft_ttl=900s, hard_ttl=3600s, required=False, collector_timeout≈15s; screener와 유사).

## 5. migration (additive)

- alembic 신규 리비전: `investment_snapshots.snapshot_kind` CHECK 제약에 `'kr_market_ranking'` 추가(기존 값 전부 보존, additive). down_revision = 현재 단일 head.
- `app/models/investment_snapshots.py`의 CHECK 문자열도 동기 갱신.
- 마이그레이션은 PR에 포함하되 operator가 별도 `alembic upgrade head` 실행.

## 6. candidate_universe 관계 (슬라이스 경계)

- `kr_market_ranking`은 **독립 evidence kind**. 기존 `candidate_universe` collector(InvestScreenerSnapshot 읽기)를 **수정/대체하지 않음** (민감한 리포트 read 경로 미변경).
- "candidate_universe 신선도 복구"는 이 슬라이스에서 **신선한 Naver 랭킹 evidence를 번들에 제공**하는 seam까지. screener-stale 시 fallback 소비/리포트 배선은 **후속**(별도 결정).

## 7. 테스트 (TDD)

1. query_service freshness: trading_date=오늘 + 최근 snapshot_at → `fresh`; trading_date=과거 → `stale`(older_trading_date); snapshot_at TTL 초과 → `stale`(older_than_ttl); 0행 → `unavailable`(no_ranking_rows).
2. query_service 매핑/정렬: repository 결과 → `RankingRow` rank 오름차순.
3. collector: fresh 랭킹 → `build_result` payload에 order_type별 rows + freshness; repository 예외 또는 0행 → `unavailable_result`(degrade, 크래시 없음).
4. snapshot_kind: 모델 CHECK 문자열에 `kr_market_ranking` 포함 + 기존 kind 전부 보존(회귀).
5. registry/policy: `kr_market_ranking` collector가 optional/non-blocking으로 등록됨.
- repository는 fake/in-memory(또는 기존 테스트 패턴)로 DB-free 단위테스트.

## 8. 비목표 (YAGNI)

- 새 랭킹 테이블(모멘텀과 중복), 새 적재 경로/CLI(기존 job 재사용), scheduler activation.
- `candidate_universe` collector 수정 / screener-stale fallback 소비 / 리포트 배선 (후속).
- priceTop/searchTop 기본 수집(파라미터로만 가용).
- 투자자 매매동향(Slice 3) · Toss screener(Slice 4).
