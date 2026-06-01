# ROB-414 — prepare_bundle symbol 스테이지 US 후보 해소 폴백

- **이슈**: ROB-414 (E라인 E1, read-only report surface)
- **유형**: Bug fix
- **작성일**: 2026-06-02
- **연관**: ROB-421(오케스트레이션), ROB-411(C라인 — symbol collector 표면 일부 공유), ROB-415(등급 정책 후속), ROB-374(fundamentals/sentiment 0커버 맥락)

## 증상 / 근본 원인

`investment_report_prepare_bundle(market="us", account_scope="kis_live", symbols=[보유20 + HCA/REGN/BSX/AZO])`
호출 시 미보유 신규매수 후보 4종이 symbol 스테이지에서 `unresolved_symbols`로 드롭됨
(`status=partial`, `result_count=21`, `missing_data: ["unresolved_symbols: AZO, BSX, HCA, REGN"]`).

근본 원인: `SymbolSnapshotCollector._resolve_symbol_payloads`는 crypto를 제외한 모든 market을
`stock_info`만으로 해소한다(`collectors/symbol.py:159`). `stock_info`는 분석 이력이 있는 종목만
row를 가지므로, 보유/분석된 종목은 해소되지만 **미보유 신규 후보는 row가 없어 전건 드롭**된다.
US 마스터 소스인 `us_symbol_universe`(전체 등록 종목)는 조회하지 않는다.

## 기대 동작 (이슈 Acceptance)

`symbols[]`에 넘긴 티커는 **보유 여부와 무관하게 해소**되거나, 해소 실패 시 **티커별 reason_code**를 반환한다.

## 설계

### 변경 표면

- `app/services/action_report/snapshot_backed/collectors/symbol.py` — `SymbolSnapshotCollector`
- `app/services/investment_stages/stages/symbol.py` — `SymbolStage` (렌더링)

US-scoped 변경. migration 0. 새 HTTP surface 없음. broker/order/watch/order-intent mutation 없음. read-only.
KR/crypto 해소 분기는 무변경(ROB-411 C라인 충돌 회피).

### Unit 1 — US 해소 폴백 (collector)

`_resolve_symbol_payloads(market, symbols)`에서 `market == "us"` 분기 추가:

1. 기존대로 `stock_info`를 `symbol IN (symbols)`로 조회 → 보유/분석이력 종목은 풍부한 메타
   (`sector`/`market_cap` 포함)로 해소. (stock_info 우선)
2. stock_info에서 해소되지 **않은** 잔여 티커만 `us_symbol_universe`(`is_active IS TRUE`) 조회.
   해소되면 payload 형태:
   - `symbol = row.symbol`
   - `name = row.name_kr or row.name_en or row.symbol`
   - `instrument_type = "equity_us"`
   - `exchange = row.exchange`
   - `sector = None`
   - `market_cap = None`
   - `is_active = row.is_active`
   (`search_us_symbols`의 매핑과 일치)
3. 두 소스 병합. 같은 심볼이 양쪽에 있으면 stock_info row가 우선(중복 미생성).

KR/crypto는 기존 단일-소스 경로 그대로(2차 쿼리 없음).

### Unit 2 — 티커별 reason_code (collector)

stock_info + us_symbol_universe(active) 모두에서 해소 실패한 US 티커는 per-ticker reason_code를 산출:

| 조건 | reason_code |
|---|---|
| us_symbol_universe에 row 존재하나 `is_active=False` | `inactive` |
| us_symbol_universe 전체 0행 | `universe_empty` |
| us_symbol_universe 조회 중 예외 | `universe_lookup_error` (fail-open, 날조 금지) |
| 그 외 (어디에도 없음) | `not_registered` |

US partial 스냅샷 payload에 구조화 필드 추가:

```json
{
  "missing_symbols": ["AZO", "BSX"],              // back-compat bulk 리스트(유지)
  "unresolved": [
    {"symbol": "AZO", "reason_code": "not_registered"},
    {"symbol": "BSX", "reason_code": "inactive"}
  ]
}
```

- `missing_symbols`는 기존 소비자(스테이지 fallback) 호환을 위해 유지.
- `unresolved`는 US partial 결과에만 첨부. KR/crypto partial 결과는 기존 `missing_symbols`-only 유지.
- universe 조회는 stock_info가 이미 성공한 뒤이므로, universe 실패가 전체를 크래시시키지 않게
  감싸고(per-ticker `universe_lookup_error`), 비어있으면 `universe_empty`(sync 필요 신호).

### Unit 3 — 스테이지 렌더링 (`SymbolStage`)

`SymbolStage.run`에서 symbol 스냅샷 payload에 `unresolved`(list of `{symbol, reason_code}`)가 있으면
`missing_data`를 per-ticker reason 포함 형태로 렌더:

```
unresolved_symbols: AZO (not_registered), BSX (inactive)
```

`unresolved`가 없으면 기존 `missing_symbols` bulk 렌더 유지(KR/crypto 무변경):

```
unresolved_symbols: AZO, BSX, HCA, REGN
```

verdict는 기존대로 항상 `NEUTRAL`(메타데이터에서 방향성 날조 금지).

### 에러 처리

- stock_info 1차 쿼리 실패: 기존 fail-open(`unavailable_result`) 유지.
- us_symbol_universe 2차 쿼리 실패: stock_info 해소분은 보존하고, 잔여 티커를
  `universe_lookup_error` reason으로 `unresolved`에 기록(전체 크래시 금지).

## 테스트 (TDD)

Collector (`tests/services/action_report/snapshot_backed/test_collectors.py`):

1. US: stock_info miss → us_symbol_universe hit → 후보 해소(payload `instrument_type="equity_us"`).
2. US: stock_info hit(보유) + universe hit(후보) 혼합 → 보유는 stock_info 메타(sector/market_cap) 우선, 중복 없음.
3. US: 둘 다 miss → `unresolved`에 `{symbol, reason_code:"not_registered"}`.
4. US: universe row `is_active=False` → reason_code `inactive`.
5. US: universe 0행 → reason_code `universe_empty`.
6. US: universe 조회 예외 → reason_code `universe_lookup_error`, stock_info 해소분 보존.
7. KR/crypto: 2차 쿼리 호출 안 함(기존 동작, `missing_symbols`-only).

Stage (`tests/.../test_symbol_stage*.py` 또는 해당 위치):

8. payload에 `unresolved` 있으면 `missing_data`에 per-ticker reason 렌더.
9. payload에 `missing_symbols`만 있으면 기존 bulk 렌더(back-compat).

## 안전 경계 / Non-goals

- US-only. KR/crypto 해소 분기 무변경.
- migration 0. 새 broker/HTTP surface 없음. broker/order/watch/order-intent mutation 없음.
- 등급 정책(`report_quality_summary.grade`) 변경 없음 — partial/reason_code 의미를 등급에 반영하는 것은 ROB-415(후속).
- us_symbol_universe sync(데이터 적재)는 operator 작업 — 이 PR 범위 아님. 비어있으면 `universe_empty`로 정직히 신호.
