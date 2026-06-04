# ROB-430 — KR Toss 조건/계산식 parity (트랙 A): 연속상승세 OHLCV window + 저평가탈출 신고가 재정의

> **상태:** spec (구현 전). 코드 grounding + 라이브 probe + Toss 재캡처(2026-06-04, 국내) 완료.
> **분류:** Bug (Toss 조건 mismatch) + hardening. ROB-429(데이터-볼륨) 후속 = **조건-정확도** gap.
> **구조:** 1 이슈(ROB-430) + 독립 PR 2개. PR-①(invest_screener_snapshots OHLCV, migration 0) / PR-②(tvscreener, additive 컬럼 1).
> Linear: https://linear.app/mgh3326/issue/ROB-430

## Current State (verified 2026-06-04)

| 프리셋 (국내) | Toss | Toss 조건 (재캡처) | auto_trader | 원인 |
|---|---|---|---|---|
| **연속 상승세** | **2개** | 주가등락률 1주일전 ≥0% + **연속상승 5일 이상** | **0건** (max streak=2) | OHLCV window ~3봉 → streak≥5 불가 |
| **저평가 탈출** | **77개** | PER 0~10 + PBR 0~1 + **신고가(52주, 최근 20일 이내 경신)** | **0건** | `price/week_high_52≥0.95`(근접도) 의미 mismatch |

### 근본 원인 (grounded)
- **① OHLCV window**: `builder.py` `derive_metrics()` streak 계산식은 정상(10봉→9, 테스트 확인). builder는 `_fetch_ohlcv_for_indicators(count=_LOOKBACK=10)`로 10봉 요청 + `closes[-10:]` 저장 → **코드 truncation 아님**. equity OHLCV 소스(`kr_candles_1d`)가 빌드 시 <6세션만 반환한 것이 유력. window<6이면 5일 연속상승 영구 불가.
- **② 신고가**: `kr_fundamentals_tv_screener.py` `_high_52w_proximity=price/week_high_52`, `UNDERVALUED_BREAKOUT_SPEC.min_high_52w_proximity=0.95`. 라이브 probe: full 4250 → PER0~10:680 → +PBR0~1:579 → +proximity≥0.95:**0** (deep-value 579 proximity median 0.67/max 0.94, week_high_52 정상 적재). deep-value는 52주고가 근처가 아니지만 **최근 신고가 경신은 함**. Toss 신고가 팝업 = 윈도우(4주/12주/**52주**) + recency(**20일** 직접입력), 저평가탈출 기본=52주/20일. tvscreener **`PRICE_52_WEEK_HIGH_DATE`**(+`HIGH_1M_DATE`/`HIGH_3M_DATE`/`HIGH_6M_DATE`) 존재 확인.

## Decisions (locked)
- **D1** 1 이슈(ROB-430) + 독립 PR 2개(①/②).
- **D2** ② 신고가 = 52주/20일 **하드코딩** + 0.95 proximity **교체** + additive `week_high_52_date`(`PRICE_52_WEEK_HIGH_DATE`).
- **D3** ① = 코드 **진단+보장**(window<6 fail-closed guard/경고 + 소스 히스토리 확보) + operator 재빌드 검증.

## PR-① 연속 상승세 OHLCV window (migration 0)
- `_fetch_ohlcv_for_indicators` equity 경로 + `kr_candles_1d` 히스토리 진단: KR 종목당 반환 세션이 왜 <6인지(sparse 히스토리 / 경로 cap).
- consecutive_up_days 신뢰엔 closes_window ≥6세션 보장(streak≥5 가능). 못 하면 **신뢰불가 마킹/경고**(가짜 streak 금지, fail-closed); 빌더가 충분 히스토리 fetch하도록 교정.
- operator 재빌드 후 Toss 국내 ~2개 재현 검증.

## PR-② 저평가 탈출 신고가 재정의 (additive migration 1)
- `InvestKrFundamentalsSnapshot.week_high_52_date: Date|None` additive(alembic single-head); provider가 `PRICE_52_WEEK_HIGH_DATE` 적재.
- `UNDERVALUED_BREAKOUT_SPEC`: `min_high_52w_proximity=0.95` **제거**; 신고가 = `week_high_52_date`가 partition_date 기준 **≤20일**. `_passes_thresholds` date-recency 체크(null/미래 fail-closed). 상수 `_NEW_HIGH_RECENCY_DAYS=20`.
- 라이브 PER0~10+PBR0~1+(52w-high-date≤20일) → Toss ~77 근접. PR #1116 경로 유지.

## Acceptance Criteria
1. ①: 재빌드 후 consecutive_gainers 국내 count가 Toss(~2) 근접·0 아님; window<6 행은 가짜 streak 없이 정직 제외/경고.
2. ①: `_fetch_ohlcv_for_indicators` equity가 KR에 ≥6세션 반환을 확인(또는 부족 시 신뢰불가 마킹).
3. ②: `week_high_52_date`가 `PRICE_52_WEEK_HIGH_DATE`로 적재(additive, single-head).
4. ②: undervalued_breakout = PER0~10+PBR0~1+(52w-high-date≤20일); proximity 제거; 매칭 Toss(~77) 근접.
5. 둘 다 데이터 없을 때 fail-closed + honest freshness.
6. broker/order/watch mutation 0; production --commit/backfill/scheduler operator 승인; Toss benchmark only(라이브러리, kr.tradingview 크롤링 금지).

## Testing Plan
| Layer | What | Count |
|---|---|---|
| Unit | ① derive_metrics window≥6 streak≥5, window<6 신뢰불가(가짜 금지) | +3 |
| Unit | ① OHLCV fetch ≥6 반환/부족 처리 | +2 |
| Unit | ② week_high_52_date recency ≤20 통과, >20/null/미래 제외 | +3 |
| Unit | ② undervalued_breakout predicate + proximity 제거 회귀 | +2 |
| Integration | ② build_screener_results 매칭 행 + totalCount | +1 |

## Rollback
①: 코드 revert(파괴적 변경 없음). ②: additive 컬럼 nullable → revert PR 무해. 두 PR 독립.

## Files Reference
| File | Change |
|---|---|
| `app/services/invest_screener_snapshots/builder.py` | ① window 보장 + window<6 신뢰불가 마킹 |
| `app/mcp_server/tooling/market_data_indicators.py` | ① equity 경로 세션 수 진단/보장 |
| `app/models/invest_kr_fundamentals_snapshot.py` | ② week_high_52_date 컬럼 |
| `alembic/versions/…` | ② additive 마이그레이션(single-head) |
| `app/services/invest_kr_fundamentals_snapshots/provider.py`+`builder.py` | ② PRICE_52_WEEK_HIGH_DATE 적재 |
| `app/services/invest_view_model/fundamentals_screener.py` | ② SPEC proximity 제거→new-high-recency |
| `app/services/invest_view_model/kr_fundamentals_tv_screener.py` | ② _passes_thresholds date-recency |

## Out of Scope (follow-up)
- ④ 순이익 연속증가(tvscreener 미제공 → DART 보강/honest warning).
- 쌍끌이 매수 investor_flow stale(ROB-205 refresh + 경고 노출 소 PR) = 트랙 B.
- exact 벤더 정확도, 신고가 윈도우 사용자 선택(4주/12주) UI, totalCount 프론트 렌더.

## Related
ROB-429(#1118/#1119) · ROB-428(#1115/#1116) · ROB-280/281/426 · ROB-205(investor_flow).

## Appendix — probe/재캡처 재현
- ② probe: `TvScreenerKrFundamentalsProvider().fetch_rows(limit=None)`=4250 → per0~10:680 → +pbr0~1:579 → +proximity≥0.95:**0** (median 0.67/max 0.94). tvscreener fields: `PRICE_52_WEEK_HIGH_DATE`/`HIGH_1M_DATE`/`HIGH_3M_DATE` 존재.
- ① grounding: `builder.py` `count=_LOOKBACK=10`, `closes[-10:]`; `_fetch_ohlcv_for_indicators(count=250 default)` equity 경로.
- Toss 재캡처: 연속상승세 국내 2개("연속상승 5일"); 저평가탈출 국내 77개(PER0~10/PBR0~1/신고가=52주·20일).
