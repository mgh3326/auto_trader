# ROB-422 PR2c-1 — cheap_value·steady_dividend full parity + mismatch 재분류 + §10.1 하드닝 설계

- **이슈**: ROB-422 — PR2 세 번째 슬라이스의 1/2 (PR2c-1)
- **상태**: design (브레인스토밍 승인 — 성장기대주 연기, 2-PR 분리, cheap/steady full 이전)
- **날짜**: 2026-06-02
- **선행**: PR2a/PR2b(`fundamentals_screener` loader + `FUNDAMENTALS_PRESET_SPECS` 레지스트리 + 선언적 `_DERIVE_CHECKS` + 'fundamentals' freshness dependency, branch `rob-422` 머지). derive `dividend_paid_streak_years`(PR1) + 연-gap 가드(PR2a).
- **범위**: PR2c는 두 인프라로 분리. **PR2c-1 = derive-기반 보강·재분류·하드닝.** PR2c-2 = 저평가탈출-Toss(valuation-only 신규 loader, 별도 spec). 성장기대주-Toss = 별도 이슈(qoq=분기 수집 선행).

---

## 1. 목표

남은 partial 2개를 full로 올리고(cheap_value·steady_dividend, fundamentals_screener로 이전), mismatch 2개를 정직 재분류하고(auto_trader_original), PR2a 리뷰 §10.1 하드닝을 닫는다.

| preset(id) | Toss 기준 | valuation(SQL) | derive(평가) | parity |
|---|---|---|---|---|
| **cheap_value** | PBR0~1.5 + PER0~15 + 순익3y≥0% | `max_per`(0<per≤15), **`max_pbr`(0<pbr≤1.5)** | `min_earnings_growth_3y_avg=0`(≥0 비음수) | partial→**full** |
| **steady_dividend** | 배당수익률≥3% + 배당성향≥30% + 배당연속지급3y + 순익연속증가3y | `min_dividend_yield=0.03`(비율) | `min_payout_ratio=30`, **`min_dividend_paid_streak_years=3`**, `min_earnings_increase_streak_years=3` | partial→**full** |

**의식적 trade-off**: 두 preset은 현재 generic provider(tvscreener)로 라이브 동작 중. fundamentals_screener 이전 → snapshot-only → **fundamentals backfill 전까지 빈 결과 + dataState=missing**(정직, 다른 fundamentals preset과 일관, backfill 후 자동 full). 지금 동작하는 PER/PBR/배당 화면을 backfill 전까지 잃는 것은 프로젝트 honesty 철학상 수용(사용자 승인).

## 2. 신규 일반화 슬롯 2개 (`FundamentalsPresetSpec`)

- valuation: **`max_pbr: Decimal | None`** → loader SQL `if spec.max_pbr: where pbr > 0 AND pbr <= max_pbr`.
- derive: **`min_dividend_paid_streak_years: int | None`** → `_DERIVE_CHECKS`에 `("min_dividend_paid_streak_years", "dividend_paid_streak_years")` 추가 + `_CARRIED_DERIVE_METRICS`에 `dividend_paid_streak_years` 추가(row carry). (derive `FundamentalsDerivation.dividend_paid_streak_years` 이미 존재.)

## 3. 레지스트리 2개 spec + 카탈로그 parity 갱신

```python
CHEAP_VALUE_SPEC = FundamentalsPresetSpec(
    preset_id="cheap_value",
    max_per=Decimal("15"), max_pbr=Decimal("1.5"),
    min_earnings_growth_3y_avg=Decimal("0"),   # ≥0% 비음수
    sort_by="earnings_growth_3y_avg",
)
STEADY_DIVIDEND_SPEC = FundamentalsPresetSpec(
    preset_id="steady_dividend",
    min_dividend_yield=Decimal("0.03"),        # 3% (비율, naver /100 일관)
    min_payout_ratio=Decimal("30"),
    min_dividend_paid_streak_years=3,
    min_earnings_increase_streak_years=3,
    sort_by="dividend_yield",
)
```
둘 다 `FUNDAMENTALS_PRESET_SPECS`에 추가 → registry dispatch가 자동 라우팅(PR2b seam). 카탈로그 `cheap_value`/`steady_dividend` 엔트리의 `parityStatus` partial→`full`, `parityNote`=None, filterChips에 추가 조건(순익증감/배당성향/연속지급·증가) 칩 반영. (이미 `_KR_ONLY_PRESET_IDS` 아님 → 추가 검토: cheap_value/steady_dividend는 현재 비-KR-only? 카탈로그 확인 후 KR-only면 유지.)

**sort 방향**: cheap_value=`earnings_growth_3y_avg` desc, steady_dividend=`dividend_yield` desc — 둘 다 "높을수록 좋음"이라 기존 desc 정렬 일반화 재사용(asc 지원 추가 불필요).

**generic provider 이탈**: 두 preset이 `FUNDAMENTALS_PRESET_SPECS`에 들어가면 dispatch가 loader로 선라우팅 + snapshot-only 가드가 fallback 차단 → 기존 `_SCREENING_FILTERS[cheap_value/steady_dividend]`는 dead. churn/회귀 위험 최소화 위해 **엔트리는 보존**(미사용, loader 우선). (drift 테스트가 _SCREENING_FILTERS 완전성 요구하면 보존이 안전.)

## 4. mismatch 2 재분류 (카탈로그만, loader 무변경)

- `oversold_recovery`: `presetOrigin='auto_trader_original'`, `parityStatus=None`, name="과매도 반등(RSI)" 류, parityNote를 자체 스크린 설명으로(Toss parity 시도 아님 명시). **RSI≤30 generic-provider 라우팅 불변**.
- `growth_expectation`: 동일, "대형 모멘텀(시총·등락률)" 자체 스크린. **market_cap+change_rate 라우팅 불변**.
- 이 둘은 `FUNDAMENTALS_PRESET_SPECS`에 **넣지 않음**(여전히 generic provider).

## 5. PR2a §10.1 하드닝 (같은 loader 영역이라 동봉)

1. **멀티소스 symbol dedup 방어**: `load_fundamentals_preset_from_snapshots` candidate 처리에 `seen: set[str]` dedup(high_yield_value_screener:149-157 패턴). KR 현재 단일소스라 live 버그는 아니나 방어.
2. **loader DB 통합 테스트**: db_session에 market_valuation + financial_fundamentals seed → 포함/제외/missing e2e(PR2a가 빠뜨린 orchestration 커버리지; high_yield_value 선례 있음).
3. **Path B 테스트**: valuation 파티션 존재 + fundamentals 0행 → `fundamentals_state='missing'` dependency가 freshness에 노출(service-level).
4. (선택) fundamentals_state 'stale' 분류: 최신 period_end가 기대 cadence보다 오래되면 stale — 가볍게 추가 또는 후속 note.

## 6. doc — 파리티 매트릭스

`docs/invest-screener-toss-parity-matrix.md` 행별:
- 아직 저렴한 가치주(#3) → `full / cheap_value`; 꾸준한 배당주(#4) → `full / steady_dividend`.
- 저평가 탈출(#6) → `missing(저평가탈출-Toss 미구현, PR2c-2 예정)`; 기존 `oversold_recovery`(RSI) → `extra(auto_trader_original)`.
- 성장 기대주(#8) → `missing(qoq=분기 수집 필요, 별도 이슈)`; 기존 `growth_expectation`(시총·등락률) → `extra(auto_trader_original)`.
- full/partial/mismatch/missing/extra 카운트 재집계(mismatch 0으로, extra +2, full +2).

## 7. 테스트 (TDD)

1. **신규 슬롯**(순수 evaluate): cheap_value spec — earnings_growth_3y_avg≥0 충족→included / 음수→제외; unavailable→제외+note. steady_dividend spec — payout/dividend_paid_streak/earnings_increase_streak 각 below→제외, dividend_paid_streak<3→제외, 전 조건 충족→included(+row가 dividend_paid_streak_years carry).
2. **max_pbr SQL 필터**(loader db_session): pbr>1.5 후보 제외, NULL pbr 제외.
3. **min_dividend_yield 0.03**(loader db_session): 0.02 후보 제외, 0.03+ 후보(steady_dividend).
4. **레지스트리/카탈로그**: cheap_value/steady_dividend가 `FUNDAMENTALS_PRESET_SPECS` + 카탈로그 parityStatus=full; dispatch가 loader로 라우팅(generic 미호출).
5. **mismatch 재분류**: oversold_recovery/growth_expectation presetOrigin='auto_trader_original', parityStatus=None, FUNDAMENTALS_PRESET_SPECS 비포함(generic 라우팅 보존).
6. **§10.1**: dedup(중복 symbol 입력→1행); loader DB 통합(포함/제외/missing); Path B(empty→missing dependency).
7. **회귀**: profitable_company/undervalued_growth/stable_growth/future_dividend_king(PR2a/2b) + high_yield_value + full-3 무변경.

## 8. 안전·범위 경계

read-only, **migration 0**(PR1 테이블 재사용), KR-only, snapshot-only(generic fallback 금지), broker/order/watch mutation 0, 무음 통과/날조 금지, derive→screener 의존 방향. **full 이전으로 cheap_value/steady_dividend는 backfill 전 빈결과+missing(의식적, 정직)**. 프로덕션 backfill·scheduler operator-gated.

## 9. 후속 (범위 아님)
- **PR2c-2**: 저평가탈출-Toss(valuation-only: PER0~10+PBR0~1+신고가) 신규 별도 loader(high_yield_value 패턴, fundamentals dependency 없음).
- **별도 이슈**: 성장기대주-Toss(qoq) — 분기 fundamentals 수집(default_dart_fetcher 확장) 선행 후.
- 프론트 칩 polish(필요 시).
