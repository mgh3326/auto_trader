# ROB-422 PR2b — 남은 missing 3개 preset (저평가성장주·안정성장주·미래의배당왕) 설계

- **이슈**: ROB-422 — PR2 두 번째 슬라이스
- **상태**: design (브레인스토밍 승인 — 3개 한 PR, 일반화 approach A)
- **날짜**: 2026-06-02
- **선행**: PR2a(`fundamentals_screener` loader + `FundamentalsPresetSpec` + `profitable_company` + 'fundamentals' freshness dependency + derive §10.1 가드, branch `rob-422`에 머지). derive streak 연-인접성 가드가 PR2b streak 사용을 받쳐줌.
- **범위**: 남은 missing 3개를 PR2a 인프라 위 thin config로 추가. cheap_value·steady_dividend 보강 + mismatch 2 재분류/신규는 PR2c(§9).

---

## 1. 목표

PR2a의 공유 인프라에 **술어 일반화 + 3개 preset config**를 얹어 Toss missing 4개 중 나머지 3개를 점등한다(돈잘버는회사는 PR2a 완료).

| preset(id) | Toss 기준 | valuation 필터(SQL) | derive 술어(evaluate) |
|---|---|---|---|
| **저평가성장주**(`undervalued_growth`) | 매출3y≥10% + PER 0~20 + 순익3y≥20% | `max_per`(0<per≤20) | `revenue_growth_3y_avg≥0.10`, `earnings_growth_3y_avg≥0.20` |
| **안정성장주**(`stable_growth`) | ROE≥15% + 순익3y≥10% + 순익연속증가3y | `min_roe`(≥15) | `earnings_growth_3y_avg≥0.10`, `earnings_increase_streak_years≥3` |
| **미래의배당왕**(`future_dividend_king`) | 배당수익률≥1% + 배당연속성장3y + 순익연속증가3y + 배당성향≥30% | `min_dividend_yield`(≥1, **단위 verify**) | `dividend_growth_streak_years≥3`, `earnings_increase_streak_years≥3`, `payout_ratio≥30` |

모두 `presetOrigin='toss_parity'`, `parityStatus='full'`(전 조건 구현; 런타임 데이터 부족은 dataState로 표현, parity 강등 아님).

## 2. 일반화 (approach A — 명시적 spec 필드)

`FundamentalsPresetSpec`에 옵션 필드 추가(기존 `min_roe`/`min_gross_margin_ttm` 패턴 그대로):

```python
@dataclass(frozen=True)
class FundamentalsPresetSpec:
    preset_id: str
    # valuation 필터 (SQL 후보쿼리):
    min_roe: Decimal | None = None              # percent (15)
    max_per: Decimal | None = None              # 0 < per <= max_per
    min_dividend_yield: Decimal | None = None   # market_valuation_snapshots.dividend_yield 단위 (plan에서 확정)
    # derive 술어 (evaluate_fundamentals_candidates):
    min_gross_margin_ttm: Decimal | None = None        # ratio (0.20)
    min_revenue_growth_3y_avg: Decimal | None = None   # ratio (0.10)
    min_earnings_growth_3y_avg: Decimal | None = None  # ratio (0.10 / 0.20)
    min_earnings_increase_streak_years: int | None = None   # 정수 (3)
    min_dividend_growth_streak_years: int | None = None     # 정수 (3)
    min_payout_ratio: Decimal | None = None            # percent (30) — DART 현금배당성향%
    sort_by: str = "roe"   # 'roe'|'gross_margin_ttm'|'earnings_growth_3y_avg'|'dividend_yield'
```

**단위 규약(혼동 주의)**: growth 3y_avg·gross_margin = **비율**(derive 계산값), `payout_ratio` = **퍼센트**(DART 직접), streak = **정수**, `dividend_yield`·`roe`·`per` = valuation 컬럼(단위는 §6 verify).

### 2.1 valuation 후보쿼리 일반화 (`load_fundamentals_preset_from_snapshots`)
현재 `min_roe`만 → 조건부 `where` 추가: `max_per`(있으면 `per>0 AND per<=max_per`), `min_dividend_yield`(있으면 `dividend_yield>=min_dividend_yield`, NULL 제외 fail-closed). **후보 정렬·캡**: 결과 정렬은 `sort_by`로 derive 후 수행하므로, 후보 cap은 `market_cap.desc().nullslast()`로 정렬 후 `max(limit*N, ...)` (대형주 우선), **캡 초과 시 coverage 경고**(무음 절단 금지). SELECT에 `dividend_yield` 추가(미래의배당왕 표시·정렬용).

### 2.2 derive 술어 일반화 (`evaluate_fundamentals_candidates`)
현재 gross_margin 단일 체크 → spec의 None-아닌 derive 필드 각각을 순회 체크하는 **선언적 체크 리스트**로 일반화:

```python
_DERIVE_CHECKS = [
    ("min_gross_margin_ttm", "gross_margin_ttm", "ge_ratio"),
    ("min_revenue_growth_3y_avg", "revenue_growth_3y_avg", "ge_ratio"),
    ("min_earnings_growth_3y_avg", "earnings_growth_3y_avg", "ge_ratio"),
    ("min_payout_ratio", "payout_ratio", "ge_value"),
    ("min_earnings_increase_streak_years", "earnings_increase_streak_years", "ge_int"),
    ("min_dividend_growth_streak_years", "dividend_growth_streak_years", "ge_int"),
]
```
각 활성 체크: `MetricResult.state != 'ok'` 또는 value None → 후보 제외 + `excluded.append({symbol, reason: "<metric> unavailable"})`(무음 통과 금지). value < threshold → 제외 + reason `"<metric> below threshold"`. 모든 활성 체크 통과해야 included. **출력 row는 체크/정렬에 쓰인 모든 derive 지표 값 + valuation 값을 carry**(정렬 + UI 표시 근거).

### 2.3 sort 일반화
`sort_key` 매핑을 `roe`/`gross_margin_ttm`/`earnings_growth_3y_avg`/`dividend_yield`로 확장(row가 해당 키 carry). 정렬은 기존 `(value is None, -value, symbol)` (desc, NULLs last, symbol tiebreak) 유지.

## 3. dispatch 레지스트리 일반화 (clean 개선)

PR2a의 단일 `elif preset_id == "profitable_company"` 분기를 **레지스트리 기반 단일 분기**로 리팩터:

```python
# fundamentals_screener.py
FUNDAMENTALS_PRESET_SPECS: dict[str, FundamentalsPresetSpec] = {
    "profitable_company": PROFITABLE_COMPANY_SPEC,
    "undervalued_growth": UNDERVALUED_GROWTH_SPEC,
    "stable_growth": STABLE_GROWTH_SPEC,
    "future_dividend_king": FUTURE_DIVIDEND_KING_SPEC,
}
```
`screener_service.py` dispatch: `elif preset_id in FUNDAMENTALS_PRESET_SPECS:` → `load_fundamentals_preset_from_snapshots(spec=FUNDAMENTALS_PRESET_SPECS[preset_id], ...)`. snapshot-only 가드·`primary_source='market_valuation_snapshots'`·'fundamentals' dependency append 도 `preset_id in FUNDAMENTALS_PRESET_SPECS`로 일반화(profitable_company 동작 보존). 빈 결과 경고 문구는 preset별 또는 공통.

## 4. 카탈로그 (3개 추가)

`SCREENER_PRESETS`에 3개 `ScreenerPreset`(presetOrigin=`toss_parity`, parityStatus=`full`, market=`kr`, `_KR_ONLY_PRESET_IDS` 추가) + filterChips:
- 저평가성장주: [국내, PER '0~20', 매출증가율 '3년평균 10%↑', 순이익증가율 '3년평균 20%↑', 데이터 '지연 스냅샷']
- 안정성장주: [국내, ROE '15%↑', 순이익증가율 '3년평균 10%↑', 순이익 '연속증가 3년↑', 데이터 '지연 스냅샷']
- 미래의배당왕: [국내, 배당수익률 '1%↑', 배당 '연속성장 3년↑', 순이익 '연속증가 3년↑', 배당성향 '30%↑', 데이터 '지연 스냅샷']

## 5. parity matrix doc
`docs/invest-screener-toss-parity-matrix.md`의 저평가성장주(#2)·안정성장주(#11)·미래의배당왕(#7) 행을 `full / <preset_id>`로 갱신. mismatch 2·partial 2는 PR2c 표기 유지.

## 6. 미해결 → plan에서 확정 (verify-first)
- **`dividend_yield` 단위**: `market_valuation_snapshots.dividend_yield`(Numeric(10,6))가 percent(2.34)인지 ratio(0.0234)인지 — 빌더(`market_valuation_snapshots/builder.py` naver percent vs yahoo ratio 매핑) + 실 저장 샘플로 확정 후 `min_dividend_yield` 값 결정(1% → 1.0 또는 0.01). steady_dividend가 `min_dividend_yield=2.0`(percent 가정)인 점 교차참조.
- **growth_3y_avg 데이터 깊이**: `_growth_3y_avg`는 4 annual 연도라야 'ok', <4면 'partial' → 후보 제외. 연간 backfill 깊이가 충분해야 저평가성장주/안정성장주가 결과를 냄(operator backfill 사안, parity와 무관).

## 7. 테스트 (TDD)
1. **spec 필드/술어 일반화**(순수 `evaluate_fundamentals_candidates`): 각 preset spec으로 — 전 조건 충족→included(+row가 지표값 carry); 한 derive 지표 below→제외+reason; 한 derive 지표 unavailable(state≠ok)→제외+"... unavailable"(무음 통과 금지); streak<3→제외; payout_ratio 퍼센트 단위 30 비교 정확.
2. **valuation SQL 필터**(loader, db_session 통합): max_per(0<per≤20) 후보; min_dividend_yield 후보; min_roe 후보; 각 NULL 제외.
3. **dispatch 레지스트리**: 4개 preset_id 전부 loader로 라우팅, snapshot-only(None→missing), generic fallback 미호출; profitable_company 회귀 무변경.
4. **catalog**: 3개 preset 존재, parityStatus='full', filterChips, KR-only.
5. **sort 일반화**: earnings_growth_3y_avg/dividend_yield 정렬 desc+NULLs last+limit.
6. **회귀**: profitable_company(PR2a) + high_yield_value + full-3 무변경.

## 8. 안전·범위 경계
read-only, **migration 0**(PR1 테이블 재사용), KR-only, snapshot-only(generic fallback 금지), broker/order/watch mutation 0, fundamentals 비backfill→0결과+missing(정직), 무음 통과/날조 금지. derive→screener 의존 방향만. 프로덕션 backfill·scheduler operator-gated.

## 9. 후속 (PR2c — 범위 아님)
cheap_value 보강(+earnings_growth_3y_avg≥0), steady_dividend 보강(2%→3% + payout_ratio/dividend_paid_streak/earnings_increase_streak), 저평가탈출-Toss(PER0~10+PBR0~1+high_52w) 신규 + 성장기대주-Toss(earnings_growth_3y_avg≥0.03+earnings_growth_qoq≥0.10) 신규, 기존 oversold_recovery/growth_expectation을 auto_trader_original 재분류, 파리티 매트릭스 전체 갱신 + 프론트 칩 polish. + PR2a §10.1 잔여(멀티소스 dedup 방어, loader DB 통합테스트, Path B 테스트, fundamentals stale 분류).
