# ROB-428 — KR Toss 11-preset 스크리너 result parity (tvscreener-backed snapshot)

> **상태:** spec (구현 전). 검토·probe·결정 grounding 완료(Linear ROB-428 코멘트 + 메모리).
> **기준일:** 코드 2026-06-04 `origin/main` d4ad9c3f. Toss 캡처 2026-06-04(operator 브라우저, benchmark only). tvscreener probe 2026-06-04.
> **목표 기준:** exact 종목 일치(불가) 아님 → **comparable coverage + 정직한 불일치 노출**.

## Context

`/invest/screener` 국내 탭 fundamentals 프리셋이 Toss 대비 거의 빈 결과 + 값 `-`. 코드 parity는 11/11 full(ROB-359/422/425)이나 **결과(종목 집합·표시값)가 Toss와 크게 다름**: 아직 저렴한 가치주 549 vs 10, 저평가 성장주 187 vs 3.

당초 원인 진단은 "fundamentals coverage(DART backfill 미실행)"였으나, **probe로 더 빠른 경로를 확인**: tvscreener 라이브러리(공개 scanner API)가 KR 펀더멘털을 직접·고커버리지로 제공한다. 따라서 screener **디스플레이**(PIT 불요)는 tvscreener-backed 스냅샷으로 즉시 parity에 접근하고, DART는 리포트/PIT 전용으로 둔다.

> **거버넌스 메모(중요):** "Toss/Naver/tvscreener 격상 금지"는 `kr.tradingview.com/screener/` **페이지를 Chrome remote-debug로 크롤링**하지 말라는 것. **tvscreener 라이브러리(scanner API) 사용은 허용**(crypto 스냅샷이 이미 `source="tvscreener_upbit"`로 사용 중). 리포트-of-record 권위 소스는 DART 유지.

## tvscreener KR probe 결과 (2026-06-04, 상위 300 KR, scanner API)

| Toss 필요 지표 | tvscreener StockField | KR 채움률 |
|---|---|---|
| ROE | `RETURN_ON_EQUITY_TTM` | 100% |
| 배당성향 | `DIVIDEND_PAYOUT_RATIO_TTM` | 100% |
| 매출총이익률 | `GROSS_MARGIN_TTM` | 93% |
| 매출 증감률 | `REVENUE_ANNUAL_YOY_GROWTH` | 100% |
| 순이익/EPS 증감률 | `NET_INCOME_ANNUAL_YOY_GROWTH` / `EPS_DILUTED_ANNUAL_YOY_GROWTH` | 92% |
| QoQ(성장기대주) | `EPS_DILUTED_QUARTERLY_QOQ_GROWTH` | 81% |
| 배당 연속지급(꾸준한배당주) | `CONTINUOUS_DIVIDEND_PAYOUT` | 100% (삼성=38) |
| 배당 연속성장(미래배당왕) | `CONTINUOUS_DIVIDEND_GROWTH` | 100% |
| 신고가(저평가탈출) | `WEEK_HIGH_52` | 100% |
| 카테고리 | `SECTOR` / `INDUSTRY`(세분류) | 100% |
| PER/PBR/배당수익률/시총/RSI | (있음) | 78–100% |

**미충족(정직):** ① `순이익 연속증가 연수` — tvscreener엔 연속 *배당*만 있고 연속 *순이익* 필드 없음 → derive 필요(DART) 또는 honest 생략/근사. ② `3년 평균` 정확 정의 — TV는 YoY(1년)+CAGR_5Y → "comparable"로 수용. ③ **PIT(filing_date/as-of)** — TV 현재값만 → 리포트/백테스트(ROB-330)는 DART 유지.

## Decisions (locked)
- **D1** comparable + honest (exact 종목 일치 비목표).
- **D2** PR-A(tvscreener KR 스냅샷) + PR-B(read-path) + PR-C(parity harness). operator DART backfill은 **count 레버에서 강등**(리포트 트랙).
- **D3** `oversold_recovery`(RSI) = tvscreener RSI로 스냅샷 백킹 가능(probe 100%) → 정직 노출.
- **D4** worktree `auto_trader.rob-428` 문서 + Linear ROB-428 코멘트.
- **D5/D6 갱신** 카테고리 = tvscreener `INDUSTRY`(+`SECTOR`)를 **PR-A 스냅샷 컬럼**으로 보유(별도 `kr_symbol_universe.sector` 컬럼/별도 sync 불요 → D6 superseded).
- **D7** tvscreener-backed로 아키텍처 개정. DART는 리포트/PIT/순이익연속 보강.

## Proposed Change

### PR-A — tvscreener-backed KR 스크리너 스냅샷 (crypto 패턴 미러, additive migration)

1. **모델/마이그레이션**: 신규 `invest_kr_fundamentals_snapshot`(또는 동등) — `invest_crypto_screener_snapshot` 패턴. 컬럼: `snapshot_date, symbol, name, price, change_rate, change_amount, volume, market_cap, per, pbr, dividend_yield, roe_ttm, payout_ratio_ttm, gross_margin_ttm, revenue_yoy, eps_yoy, eps_qoq, net_income_yoy, net_income_cagr_5y, continuous_dividend_payout, continuous_dividend_growth, week_high_52, rsi14, sector, industry, source('tvscreener_kr'), computed_at`. additive(non-destructive), operator `alembic upgrade`.
2. **빌더/잡**: `app/services/invest_kr_fundamentals_snapshots/builder.py` + `app/jobs/...` — `TvScreenerService.query_stock_screener(markets=[Market.KOREA], columns=[...probe-validated fields...])` → 정규화 → upsert(`computed_at=func.now()`, crypto `repository.py:61` 패턴). dry-run 기본, `--commit` write, 분포/행수 가드(`snapshot_commit_guard`) 재사용. tvscreener 필드명은 `_get_tvscreener_attr` 버전-안전 조회(`screening/kr.py` 패턴) + `tvscreener_capabilities.py`에 신규 capability 추가.
3. **freshness**: crypto와 동일 `computed_at` 나이 기반(라이브 소스 → 저장 스냅샷). healthy-partition/`resolve_healthy_partition` 재사용.

### PR-B — read-path: fundamentals/valuation 프리셋이 tvscreener 스냅샷 소비 (migration 0)

- `fundamentals_screener.py` / `high_yield_value_screener.py` / `undervalued_breakout_screener.py` 및 catalog 로더가 **DISPLAY 경로에서 신규 tvscreener KR 스냅샷**을 primary로 읽도록 배선. row에 price/change/volume/category(sector·industry)/market_cap/per/pbr/roe/payout/margin/growth/dividend-streak 채움 → `-` 및 count gap 동시 해소.
- 프리셋 매핑: 고수익저평가(ROE+PER)·돈잘버는회사(ROE+gross_margin)·저평가탈출(PER/PBR/52wH)·아직저렴한가치주(PER/PBR+eps_yoy)·저평가성장주(rev/eps growth)·성장기대주(eps 3y+QoQ)·고수익저평가/안정성장주(ROE+growth)·꾸준한배당주(yield+payout+`continuous_dividend_payout`)·미래배당왕(yield+`continuous_dividend_growth`+payout). 연속상승세는 OHLCV(invest_screener_snapshots) 유지.
- **순이익 연속증가 3년**(steady_dividend/stable_growth/future_dividend_king): tvscreener 미제공 → 해당 sub-condition은 honest 생략 또는 `net_income_yoy>0` 근사 + warning, 또는 DART 보강(후속). fail-closed/위조 금지.
- `oversold_recovery`: tvscreener RSI로 스냅샷 백킹(더는 live-only 아님).
- 미적재 심볼/필드는 `-`+warning, fail-closed 유지.

### PR-C — parity validation harness (dry-run, migration 0)

`scripts/validate_screener_toss_parity.py` — operator 캡처 Toss JSON(자동 scraping 금지) vs auto_trader, preset별 count/top-N overlap/rank diff artifact. acceptance smoke 11 preset.

### reports/PIT track (별도, DART 유지)

DART `financial_fundamentals_snapshots`(ROB-422/425)는 **리포트/백테스트 PIT(ROB-330)** + 순이익 연속증가 보강용으로 유지. operator DART backfill은 리포트 정확도용이며 **screener 디스플레이의 블로커 아님**.

## Acceptance Criteria

1. tvscreener KR 스냅샷이 probe-validated 필드를 dry-run/commit으로 적재(`computed_at` freshness).
2. 국내 탭 fundamentals/valuation 프리셋 결과 row의 현재가/등락률/거래량/카테고리/시총 + 프리셋 지표가 `-` 없이 채워짐(스냅샷 존재 시); 누락은 `-`+warning, fail-closed(위조 0).
3. count가 Toss 수준에 **비교 가능**(harness로 측정); operator DART backfill 불요.
4. `순이익 연속증가` 미충족 프리셋은 honest 처리(생략/근사+warning, 위조 금지).
5. `oversold_recovery` RSI 스냅샷 백킹.
6. parity harness가 preset별 count/overlap/rank diff 출력.
7. broker/order/watch mutation 0. **kr.tradingview.com 브라우저 크롤링 0**(라이브러리 scanner API만). migration = additive 1건(operator upgrade).

## Testing Plan
| Layer | What | Count |
|---|---|---|
| Unit | tvscreener 빌더 정규화/필드 매핑/dry-run·commit | +3 |
| Unit | read-path 프리셋 매핑(채움/누락 degrade) + 순이익연속 honest | +3 |
| Unit | parity harness count/overlap/rank | +2 |
| Integration | fundamentals preset e2e: 채워진 row + category + freshness | +2 |

## Out of Scope
- DART를 screener 디스플레이에서 제거하지 않음(리포트/PIT/순이익연속 유지). market_valuation_snapshots(Naver) 폐기 안 함(별도 결정).
- exact 종목 일치, US(ROB-427), scheduler 활성화.

## Files Reference
| File | Change |
|---|---|
| `app/models/invest_kr_fundamentals_snapshot.py` | 신규 모델(crypto 스냅샷 미러) |
| `alembic/versions/*` | additive 신규 테이블 |
| `app/services/invest_kr_fundamentals_snapshots/{builder,repository}.py` | tvscreener KR 빌더+upsert |
| `app/jobs/invest_kr_fundamentals_snapshots.py` | dry-run-default 잡 |
| `app/services/tvscreener_capabilities.py` | roe/payout/gross_margin/growth/continuous_dividend/industry/52wH capability 추가 |
| `app/services/invest_view_model/fundamentals_screener.py` 외 로더 | read-path → tvscreener 스냅샷 primary(display) |
| `scripts/validate_screener_toss_parity.py` | parity harness |
| `tests/...` | builder/read-path/harness |

## Effort
PR-A ~5-6h(모델+migration 1h / 빌더+잡 3h / capability+test 2h) · PR-B ~4-5h · PR-C ~2-3h.

## Sequencing
PR-A → PR-B(스냅샷 소비) → PR-C(검증). DART/reports 트랙 병렬·독립. US(ROB-427)는 동일 패턴(tvscreener US StockScreener) 재사용 가능.

## Safety / Boundaries
- tvscreener는 **라이브러리 scanner API**로 주기적 sync→스냅샷 적재(요청당 라이브 아님). kr.tradingview.com 브라우저 크롤링 금지.
- additive 테이블 + dry-run 잡 + read-path. broker/order/watch mutation 없음.
- DART = 리포트 권위 소스 유지. production commit/scheduler 활성화 operator 승인.

## Related
ROB-359(parent) · ROB-422/425(DART fundamentals=리포트/PIT) · ROB-426(degraded-state·healthy-partition 재사용) · ROB-427(US, 동일 tvscreener 패턴) · ROB-280/281(refresh cadence) · ROB-330(PIT).

## Appendix — probe 재현
`StockScreener().set_markets(Market.KOREA).select(*fields).set_range(0,N).get()` (`tvscreener_service.query_stock_screener`). 검증 필드/커버리지는 위 표. 전체 universe 커버리지(소형주 포함)·DART 교차 정확도는 PR-A 빌더 구현 시 재확인.
