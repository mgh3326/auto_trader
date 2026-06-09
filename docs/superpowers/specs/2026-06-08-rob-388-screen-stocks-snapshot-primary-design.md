# ROB-388 — screen_stocks(kr) snapshot-primary 발굴 경로 복구 (설계)

- **Linear**: ROB-388 ([데이터] screen_stocks KR 발굴 경로가 장 개장 시점에 다운)
- **작성일**: 2026-06-08
- **상태**: 설계 확정 (구현 전)
- **짝꿍 이슈**: ROB-446 (write-side 신선도 — Naver 모멘텀 빌더 cron 미커밋)

## 1. 배경 / 문제

`screen_stocks(market="kr")`는 KR 신규매수 후보 발굴의 핵심 경로인데, 장 개장 시점에 두 가지로 다운한다.

1. **KRX 세션 만료**: `krx_session_expired` → `{data_state:"unavailable", retryable:true, ...}`, `results:[]`/`total_count:0`. `retryable:true`인데 즉시 재시도 3회 모두 동일 실패(실제로는 회복 안 됨).
2. **classifier 일시 불가**: 정규장에서 호출 안전 분류기 일시 불가로 `screen_stocks` 실행 자체가 거부됨.

근본 구조 문제: KR 발굴이 **live-only**(tvscreener → legacy KRX API)라 durable fallback이 없다. KRX 세션이 죽으면 무조건 0건으로 끝난다. 폴백 후보였던 `get_momentum_candidates`도 stale(ROB-389/446)이라 우회 불가.

## 2. 목표

`screen_stocks(market="kr")`가 KRX live가 죽어도 **0건으로 끝나지 않도록**, durable read-model `kr_market_ranking`을 **primary**로 소비한다. stale/missing/partial이면 신규매수 후보로 과장하지 말고 `data_state`/`freshness`/`warnings`로 정직하게 표기한다.

## 3. 결정 (locked, 브레인스토밍 Q&A 결과)

- **D1 — snapshot-primary**: `screen_stocks(kr)`는 `kr_market_ranking` read-model을 primary로 읽는다.
- **D2 — read-adapter only**: 스냅샷 신선도(cron 미커밋)는 **ROB-446**(짝꿍)이 담당한다. ROB-388은 read-side 어댑터 + 정직 리포팅 + live fallback만 구현한다. 이 PR만으로는 "정직하게 stale 표기"까지만 보장되고, 실제 fresh 발굴은 ROB-446이 cron을 고쳐야 발현된다.
- **D3 — 사다리**: snapshot-primary. **stale도 정직히 반환**(하드-0 금지). live(tvscreener → legacy KRX)는 스냅샷이 **완전 부재(행 0 = unavailable)** 일 때만 fallback.
- **D4 — 세부 (이 spec에서 확정)**:
  - enrichment는 best-effort + null-safe (위조 금지).
  - 미커버 `sort_by`는 스냅샷을 스킵하고 곧장 live.
  - failure mode #2(classifier 일시 거부)는 **비범위**(일시 인프라, 코드 버그 아님).
  - 수급/`investor_flow`(ROB-398 Slice 3)는 **비범위**(`sort_by` 차원 아님).
  - live KRX 세션 re-auth 수리(option C)는 **미채택**(fallback으로 남김).

## 4. 데이터 소스 (그라운딩)

- **read-model**: `app/models/invest_momentum_event_snapshot.py::InvestMomentumEventSnapshot`. `kr_market_ranking`은 snapshot_kind으로 등록(`app/models/investment_snapshots.py` CHECK).
- **query service**: `app/services/invest_momentum_events/query_service.py::MomentumRankingQueryService.get_ranking(order_type, market="kr", limit, now, ttl_minutes=RANKING_TTL_MINUTES=15)`
  - 반환 `MomentumRanking(market, order_type, trading_date, rows: tuple[RankingRow], freshness: Freshness)`.
  - `RankingRow(rank, symbol, name, price, change_rate, volume, trade_value, market_cap)`.
  - `Freshness(overall: "fresh"|"stale"|"unavailable", latest_snapshot_at, stale_reason)`.
  - **세션만으로 호출 가능**(유저 컨텍스트 불요). MCP 세션 획득은 `app/mcp_server/tooling/screener_snapshot_tool.py` 패턴을 미러.
- **order_type 버킷**: `up`(상승), `quantTop`(거래량), `priceTop`, `searchTop`. 빌더 기본 수집은 `("up","quantTop")` (`app/services/action_report/snapshot_backed/collectors/kr_market_ranking.py`).
- **freshness 규칙(자체 스킴, screener partition_health 아님)**: `trading_date != today(KST)` → `stale(older_trading_date)`; `now - latest_snapshot_at > 15min` → `stale(older_than_ttl)`; 행 0 → `unavailable(no_ranking_rows)`.

> ⚠️ 이 소스는 ROB-446이 "23거래일 stale, cron 미커밋"으로 보고한 바로 그 파이프라인이다. ROB-388은 read-side만 책임지고, 신선도는 ROB-446에 위임한다(D2).

## 5. 아키텍처 / 시임(seam)

신규 헬퍼를 screening 레이어에 추가한다 (예: `app/mcp_server/tooling/screening/kr_ranking_snapshot.py`):

```python
async def load_kr_ranking_snapshot(
    session, *, sort_by, sort_order, filters, limit, now
) -> KrRankingSnapshotResult | None
```

- `sort_by`가 snapshot-eligible이 아니면 **None** 반환(→ 호출자가 live로).
- `get_ranking(order_type=<매핑>, market="kr", limit, now)` 호출 → 행 매핑 + enrichment + freshness.
- 반환 `KrRankingSnapshotResult`: `rows`(screen_stocks 행 모양), `data_state`, `source`, `latest_snapshot_at`, `stale_reason`, `warnings`, `total_count`, `coverage_note`.

**배선 지점**: `app/mcp_server/tooling/screening/kr.py::_screen_kr_with_fallback`의 **맨 앞**(tvscreener/legacy 시도 *전*)에 사다리를 끼운다. `AsyncSession`을 스레딩해야 한다: `screen_stocks_impl`(handler) → `screen_stocks_unified` → `_dispatch_kr_screen` → `_screen_kr_with_fallback` 경로에 `session` 인자 추가, handler에서 세션 생성/주입.

**사다리 의사코드**:

```text
if session and sort_by in SNAPSHOT_ELIGIBLE_SORTS:
    snap = await load_kr_ranking_snapshot(session, sort_by=..., ...)
    if snap is not None and snap.rows:        # fresh OR stale → 둘 다 행 반환
        return build_screen_response(snap.rows, snap.total_count, ...,
                                     meta_fields={data_state, source, latest_snapshot_at,
                                                  stale_reason, warnings})   # 하드-0 아님
    # snap is None(미커버 sort_by) 또는 snap.rows 비어있음(unavailable) → live로 진행
# 기존 경로: tvscreener → legacy KRX(_screen_kr) → KRXSessionExpired → _krx_session_unavailable_response
```

## 6. sort_by ↔ ranking 매핑

| `sort_by` | 매핑 | 비고 |
|---|---|---|
| `change_rate` | order_type `up` | 직접 |
| `volume` | order_type `quantTop` | 직접 |
| `trade_amount` | 수집 행(up∪quantTop)을 `trade_value` desc 재정렬 | **top-movers 한정(버킷당 ~30) — 전체 유니버스 아님** → `coverage_note` + warning |
| `market_cap` | 수집 행을 `market_cap` desc 재정렬 | 동일 한계 → warning |
| `dividend_yield`, `week_change_rate`, `rsi`, `score` | **미커버** | 헬퍼 None 반환 → live(tvscreener가 rsi 등 지원) |

- `sort_order`(asc/desc)를 존중한다.
- 정확한 `sort_by` enum은 `app/mcp_server/tooling/screening/common.py`의 KR 정렬 정의를 기준으로 한다.

## 7. 행 enrichment (best-effort, null-safe)

- **직접 매핑**: `symbol`, `name`, `price`(Decimal→float), `change_rate`, `volume`(int→float), `trade_value`→**`trade_amount`**(필드명 다름), `market_cap`.
- **KRX universe(cached)** 에서: `short_code`/`code`/`sector`/`instrument_type`. 매칭 실패 시 symbol 기반 최선 + null.
- **valuation cached** 에서 best-effort: `per`/`pbr`/`dividend_yield`. 없으면 **null**(기존 screen_stocks 동작과 동일).
- **위조 금지**: 값을 못 구하면 null. 추정/날조 금지.

## 8. 정직 리포팅

- `data_state`: `freshness.overall` 매핑 — `fresh`→`"fresh"`, `stale`→`"stale"`. (`unavailable`은 스냅샷 경로를 끝내고 live로 넘어가므로 스냅샷 응답으로는 나오지 않음.)
- `meta_fields`: `source="kr_market_ranking"`, `latest_snapshot_at`, `stale_reason`, `warnings`(한글).
  - stale 시: `"모멘텀 랭킹 스냅샷이 오래됨(<stale_reason>) — 신규 후보 발굴에 주의"`.
  - 항상: `"모멘텀 랭킹 상위 N종목 기반 — 전체 KRX 스캔이 아님"` (커버리지 경고).
- `total_count`는 실제 반환 행 수. data_state를 동봉하여 신규매수 후보로 과장되지 않게 한다.

## 9. 테스트 (migration 0, read-only)

**단위(신규)**:
- `sort_by` → order_type 매핑 (change_rate→up, volume→quantTop).
- 미커버 `sort_by`(dividend_yield 등) → 헬퍼 None.
- 행 매핑: `trade_value`→`trade_amount`, Decimal→float, int→float.
- `freshness.overall` → `data_state` 매핑.
- **stale 스냅샷도 행 반환**(하드-0 아님) + warnings.
- `unavailable`(행 0) → None → live fallthrough.
- enrichment null-safe (universe/valuation 미스 시 null, 위조 없음).
- `trade_amount`/`market_cap` 재정렬 + top-movers 한계 warning.

**회귀**: `tests/test_mcp_screen_stocks_kr.py`
- 스냅샷 비활성/empty 시 기존 live 경로(tvscreener→legacy) 보존.
- 미커버 sort_by 시 live.
- 세션 주입 경로.

## 10. 비범위 (Out of scope)

- **ROB-446** 모멘텀 cron 신선도(write-side) — 짝꿍 후속. 없으면 ROB-388은 "정직하게 stale"까지만.
- **failure mode #2** (classifier 일시 거부) — 일시 인프라, screen_stocks 코드 버그 아님. snapshot durable이 간접 완화하나 분류기는 추적하지 않음.
- **수급/`investor_flow`** (ROB-398 Slice 3) — `sort_by` 차원이 아님.
- **live KRX 세션 re-auth 수리**(option C) — 미채택. fallback으로 남김.
- **migration 없음**. broker/order/watch/order-intent/scheduler mutation 없음.

## 11. Acceptance criteria

- [ ] `screen_stocks(market="kr", sort_by ∈ {change_rate, volume, trade_amount, market_cap})`가 **fresh** `kr_market_ranking` 스냅샷이 있으면 그것을 반환 (`data_state="fresh"`, `source="kr_market_ranking"`).
- [ ] 스냅샷이 **stale**이면 행을 반환하되 `data_state="stale"` + warnings (**하드-0 아님**).
- [ ] 스냅샷 **완전 부재**(행 0)면 live(tvscreener→legacy KRX)로 fallthrough, 그것도 실패면 기존 `unavailable` 응답.
- [ ] **미커버** sort_by(dividend_yield/week_change_rate/rsi/score)는 스냅샷 스킵하고 live.
- [ ] `trade_amount`/`market_cap`은 top-movers 한계를 warning으로 정직 표기.
- [ ] enrichment는 null-safe, 위조 없음.
- [ ] migration 0, broker/order/watch mutation 0.
- [ ] 단위 + 회귀 테스트 green, ruff clean.

## 12. 안전 경계

read-only 데이터 경로. 신규 쿼리/테이블 없음(기존 `MomentumRankingQueryService` 재사용). broker/order/watch/order-intent/scheduler 무접근. 시세/발굴 데이터만 다룬다.
