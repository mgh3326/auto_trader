# ROB-422 PR2c-2 — 저평가탈출-Toss (undervalued_breakout, valuation-only) 설계

- **이슈**: ROB-422 — PR2c 두 번째 1/2
- **상태**: design (브레인스토밍 승인 — 신고가 근접 5%, valuation-only 별도 loader)
- **날짜**: 2026-06-02
- **선행**: `high_yield_value_screener.py`(valuation 스냅샷 preset loader, 정확한 선례). PR2c-1에서 기존 `oversold_recovery`(RSI)는 `auto_trader_original`로 재분류됨 → 이 spec이 진짜 Toss '저평가 탈출'을 신규 추가.
- **범위**: 단일 valuation-only preset. fundamentals/derive 무관(`fundamentals_screener` 사용 안 함). 성장기대주-Toss = 별도 이슈(qoq=분기 수집 선행).

---

## 1. 목표

Toss '저평가 탈출'(PER 0~10 + PBR 0~1 + 신고가)을 valuation + price 스냅샷만으로 신규 preset `undervalued_breakout`로 구현. fundamentals가 필요 없어 **fundamentals dependency를 붙이지 않으며**, 두 소스 테이블이 이미 적재돼 있어 **operator backfill 없이 라이브로 즉시 동작 가능**(다른 신규 Toss preset과의 핵심 차이).

| preset(id) | Toss 기준 | 소스/조건 |
|---|---|---|
| **저평가 탈출**(`undervalued_breakout`) | PER 0~10 + PBR 0~1 + 신고가 | valuation `per`(0<per≤10)·`pbr`(0<pbr≤1)·`high_52w`(market_valuation_snapshots) + `latest_close`(invest_screener_snapshots) → 신고가 근접 `close >= high_52w * 0.95` |

`presetOrigin=toss_parity`, `parityStatus=full`, KR-only, snapshot-only.

**데이터 가용성(검증됨)**: `market_valuation_snapshots`는 KR naver에서 per/pbr/high_52w 적재(`naver_finance/valuation.py:90-94` "52주 최고"→high_52w 추출 확인). `invest_screener_snapshots`는 OHLCV(latest_close) 적재. 둘 다 기존 동작 → 저평가탈출은 라이브 동작(빈결과는 조건 미충족 또는 high_52w 결측 시).

## 2. loader — `app/services/invest_view_model/undervalued_breakout_screener.py` (신규)

`high_yield_value_screener.py` 미러. **`fundamentals_screener`와 무관**(valuation+price만).

`load_undervalued_breakout_from_snapshots(session, *, market, limit=20, today_market_date=None) -> list[dict] | None`:
1. `market != "kr"` 또는 valuation 파티션 없음 → `None`(caller→dataState=missing).
2. candidate = `market_valuation_snapshots` 최신 파티션 `per > 0 AND per <= 10 AND pbr > 0 AND pbr <= 1`(NULL per/pbr fail-closed 제외). `high_yield_value` SQL 형태 미러 + `pbr`/`high_52w` SELECT.
3. `invest_screener_snapshots` 최신 파티션 OUTER JOIN으로 `latest_close`(+ change_rate/volume 표시용).
4. **신고가 필터**(Python 또는 SQL): `latest_close is not None AND high_52w is not None AND latest_close >= high_52w * _NEAR_HIGH_RATIO`. close/high_52w 결측 → 신고가 판정 불가 → 제외(fail-closed, 날조 금지).
5. common-stock 필터(`_is_kr_toss_common_stock`) + symbol dedup(seen).
6. 정렬: `latest_close / high_52w` desc(신고가 근접도 높은 순), tiebreak `per` asc, `symbol` asc. limit.
7. 각 row: symbol/name/latest_close/per/pbr/high_52w/근접도/`_screener_snapshot_state`(valuation 파티션 fresh|stale)/snapshot_date.

`_NEAR_HIGH_RATIO = Decimal("0.95")` 모듈 상수(조정 가능). freshness: valuation 파티션이 당일=fresh, 아니면 stale(high_yield_value 패턴).

## 3. dispatch — `screener_service.py` (high_yield_value 패턴 미러)

- dispatch elif(line ~1545 high_yield_value 블록 옆): `elif preset_id == "undervalued_breakout":` → `load_undervalued_breakout_from_snapshots(session, market=requested_market, limit=...)`; `_snapshot_check_result = result`; empty 경고 문구.
- snapshot-only 가드(line ~1610 옆): `if preset_id == "undervalued_breakout" and _snapshot_check_result is None: _snapshot_check_result=[]; _snapshot_state_override="missing"; 경고`. **generic fallback 금지**(generic provider엔 신고가 필터 없음).
- primary_source(line ~1712 옆): `elif preset_id == "undervalued_breakout": primary_source = "market_valuation_snapshots"`.
- **fundamentals dependency 없음**(valuation+price만 — high_yield_value와 동일하게 dependency 미부착). `FUNDAMENTALS_PRESET_SPECS` 비포함.
- primary_snapshot_date: high_yield_value처럼 rows[0].snapshot_date fallback(별도 _SnapshotLoadResult 불필요).
- (있으면) preset→metric 맵(`screener_service.py` ~828 `"high_yield_value": "roe"`)에 `"undervalued_breakout"` 엔트리 추가 검토(verify-first).

## 4. 카탈로그 + parity doc

- `SCREENER_PRESETS` 신규 엔트리: id=`undervalued_breakout`, name="저평가 탈출", presetOrigin=`_TOSS`, parityStatus=`_FULL`, market="kr", filterChips=[국내, PER '0~10', PBR '0~1', 신고가 '52주 고가 5% 이내', 데이터 '지연 스냅샷'], metricLabel="신고가 대비". `_KR_ONLY_PRESET_IDS` 추가.
- `docs/invest-screener-toss-parity-matrix.md` #6 저평가 탈출 → `full / undervalued_breakout`(PR2c-1에서 missing으로 둔 행을 채움). missing 카운트 -1, full +1.

## 5. 테스트 (TDD)

1. **loader 신고가 필터**(db_session 통합): per≤10+pbr≤1+close≥high_52w*0.95 → 포함; close < 0.95*high_52w → 제외; close 또는 high_52w NULL → 제외(fail-closed); per>10 또는 pbr>1 → SQL 후보 제외.
2. **정렬**: 근접도(close/high_52w) desc; tiebreak per asc.
3. **dedup/common-stock**: 중복 symbol 1행; ETF/우선주 제외.
4. **None 계약**: valuation 파티션 없음 → None.
5. **dispatch**: preset_id='undervalued_breakout' → loader 호출, snapshot-only(None→missing), primary_source='market_valuation_snapshots', **fundamentals dependency 미부착**, generic 미호출.
6. **catalog**: preset 존재, full/toss_parity, KR-only.
7. **회귀**: high_yield_value + fundamentals presets(PR2a/2b/2c-1) + full-3 무변경.

## 6. 안전·범위 경계

read-only, **migration 0**(기존 테이블 재사용), KR-only, snapshot-only(generic fallback 금지), broker/order/watch mutation 0, NULL fail-closed/날조 금지. fundamentals_screener·derive 무관. 프로덕션 backfill 불필요(소스 기적재)이나 high_52w 결측 종목은 정직 제외.

## 7. 후속 (범위 아님)
- 성장기대주-Toss(qoq) — 분기 fundamentals 수집 선행 별도 이슈.
- 프론트 칩 polish(필요 시).
- ROB-422 PR 생성(PR1+PR2a+PR2b+PR2c-1+PR2c-2).
