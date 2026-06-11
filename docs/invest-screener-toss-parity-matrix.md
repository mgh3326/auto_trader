# /invest/screener ↔ Toss 골라보기 parity gap matrix (ROB-359 Scope A)

> **무엇인가:** Toss Invest `주식 골라보기`의 토스증권 제작 기본 프리셋 11개 대비
> auto_trader `/invest/screener` KR 프리셋 구현 현황을 정확히 정리한 reference matrix.
>
> **무엇이 아닌가:** 신규 구현/마이그레이션 PR이 아님. 이 문서는 ROB-359 umbrella의
> **PR2(조사·문서)** 산출물이며, missing/mismatch 프리셋의 실제 구현은 후속 슬라이스/이슈로 분리한다.
>
> **기준일:** Toss 프리셋·필터값은 2026-05-29 브라우저 확인(이슈 ROB-359 본문) 기준.
> auto_trader 측은 `app/services/invest_view_model/screener_presets.py` 현재 코드 기준.

관련: ROB-170(`consecutive_gainers` Toss parity, Done), ROB-276(`double_buy` screenId=18 parity, Done),
ROB-280(snapshot refresh cadence — freshness 자체는 ROB-359 범위 밖), ROB-340(데이터 소스 계약).

---

## 1. read-model 인벤토리 (parity의 데이터 기반)

KR screener가 끌어올 수 있는 durable read-model과 컬럼은 다음과 같다. **Scope C는 새 테이블 설계가
아니라 아래 기존 모델을 screener read-path에 join하는 방향으로 좁힌다.**

| read-model | source(허용) | 노출 컬럼 (parity 관련) |
|---|---|---|
| `invest_screener_snapshots` | OHLCV 파생 | `latest_close`, `prev_close`, `change_amount`, `change_rate`, `consecutive_up_days`, `week_change_rate`, `daily_volume`, `closes_window` |
| `market_valuation_snapshots` | `naver_finance`, `yahoo` | `per`, `pbr`, `roe`, `dividend_yield`, `market_cap`, `high_52w`, `low_52w` |
| `investor_flow_snapshots` (KR only) | `naver_finance`, `kis`, `manual` | `foreign_net`, `institution_net`, `individual_net`, `*_net_buy/sell_rank`, `double_buy`, `double_sell`, `foreign/institution/individual_consecutive_buy/sell_days` |
| (요청시 파생) | OHLCV | `RSI` (스냅샷 컬럼 아님 — request-time 계산) |

### 1.1 전혀 없는 지표 (read-model 컬럼·source 모두 부재 → 재무제표 소스 필요)

아래는 Toss의 fundamentals 프리셋들이 요구하지만 **어떤 read-model에도 없고 수집 source도 미연결**이다.
KIS 재무비율 API / Naver financials 등 **다기간 재무제표 소스를 새로 도입해야** 채울 수 있다.

- 배당성향 (`payout_ratio`)
- 매출총이익률 TTM (`gross_margin_ttm`)
- 3년 평균 매출액 증감률 (`revenue_growth_3y_avg`)
- 3년 평균 순이익 증감률 (`earnings_growth_3y_avg`)
- 직전분기 대비 순이익 증감률 / QoQ (`earnings_growth_qoq`)
- 순이익 연속증가 연수 (`earnings_increase_streak_years`)
- 배당 연속지급 / 연속성장 연수 (`dividend_paid_streak_years`, `dividend_growth_streak_years`)

> `신고가`는 `high_52w`로 **근접(52주 고가 대비)** 판정은 가능하나, 엄밀한 "전고점 돌파" 신고가는
> 당일 고가 vs `high_52w` 비교 로직이 추가로 필요(partial/feasible).

---

## 2. Toss 11개 기본 프리셋 parity matrix

상태 범례: **full**(의미·조건 일치) · **partial**(일부 조건만, 나머지 source 부재) ·
**mismatch**(같은/유사 이름이나 의미가 다름) · **missing**(대응 프리셋 없음) · **extra**(auto_trader 자체, Toss 기본에 없음).

| # | Toss 프리셋 (screenId) | Toss 필터 | auto_trader preset | 상태 | 필요 read-model 컬럼 (have/missing) | source | reports 신규후보 사용 |
|---|---|---|---|---|---|---|---|
| 1 | 연속 상승세 | 주가등락률 1주일 전보다 ≥0% + 주가 연속상승 5일↑ | `consecutive_gainers` | **full** (ROB-170) | `week_change_rate`✅ `consecutive_up_days`✅ | derived OHLCV (`invest_screener_snapshots`) | ✅ yes |
| 2 | 저평가 성장주 | 3년 평균 매출 증감률 ≥10% + PER 0~20 + 3년 평균 순이익 증감률 ≥20% | `undervalued_growth` | **full** (ROB-422) | `per`✅ `revenue_growth_3y_avg`✅ `earnings_growth_3y_avg`✅ | `market_valuation_snapshots` + `financial_fundamentals_snapshots` | ✅ yes |
| 3 | 아직 저렴한 가치주 | PBR 0~1.5 + PER 0~15 + 3년 평균 순이익 증감률 ≥0% | `cheap_value` | **full** (ROB-422 PR2c-1) | `per`✅ `pbr`✅ `earnings_growth_3y_avg`✅ | `market_valuation_snapshots` + `financial_fundamentals_snapshots` | ✅ yes (gated by operator backfill) |
| 4 | 꾸준한 배당주 | 배당수익률 ≥3% + 배당성향 ≥30% + 배당 연속지급 3년 + 순이익 연속증가 3년 | `steady_dividend` | **full** (ROB-422 PR2c-1) | `dividend_yield`✅ `payout_ratio`✅ `dividend_paid_streak_years`✅ `earnings_increase_streak_years`✅ | `market_valuation_snapshots` + `financial_fundamentals_snapshots` | ✅ yes (gated by operator backfill) |
| 5 | 돈 잘버는 회사 찾기 | 최근 1년 TTM 매출총이익률 ≥20% + ROE ≥15% | `profitable_company` | **full** | `roe`✅ `gross_margin_ttm`✅ | `market_valuation_snapshots` (roe) + `financial_fundamentals_snapshots` (gross_margin_ttm) | ✅ yes (implemented in ROB-422 PR2a) |
| 6 | 저평가 탈출 (Toss) | PER 0~10 + PBR 0~1 + 52주 신고가 20거래일 이내 | `undervalued_breakout` | **full** (ROB-422 PR2c-2, ROB-430/432 의미 정정) | `per`✅ `pbr`✅ `week_high_52_date`✅ | `invest_kr_fundamentals_snapshots` | ✅ yes |
| 7 | 미래의 배당왕 찾기 | 배당수익률 ≥1% + 배당 연속성장 3년 + 순이익 연속증가 3년 + 배당성향 ≥30% | `future_dividend_king` | **full** (ROB-422) | `dividend_yield`✅ `dividend_growth_streak_years`✅ `earnings_increase_streak_years`✅ `payout_ratio`✅ | `market_valuation_snapshots` + `financial_fundamentals_snapshots` | ✅ yes |
| 8 | 성장 기대주 (Toss) | 3년 평균 순이익 증감률 ≥3% + 직전분기 대비 순이익 증감률 ≥10% | `growth_expectation_toss` | **full** (ROB-425, 분기 backfill 후 활성) | `earnings_growth_3y_avg`✅ `earnings_growth_qoq`✅ | `market_valuation_snapshots` + `financial_fundamentals_snapshots` (분기) | ✅ yes |
| 9 | 쌍끌이 매수 (screenId=18) | 1일 등락률 ≥0% + 외국인 순매수 증가 + 기관 순매수 증가 | `double_buy` | **full** (ROB-276) | `change_rate`✅ `foreign_net`✅ `institution_net`✅ `double_buy`✅ | OHLCV + `investor_flow_snapshots` | ✅ yes |
| 10 | 고수익 저평가 | ROE ≥15% + PER 0~10 | `high_yield_value` | **full** (ROB-359) | `roe`✅ `per`✅ (둘 다 보유) | `market_valuation_snapshots` (roe+per) | ✅ yes |
| 11 | 안정 성장주 | ROE ≥15% + 3년 평균 순이익 증감률 ≥10% + 순이익 연속증가 3년 | `stable_growth` | **full** (ROB-422) | `roe`✅ `earnings_growth_3y_avg`✅ `earnings_increase_streak_years`✅ | `market_valuation_snapshots` + `financial_fundamentals_snapshots` | ✅ yes |

### 2.1 auto_trader 자체 프리셋 (Toss 기본 11개에 없음 → extra)

| auto_trader preset | name | 조건 | 상태 | 비고 |
|---|---|---|---|---|
| `kr_high_volume_surge` | 거래량 급증 | `volume` desc 상위 | **extra** | Toss 기본 프리셋 아님 |
| `investor_flow_momentum` | 수급 모멘텀 | 외국인 3일+ 연속순매수 + `double_buy` | **extra** | `쌍끌이 매수`(screenId=18)와 관련되나 별개(외국인 연속성 중심) |
| `oversold_recovery` | 과매도 반등 (RSI) | `RSI` 30 이하 | **extra** | auto_trader 자체 스크린. Toss '저평가 탈출'과의 mismatch로 인해 재분류됨. |
| `growth_expectation` | 대형 모멘텀 (시총·등락률) | 시가총액 ≥1조 + 등락률 상위 | **extra** | auto_trader 자체 스크린. Toss '성장 기대주'와의 mismatch로 인해 재분류됨. |

---

## 3. 상태 요약

| 상태 | 개수 | 프리셋 |
|---|---|---|
| **full** | 11 | 연속 상승세, 쌍끌이 매수, 고수익 저평가, 아직 저렴한 가치주, 꾸준한 배당주, 돈 잘버는 회사, 저평가 성장주, 안정 성장주, 미래의 배당왕, 저평가 탈출, 성장 기대주 |
| **partial** | 0 | — |
| **mismatch** | 0 | — |
| **missing** | 0 | — |
| **extra** | 4 | 거래량 급증, 수급 모멘텀, 과매도 반등 (RSI), 대형 모멘텀 (시총·등락률) |

- 11개(full): 연속 상승세/쌍끌이 매수 = ROB-170/276, 고수익 저평가 = ROB-359, 돈 잘버는 회사 = ROB-422 PR2a, 저평가 성장주/안정 성장주/미래의 배당왕 = ROB-422 PR2b, 아직 저렴한 가치주/꾸준한 배당주 = ROB-422 PR2c-1, 저평가 탈출 = ROB-422 PR2c-2, 성장 기대주 = ROB-425.
- mismatch 프리셋들은 `auto_trader_original` 자체 프리셋(`extra`)으로 재분류됨.
- Toss 11/11 모든 기본 프리셋에 대해 full parity 완료 (ROB-425가 마지막 missing 종결).


---

## 4. missing/mismatch 분류 → 후속 작업 제안

### 4.1 기존 read-model로 즉시/근시일 구현 가능 (재무제표 소스 불요)
- **고수익 저평가** (ROE≥15 + PER 0~10) — ✅ **구현 완료 (ROB-359 PR4)**: `high_yield_value` preset + `market_valuation_snapshots`(roe+per) snapshot-first 로더(`high_yield_value_screener.py`). NULL roe/per은 fail-closed로 제외, 밸류에이션 파티션 부재 시 `missing` 정직 표면화.
- **저평가 탈출** — ✅ **구현 완료 (ROB-422 PR2c-2, ROB-430/432 의미 정정)**: `undervalued_breakout` preset + KR tvscreener fundamentals snapshot 로더. PER 0~10 + PBR 0~1 + 최근 20거래일 내 52주 신고가 필터링. `high_52w_proximity`는 보조 표시값이며 KR 통과 조건이 아니다.
- **cheap_value** partial: PER/PBR 임계는 일치, `순이익 성장 ≥0%` 조건만 미보유 → 4.2 의존.
- **steady_dividend** partial: 임계 2%→3% 정정은 즉시 가능, 배당성향/연속성 조건은 4.2 의존.

→ PR3(Scope B preset 정리) 또는 별도 small PR에서 처리. **결과를 억지로 만들지 말고** 미보유 조건은
`partial`로 표면화하고 freshness/warnings로 정직하게 노출.

### 4.2 재무제표(다기간 fundamentals) 소스 신규 도입이 선행돼야 하는 프리셋 (별도 이슈)
대상: **저평가 성장주, 돈 잘버는 회사 찾기, 미래의 배당왕 찾기, 안정 성장주** + cheap_value/steady_dividend의 잔여 조건.

필요 지표: `payout_ratio`, `gross_margin_ttm`, `revenue_growth_3y_avg`, `earnings_growth_3y_avg`,
`earnings_growth_qoq`, `earnings_increase_streak_years`, `dividend_paid/growth_streak_years`.

→ **신규 follow-up 이슈**로 분리(ROB-359 범위 밖). source 후보(KIS 재무비율 API vs Naver financials) 결정과
새 fundamentals/dividend read-model(또는 `market_valuation_snapshots.raw_payload` 확장) 설계가 PR-sized의 전제.

---

## 5. 경계
- 이 문서는 read/reference 산출물. broker/order/watch/order-intent mutation과 무관.
- Toss/Naver는 benchmark/gap-analysis용 reference이며 production source-of-truth로 격상하지 않는다.
- snapshot 신선도(스케줄러/refresh cadence) 자체는 **ROB-280** 소관. ROB-359는 label/dataState/warnings 정직성만 책임진다.
