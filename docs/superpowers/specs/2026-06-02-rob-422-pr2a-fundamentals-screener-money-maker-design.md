# ROB-422 PR2a — fundamentals 스크리닝 인프라 + 돈잘버는회사 vertical 설계

- **이슈**: ROB-422 ([수집] /invest/screener Toss fundamentals 프리셋 parity) — PR2 첫 슬라이스
- **상태**: design (브레인스토밍 승인 완료 — mismatch=재분류+신규, PR2a=인프라+1 vertical)
- **날짜**: 2026-06-02
- **선행**: PR1(`financial_fundamentals_snapshots` read-model + `derive.py` — branch `rob-422`에 머지됨, derive **미배선**). 선례=`high_yield_value_screener.py`(valuation 스냅샷 기반 preset loader).
- **범위**: PR2 전체(8 preset)는 과대 → 분할. **PR2a = 공유 인프라 + 돈잘버는회사(money_maker) 1개 preset end-to-end.** 나머지 7개 preset은 PR2b/c(§10).

---

## 1. 배경

PR1이 `financial_fundamentals_snapshots`(회계기간 grain, PIT `filing_date`) + 순수 `derive_fundamentals_metrics`(8지표, PIT-게이트)를 만들었으나 **어떤 surface에도 미배선**(read-path 부재). PR2a가 derive를 `/invest/screener` read-path에 **처음 배선**한다. `high_yield_value`(valuation 스냅샷에서 ROE/PER 필터→랭킹, `screener_service.py:1537` dispatch + snapshot-only 가드 1582)가 정확한 선례다.

접근 B(재현, not 미러링) 유지: Toss 프리셋 *기준*을 우리 read-model로 계산.

## 2. 목표·범위 (PR2a)

가장 단순한 2-조건 Toss preset **돈잘버는회사(`profitable_company`) = `gross_margin_ttm`≥20% + ROE≥15%**를 DB→loader→screener_service→API→프론트까지 띄워 전체 경로를 증명하고, 이후 7개 preset이 얹힐 **공유 fundamentals 스크리닝 인프라**를 만든다.

**포함**: derive §10.1 정합 수정(배선 전 latent 닫기), bulk `latest_periods_for_symbols`, 범용 `fundamentals_screener` loader, `'fundamentals'` freshness dependency, `profitable_company` preset 배선 + parity matrix doc 갱신, TDD.

**제외(PR2b/c §10)**: 저평가성장주/미래의배당왕/안정성장주, cheap_value·steady_dividend 보강, 저평가탈출·성장기대주-Toss 신규 + 기존 2 재분류, 프론트 칩 정리.

**안전 경계(상속)**: broker/order/watch/order-intent/trade-journal mutation 0. **migration 0**(PR1 테이블 재사용). KR-only. read-only. derive→screener 의존 방향만(역방향 import 금지). 프로덕션 backfill·scheduler는 operator-gated.

## 3. derive §10.1 정합 수정 (`derive.py` — 배선 전 선결)

derive가 PR2a에서 처음 wired되고, `derive_fundamentals_metrics`는 preset이 1지표만 읽어도 8지표 전부 계산하므로 streak/배당 경로가 reachable해진다. 알려진 latent 항목을 먼저 닫는다:

1. **streak 연-인접성 가드**: `_increase_streak`/`_dividend_paid_streak`/`_dividend_growth_streak`가 리스트-인접만 보고 회계연도-인접을 미확인 → 누락 연도가 연속으로 오집계. 인접 두 행의 `int(fiscal_period[:4])`가 `+1` 연속이 아니면 streak break. (streak 함수가 `FundamentalPeriod.fiscal_period`를 받도록 — 현재 값 리스트만 받으면 `(value, year)` 페어로 변경.)
2. **dividend-on-empty 시맨틱**: 0 visible periods(`annual` 비어있음)면 `dividend_paid_streak_years`/`dividend_growth_streak_years`도 `unavailable`(value 0 금지) — missing≠zero 일관. (현재 `(ok,0)`/`(partial,0)` 반환을 `_UNAVAILABLE`로 교체; `annual` 비었을 때만.)
3. **커버리지 보강**: streak 연-gap break, qoq ok-path(분기 2개), gross_margin 4Q TTM rollup, 0-visible 전부-unavailable.

## 4. bulk 조회 — `repository.latest_periods_for_symbols`

`FinancialFundamentalsSnapshotsRepository`에 신규:

```python
async def latest_periods_for_symbols(
    self, *, market: str, symbols: Iterable[str], period_type: str | None = None
) -> dict[str, list[FinancialFundamentalsSnapshot]]:
    """symbol → period_end_date asc 행 리스트. 유니버스 스크리닝 N+1 방지(1 쿼리)."""
```

`market`+`symbol IN (...)`[+`period_type`] 단일 SELECT, `order_by(symbol, period_end_date asc)`, 파이썬에서 symbol별 그룹핑. 미존재 symbol은 키 없음(빈 처리는 호출측).

## 5. 범용 loader — `app/services/invest_view_model/fundamentals_screener.py` (신규)

`high_yield_value_screener.py` 미러. preset별 임계값은 config로 주입(이후 preset 재사용).

```python
@dataclass(frozen=True)
class FundamentalsPresetSpec:
    preset_id: str
    min_roe: Decimal | None = None              # valuation.roe (percent, 예: 15.0)
    min_gross_margin_ttm: Decimal | None = None # derive ratio (예: 0.20)
    # PR2b 확장 슬롯: min_revenue_growth_3y_avg, min_earnings_growth_3y_avg,
    #   min_payout_ratio, min_dividend_yield, min_*_streak_years, max_per, max_pbr ...
    sort_by: str = "roe"                         # 'roe' | 'gross_margin_ttm' | ...
```

`load_fundamentals_preset_from_snapshots(*, market, spec, today, limit) -> _SnapshotLoadResult | None`:
1. **candidate 유니버스**: valuation 최신 파티션에서 `spec`의 valuation 조건(예: `roe >= min_roe`, roe NOT NULL fail-closed) 만족 symbol 집합. (valuation 파티션 없으면 `None` → snapshot-only 가드가 missing 처리.)
2. **fundamentals bulk read**: `latest_periods_for_symbols(market, candidate_symbols)`.
3. **per-symbol derive**: 행→`FundamentalPeriod` 변환 → `derive_fundamentals_metrics(periods, report_date=today)`. (라이브 스크리너 = `report_date=today` → PIT 게이트 = `filing_date<=today` = 최신 공시.)
4. **predicate**: `spec`의 fundamentals 조건(예: `gross_margin_ttm.state=='ok' and value>=min_gross_margin_ttm`). **`state in {'partial','unavailable'}` → 후보 제외 + per-row reason note(무음 통과 금지).**
5. **랭킹**: `spec.sort_by` desc. limit 적용.
6. **반환**: `_SnapshotLoadResult`(rows + valuation partition_date/computed_at + fundamentals partition 메타 + per-state). **fundamentals가 비어있음(운영 backfill 전) → rows=[] + fundamentals dependency dataState='missing'**(크래시 금지, 정직).

`_SnapshotLoadResult` 반환으로 high_yield_value의 `list[dict]|None`(빈 결과 시 partition_computed_at 유실) 한계도 이 경로에선 해소.

## 6. data_state 매핑 + 'fundamentals' freshness dependency

- **MetricResult.state → 후보 포함/제외**: `ok`=평가, `partial`/`unavailable`=제외+note. 절대 fabricate 통과 없음.
- **`'fundamentals'` dependency**: `screener_service.py:1724-1757`(investor_flow 템플릿) 뒤에 KR + fundamentals-backed preset일 때 append: `{kind:'fundamentals', snapshot_date, collected_at(=source_collected_at), data_state, source:'financial_fundamentals_snapshots'}`. fundamentals partition 결측/0행 → `data_state='missing'` → `compute_overall_state`가 overall을 보수적으로 missing/stale로 끌어내림(이미 존재하는 로직).
- **primary**: `profitable_company`의 primary_source='market_valuation_snapshots'(candidate 유니버스 구동), fundamentals는 dependency. (둘 다 신선해야 overall fresh.)

## 7. 돈잘버는회사 preset 배선

- **카탈로그**(`screener_presets.py`): `SCREENER_PRESETS`에 추가 — `id='profitable_company'`, `name='돈 잘버는 회사'`, `presetOrigin='toss_parity'`, `parityStatus='full'`, `parityNote=None`(두 조건 다 구현), `filterChips=[국내, 매출총이익률 TTM '20% 이상', ROE '15% 이상', 데이터 '지연 스냅샷 기반']`, `metricLabel='ROE'`, `market='kr'`. `_KR_ONLY_PRESET_IDS`에 추가.
- **dispatch**(`screener_service.py:1537` high_yield_value 블록 뒤): `elif preset_id == 'profitable_company'`: `load_fundamentals_preset_from_snapshots(market, spec=PROFITABLE_COMPANY_SPEC(min_roe=15.0, min_gross_margin_ttm=0.20, sort_by='roe'), today, limit)`. **snapshot-only 가드**(loader None → `_snapshot_state_override='missing'`, 제네릭 fallback 금지 — 제네릭 provider엔 gross_margin 필터 없음).
- **doc**: `docs/invest-screener-toss-parity-matrix.md`의 돈잘버는회사 행을 `full / profitable_company 구현`으로 갱신(나머지 7개는 PR2b/c 표기 유지).

## 8. 테스트 (TDD)

1. **derive 수정**: streak 연-gap break(연도 누락→streak 미연장); dividend-on-empty→unavailable; qoq ok-path(분기2); gross_margin 4Q TTM rollup; 0-visible 전부 unavailable.
2. **bulk repo**(integration, db_session): 다중 symbol upsert→`latest_periods_for_symbols`가 symbol별 period asc 그룹 반환; 미존재 symbol 키 없음.
3. **loader**(순수, fake repo/valuation): valuation ROE≥15 후보 → gross_margin≥0.20 ok면 포함/미달·unavailable면 제외+note; fundamentals 빈 테이블→rows[] + fundamentals dataState missing; 랭킹/limit; valuation 파티션 없음→None.
4. **freshness dependency**: fundamentals dependency가 spec 결과에 포함; missing이면 overall이 missing/stale로.
5. **catalog**: `profitable_company` 존재, parityStatus='full', filterChips 정확, KR-only.
6. **screener_service dispatch**: preset_id='profitable_company'→loader 호출, snapshot-only(None→missing override), primary_source='market_valuation_snapshots'.
7. **회귀**: high_yield_value/consecutive_gainers/double_buy 경로 무변경(full-3 비회귀); derive 기존 7 테스트 green.

## 9. 운영 현실 (정직)

프로덕션 `financial_fundamentals_snapshots`는 **operator backfill 전까지 비어있음**(PR1은 dry-run, 프로덕션 write 0). 따라서 돈잘버는회사는 backfill 전 **0결과 + dataState=missing**(정직 — `fresh/full` 위장 금지, AC 충족). PR2a는 "배선 완료, 데이터는 operator-gated". backfill 후 자동으로 결과 표출.

## 10. 후속 (PR2b/c — 범위 아님)

- **PR2b**: 저평가성장주(revenue_growth_3y_avg/earnings_growth_3y_avg + PER), 안정성장주(ROE+earnings_growth_3y_avg+earnings_increase_streak), 미래의배당왕(dividend_yield+dividend_growth_streak+earnings_increase_streak+payout_ratio) — `FundamentalsPresetSpec` 확장 슬롯에 임계값만 추가. streak/배당 지표 본격 사용(§3 가드 필수).
- **PR2c**: cheap_value 보강(+earnings_growth_3y_avg≥0%), steady_dividend 보강(2%→3% + payout_ratio/dividend_paid_streak/earnings_increase_streak), 저평가탈출-Toss(PER0~10+PBR0~1+high_52w) 신규 + 성장기대주-Toss(earnings_growth_3y_avg≥3%+earnings_growth_qoq≥10%) 신규, 기존 oversold_recovery/growth_expectation을 `auto_trader_original`로 재분류(parityStatus mismatch 제거, 이름/note 자체 스크린 명확화), 파리티 매트릭스 전체 갱신 + 프론트 칩 polish.
- **operator-gated**: 프로덕션 fundamentals backfill(연간 우선 페이싱) + scheduler.

### 10.1 PR2a 구현 리뷰 후속 (12-agent adversarial, 2026-06-02)

PR2a 구현은 wired 코드 blocker 0 / faithful·safe(전부 PRAISE)로 검증됨. fix-now(랭킹/limit 회귀 테스트)는 PR2a에서 처리 완료(`44e1123b`). 아래는 PR2b/c에서 처리:
1. **(방어) 멀티소스 symbol dedup**: `fundamentals_screener` candidate 쿼리가 `source` 미필터 → KR이 다중 valuation source가 되면 symbol 중복. 현재는 KR=`naver_finance` 단일소스라 live 버그 아님(검증됨), 그러나 sibling `high_yield_value_screener`엔 `seen` 가드 있음. `DISTINCT ON (symbol)` 또는 source 우선순위/`seen` 가드 추가(PR2b가 다중 metric/소스 도입 시 필수).
2. **loader DB 통합 테스트**: `load_fundamentals_preset_from_snapshots`의 오케스트레이션(valuation max-date + min_roe SQL + KRSymbolUniverse name join + `_to_period` 매핑 + derive PIT)이 e2e 미테스트(순수 core + repo leaf만 테스트). `high_yield_value` 선례엔 db_session 통합 테스트 존재 → 동일 패턴으로 3테이블 seed 후 loader 호출 테스트 추가.
3. **empty-fundamentals→missing dependency 테스트(Path B)**: valuation 존재+fundamentals 빈 → `fundamentals_state='missing'` dependency가 freshness에 노출되는 경로 미테스트(현재 backfill 전 *기본 동작*이라 중요). mock loader가 `FundamentalsScreenResult(rows=[], fundamentals_state='missing')` 반환 시 `freshness.dependencies`에 `kind='fundamentals', dataState='missing'` 단언.
4. **dependency 메타데이터 값 단언**: 현 service 테스트는 `kind='fundamentals'` 존재만 확인 → snapshotDate/collectedAt/dataState 값까지 단언 보강.
5. **(선택) fundamentals_state 'stale' 분류**: 현재 fresh/missing만 — 최신 period_end가 cadence보다 오래되면 stale 분류(honesty 보강). row-level `_screener_snapshot_state`는 valuation만 반영하므로 fundamentals staleness는 dependency로만 노출됨을 문서화 or 보강.
