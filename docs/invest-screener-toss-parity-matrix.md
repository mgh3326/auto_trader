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
| 2 | 저평가 성장주 | 3년 평균 매출 증감률 ≥10% + PER 0~20 + 3년 평균 순이익 증감률 ≥20% | — | **missing** | `per`✅ / `revenue_growth_3y_avg`❌ `earnings_growth_3y_avg`❌ | per: valuation / 성장률: **unavailable** | ❌ no (source gap) |
| 3 | 아직 저렴한 가치주 | PBR 0~1.5 + PER 0~15 + 3년 평균 순이익 증감률 ≥0% | `cheap_value` | **partial** | `per`✅ `pbr`✅ / `earnings_growth_3y_avg`❌ | per/pbr: valuation / 성장: unavailable | ⚠️ partial |
| 4 | 꾸준한 배당주 | 배당수익률 ≥3% + 배당성향 ≥30% + 배당 연속지급 3년 + 순이익 연속증가 3년 | `steady_dividend` | **partial** (+threshold mismatch) | `dividend_yield`✅(단 임계 **2%** vs Toss **3%**) / `payout_ratio`❌ `dividend_paid_streak_years`❌ `earnings_increase_streak_years`❌ | dividend_yield: valuation / 나머지: unavailable | ⚠️ partial |
| 5 | 돈 잘버는 회사 찾기 | 최근 1년 TTM 매출총이익률 ≥20% + ROE ≥15% | `profitable_company` | **full** | `roe`✅ `gross_margin_ttm`✅ | `market_valuation_snapshots` (roe) + `financial_fundamentals_snapshots` (gross_margin_ttm) | ✅ yes (implemented in ROB-422 PR2a) |
| 6 | 저평가 탈출 | PER 0~10 + PBR 0~1 + 신고가 | `oversold_recovery` | **mismatch** | `per`✅ `pbr`✅ `high_52w`✅ / 현재는 `RSI≤30` 기반(의미 다름) | per/pbr/high_52w: valuation / RSI: derived OHLCV | ❌ no (semantics) |
| 7 | 미래의 배당왕 찾기 | 배당수익률 ≥1% + 배당 연속성장 3년 + 순이익 연속증가 3년 + 배당성향 ≥30% | — | **missing** | `dividend_yield`✅ / `dividend_growth_streak_years`❌ `earnings_increase_streak_years`❌ `payout_ratio`❌ | dividend_yield: valuation / 나머지: unavailable | ❌ no (source gap) |
| 8 | 성장 기대주 | 3년 평균 순이익 증감률 ≥3% + 직전분기 대비 순이익 증감률 ≥10% | `growth_expectation` | **mismatch** | 현재는 `market_cap`≥1조 + `change_rate` 상위(의미 다름) / `earnings_growth_3y_avg`❌ `earnings_growth_qoq`❌ | 현재: valuation(market_cap)+OHLCV / 성장: unavailable | ❌ no (semantics) |
| 9 | 쌍끌이 매수 (screenId=18) | 1일 등락률 ≥0% + 외국인 순매수 증가 + 기관 순매수 증가 | `double_buy` | **full** (ROB-276) | `change_rate`✅ `foreign_net`✅ `institution_net`✅ `double_buy`✅ | OHLCV + `investor_flow_snapshots` | ✅ yes |
| 10 | 고수익 저평가 | ROE ≥15% + PER 0~10 | `high_yield_value` | **full** (ROB-359) | `roe`✅ `per`✅ (둘 다 보유) | `market_valuation_snapshots` (roe+per) | ✅ yes |
| 11 | 안정 성장주 | ROE ≥15% + 3년 평균 순이익 증감률 ≥10% + 순이익 연속증가 3년 | — | **missing** | `roe`✅ / `earnings_growth_3y_avg`❌ `earnings_increase_streak_years`❌ | roe: valuation / 나머지: unavailable | ❌ no (source gap) |

### 2.1 auto_trader 자체 프리셋 (Toss 기본 11개에 없음 → extra)

| auto_trader preset | name | 조건 | 상태 | 비고 |
|---|---|---|---|---|
| `kr_high_volume_surge` | 거래량 급증 | `volume` desc 상위 | **extra** | Toss 기본 프리셋 아님 |
| `investor_flow_momentum` | 수급 모멘텀 | 외국인 3일+ 연속순매수 + `double_buy` | **extra** | `쌍끌이 매수`(screenId=18)와 관련되나 별개(외국인 연속성 중심) |

---

## 3. 상태 요약

| 상태 | 개수 | 프리셋 |
|---|---|---|
| **full** | 4 | 연속 상승세(`consecutive_gainers`), 쌍끌이 매수(`double_buy`), 고수익 저평가(`high_yield_value`), 돈 잘버는 회사(`profitable_company`) |
| **partial** | 2 | 아직 저렴한 가치주(`cheap_value`), 꾸준한 배당주(`steady_dividend`) |
| **mismatch** | 2 | 저평가 탈출(`oversold_recovery`), 성장 기대주(`growth_expectation`) |
| **missing** | 3 | 저평가 성장주, 미래의 배당왕 찾기, 안정 성장주 |
| **extra** | 2 | 거래량 급증, 수급 모멘텀 |

- 3개(full): 연속 상승세/쌍끌이 매수 = ROB-170/276, 고수익 저평가 = ROB-359 PR4(`market_valuation_snapshots` roe+per snapshot-first 로더). **재조사 불필요.**
- 4개(partial/mismatch)는 Acceptance criteria 대상: 의미 정정 또는 `partial/mismatch` 명시 필요(PR3 Scope B, 완료).
- 남은 4개(missing)는 재무제표 소스 신규 도입 선행 필요(§4.2).

---

## 4. missing/mismatch 분류 → 후속 작업 제안

### 4.1 기존 read-model로 즉시/근시일 구현 가능 (재무제표 소스 불요)
- **고수익 저평가** (ROE≥15 + PER 0~10) — ✅ **구현 완료 (ROB-359 PR4)**: `high_yield_value` preset + `market_valuation_snapshots`(roe+per) snapshot-first 로더(`high_yield_value_screener.py`). NULL roe/per은 fail-closed로 제외, 밸류에이션 파티션 부재 시 `missing` 정직 표면화.
- **저평가 탈출** mismatch 정정 — PER 0~10 + PBR 0~1 (+ `high_52w` 근접). RSI 기반 의미를 Toss 의미로 교체 또는
  현 RSI 프리셋을 `extra`로 재분류.
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
