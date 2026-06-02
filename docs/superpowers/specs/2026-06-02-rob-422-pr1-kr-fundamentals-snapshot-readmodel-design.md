# ROB-422 PR1 — KR 다기간 재무제표/배당 snapshot read-model (DART foundation) 설계

- **이슈**: ROB-422 ([수집] /invest/screener Toss fundamentals 프리셋 parity) — parent ROB-359
- **상태**: design (브레인스토밍 승인 완료 — 접근 B + PR1 데이터 기반만)
- **날짜**: 2026-06-02
- **선행/관련**: ROB-330(fundamentals PIT 패널, **설계만/코드0** — 컬럼 의미 정합), ROB-280(screener freshness policy), ROB-398(KR collector 라인, snapshot 패턴 템플릿), ROB-363(reports 후보 전환, 후속 소비자).
- **범위 결정**: ROB-422 전체(source audit + read-model + collector + screener catalog + reports)는 한 spec에 과대. 이 spec은 **PR1 = 데이터 기반만**. 화면/프리셋/리포트 배선은 PR2/PR3(§10).

---

## 1. 배경 — 검증된 사실 (코드 교차검증 완료)

- **`/invest/screener`는 이미 자체 스냅샷 기반**: preset 결과는 `invest_screener_snapshots`(OHLCV)·`market_valuation_snapshots`(PER/PBR/ROE/배당수익률)·`investor_flow_snapshots`(수급)에서 계산. Toss 피드는 경로에 없음.
- **Toss는 코드 전체에서 reference-only**: `invest_data_source_contract.py:161-171` `toss_screen` = `authority_tier="low_trust_attention"`, `may_affect_ranking=False`; `invest_coverage_service.py:119` "Toss is used only as a parity benchmark/reference, not as a data source." Toss API 클라이언트 부재(공식 스크리너 API 없음, 내부 엔드포인트는 비공식·쿠키인증·ToS 리스크). → **접근 B 확정**: Toss 출력 미러링이 아니라 Toss 프리셋 *기준(criteria)* 을 우리 데이터로 재현.
- **8개 지표 전부 부재**: `payout_ratio`, `gross_margin_ttm`, `revenue_growth_3y_avg`, `earnings_growth_3y_avg`, `earnings_growth_qoq`, `earnings_increase_streak_years`, `dividend_paid_streak_years`, `dividend_growth_streak_years` — 모델/스키마/KIS/collector 어디에도 없음.
- **DART 클라이언트는 이미 존재(공시 목록 전용)**: `app/services/disclosures/dart.py` — `OpenDartReader` lazy 빌드(`_get_client()` 155-170), corp_codes 일일 캐시+flock(99-152), `list_filings()`(공시 list만). `OPENDART_API_KEY` config 기존(`config.py:311`). **필요한 `finstate_all`(재무제표)·`report('배당')`(배당) 메서드는 vendored `opendartreader>=0.2.3`에 이미 포함, 현재 미사용**(repo-wide grep `finstate_all`/`report(`/`alotMatter` = 0 consumer).

## 2. 소스 결정 — DART/OpenDART = PRIMARY

| 소스 | 판정 | 근거 |
|------|------|------|
| **DART** | **PRIMARY** | 8개 전부 깨끗한 경로(2 direct: `payout_ratio`/원시 시리즈, 6 computed). 규제 원천, FY2015+ 깊은 이력, 배당 공시(`alotMatter`) 포함. 클라이언트 70% 기배선(신규 HTTP 클라이언트 불필요). |
| KIS | supplement만(PR1 제외) | 5개 계산 가능하나 기간 얕음(~3-5), **배당 3개 불가**, **PIT 없음**(`stac_yymm`만), 재무 TR 6개 신규구현 HIGH effort. |
| Naver | source-of-truth 회피 | `finance.naver.com/robots.txt`가 `/item/` Disallow + FnGuide 라이선스 → ROB-422 Non-goal 직격. depth ~3년, PIT 없음. (배당성향은 사실 페이지에 있으나 파서 allowlist `company.py:152`가 드롭 — PR1 무관.) |

**DART 도입 타협 불가 3가지(검증됨):**
1. **PIT 조인** — `finstate_all`/`alotMatter` 페이로드는 `rcept_dt`(공시일)를 안 줌. `alotMatter`의 `stlm_dt`는 결산기준일(기간말)이라 부족(45–90일 공시 지연). 각 행의 `rcept_no`를 **이미 호출 중인 공시목록 엔드포인트**(`client.list`)에 조인해 `filing_date`를 복원. → lookahead 방지의 핵심.
2. **분기 누적치 차분** — KR 분기보고서는 YTD 누적(`thstrm_add_amount`). 단일분기 값은 연속분기 차분(예: Q3누적 − 반기누적)으로 도출 후 저장(`discrete_*`). 미차분 시 QoQ/TTM 오류.
3. **백필 페이싱** — 키당 ~20k req/일. 전 종목 분기 일괄(≈2,500×4 reprt×N년×2 endpoint)은 한도 초과. **연간 우선(`reprt_code=11011`) + 분기 on-demand를 며칠 분산**. (PR1은 dry-run/소량이라 무관하나 설계에 반영.)

## 3. 목표·범위 (PR1)

기존 3개 snapshot 패밀리와 **동일 패턴**으로 신규 `financial_fundamentals_snapshots` 적재 경로 + PIT-aware 파생 헬퍼(unit-tested, **미배선**)를 추가한다.

**포함**: 신규 테이블/migration/ORM/repository/builder/freshness, DART 잠자는 메서드 활성화(`finstate_all`+`report('배당')`), `rcept_no→rcept_dt` PIT 조인, 분기 누적 차분, dry-run 기본 CLI, PIT-게이트 파생 헬퍼(순수 함수), TDD.

**제외(PR2/PR3 §10)**: screener preset/카탈로그/`parityStatus` 변경, `/invest/screener` API·프론트, `screen_stocks(kr)`/`/invest/reports` candidate lineage, 프로덕션 backfill·migration apply·scheduler 활성화, KIS/Naver 배선.

**안전 경계(ROB-422 Non-goal 상속)**: broker/order/watch/order-intent/trade-journal mutation 0. 주문 preview/submit/cancel/modify 0. 프로덕션 DB write는 `--commit` + operator. Toss/Naver scraping을 source-of-truth로 격상 안 함. missing metric을 heuristic으로 `full` 위장 금지. raw payload는 저장 전 `_redact_sensitive_keys` 적용(market-events 패턴).

## 4. Read-model 계약 — 신규 테이블 `financial_fundamentals_snapshots`

기존 `market_valuation_snapshots`(일별 grain, 현재 full-3 preset을 `fundamentals_evidence.py:29` 경유 구동 중)를 **확장하지 않는다** — fundamentals는 **회계기간(연/분기) grain + 공시 지연**으로 결이 다르고, 일별 키에 `filing_date`/`fiscal_period`를 얹으면 unique 키가 깨지거나 일별 행이 대부분-NULL로 오염되어 working preset 회귀 위험.

```
financial_fundamentals_snapshots
  id                  BigInteger PK
  market              String(8)   NOT NULL  CHECK IN ('kr','us')   -- PR1=kr만 적재, 컬럼은 market-agnostic
  symbol              String(20)  NOT NULL
  fiscal_period       String(10)  NOT NULL  -- 정규 라벨 '2025A' | '2025Q1'
  period_type         String(10)  NOT NULL  CHECK IN ('annual','quarterly')
  period_end_date     Date        NOT NULL  -- = as_of: 수치가 설명하는 회계기간말
  filing_date         Date        NULL      -- 공시일 = lookahead 경계. NULL이면 read-path에서 fail-closed
  effective_at        Date        NULL      -- 인용 가능 최초일(기본 = filing_date, ROB-330 위해 분리 보유)
  source              String(16)  NOT NULL  CHECK IN ('dart')      -- PR1=dart만. PR2+에서 kis/finnhub 확장
  source_collected_at TIMESTAMPtz NOT NULL  -- provenance fetch 시각(게이트 아님)
  currency            String(8)   NULL
  -- 저장 = 원시 기간 사실 + 차분 단일분기값 (집계는 저장 안 함):
  revenue             Numeric(30,2) NULL
  net_income          Numeric(30,2) NULL
  gross_profit        Numeric(30,2) NULL    -- ifrs-full_GrossProfit (없으면 NULL)
  cost_of_sales       Numeric(30,2) NULL    -- gross_profit 부재 시 매출액-매출원가 fallback용
  roe                 Numeric(20,4) NULL
  payout_ratio        Numeric(10,6) NULL    -- alotMatter 직접 (현금배당성향%)
  dividend_per_share  Numeric(20,4) NULL    -- alotMatter 주당현금배당금
  discrete_revenue    Numeric(30,2) NULL    -- 분기행: 누적 차분 단일분기 매출 (annual행=revenue)
  discrete_net_income Numeric(30,2) NULL    -- 분기행: 누적 차분 단일분기 순이익
  data_state          String(12)  NOT NULL DEFAULT 'fresh' CHECK IN ('fresh','stale','partial','unavailable')
  raw_payload         JSONB       NULL      -- redact 후 source 원본
  schema_version      Integer     NOT NULL DEFAULT 1
  computed_at, created_at, updated_at  TIMESTAMPtz  (server_default now(), updated_at onupdate now())
  UniqueConstraint(market, symbol, fiscal_period, source)  name uq_financial_fundamentals_snapshots_msfs
  Index(market, symbol, period_end_date)
  Index(market, symbol, filing_date)        -- "report_date 이하 최신 공시" PIT 쿼리용
```

**집계 지표는 컬럼으로 저장하지 않는다** — `revenue_growth_3y_avg`, `earnings_growth_3y_avg`, `earnings_growth_qoq`, `gross_margin_ttm`, `earnings_increase_streak_years`, `dividend_paid/growth_streak_years`는 **read-path에서 report_date 시점에 보이는(`filing_date <= report_date`) per-period 행들로 파생**. 미리 집계해 저장하면 최신 공시가 과거에 새어들어가 lookahead를 깬다(ROB-330 금지).

**ROB-330 정합**: 4개 시간 의미를 **별도 컬럼으로 분리**(period_end=as_of / filing=경계 / effective / collected). lookahead 규칙 `report_date >= filing_date(/effective_at) ⇒ visible, else unavailable`이 표현 가능. 배당은 동일 grain/키/소스라 **단일 테이블에 배당 컬럼 포함**(별도 `dividend_*` 테이블은 배당 소스가 갈릴 때만 — PR1 불필요).

## 5. Collector/loader — `app/services/financial_fundamentals_snapshots/` (신규)

기존 `app/services/{invest_screener,market_valuation,investor_flow}_snapshots/` 패턴 미러:

- **`builder.py`** — symbol(6자리) → `_get_client().find_corp_code` → corp_code. 연간 `finstate_all(corp, year, '11011', fs_div)` + `report(corp,'배당',year,'11011')`. 분기 필요 시 `reprt_code` 11013/11012/11014. account_id(XBRL: `ifrs-full_Revenue`/`ifrs-full_GrossProfit`/`ifrs-full_ProfitLoss`/`CostOfSales`) 우선, `account_nm` fallback. **분기 누적 차분**으로 `discrete_*` 산출. `FinancialFundamentalsUpsert` 페이로드 반환.
- **PIT 조인** — 각 `rcept_no`를 `client.list`(공시목록)에 조인해 `rcept_dt → filing_date`(`effective_at` 기본 동일). 조인 실패 시 `filing_date=NULL`(read-path fail-closed, 날조 금지).
- **`repository.py`** — `FinancialFundamentalsSnapshotsRepository` (서비스 내부 전용). `insert().on_conflict_do_update`(uq 제약), `latest_periods_for_symbols(...)`, `coverage_counts(...)`. `market_valuation_snapshots/repository.py:55-76` idiom 미러.
- **`freshness.py`** — `data_state` 분류(§7).
- **`derive.py`** (순수 함수, **미배선**) — per-period 행 + `report_date` → 8개 지표 + per-metric `data_state`. PIT 게이트(`filing_date<=report_date`), 음수/0 base 가드, missing≠zero, gross_margin IFRS 단일식 부재 시 `partial`.
- **`app/jobs/financial_fundamentals_snapshots.py`** — symbol 해석(`kr_symbol_universe.is_active`), 배치, dry-run-first + guards + idempotency dict(기존 job 패턴), 결과 dataclass(samples/distribution/warnings/committed).
- **`scripts/build_financial_fundamentals_snapshots.py`** — `--market kr --limit N | --all`, `--annual-only`(기본)/`--with-quarterly`, **`args.dry_run = not args.commit`**(dry-run 기본), `_print_result()`.

## 6. 심볼 매핑

`kr_symbol_universe.symbol`(6자리 PK) → `OpenDartReader.find_corp_code`(6자리/사명/8자리 corp_code 수용) → corp_code. **신규 매핑 테이블 불필요**, 기존 `_get_client()` corp_codes 일일 캐시 재사용. 미해석 코드(상장폐지/특수)는 `find_corp_code` None → fail-open(행 skip, 날조 금지).

## 7. freshness / data_state (회계기간 grain)

- 0행/조회 실패 → `unavailable`.
- 행 존재하나 `filing_date IS NULL` → `partial`(공시일 미상 → read-path에서 미인용).
- 최신 `period_end_date`가 직전 정기보고 기대 분기보다 오래됨 → `stale`(연 4회 발표 cadence 기준; 임계는 plan에서 확정, `now` 주입으로 테스트 결정성).
- 그 외 → `fresh`.
- gross_margin: gross_profit·cost_of_sales 모두 NULL(IFRS 단일식) → 해당 지표만 `partial`(행 전체는 정상).

## 8. 테스트 (TDD)

1. **builder 파싱**: `finstate_all` 픽스처 → revenue/net_income/gross_profit/roe; account_id 우선·account_nm fallback; gross_profit 부재 시 NULL(cost_of_sales fallback 표식).
2. **분기 누적 차분**: Q1/반기/Q3/사업보고서 누적 픽스처 → `discrete_*` 정확(Q3=Q3누적−반기누적); annual행 discrete=원값.
3. **PIT 조인**: `rcept_no→rcept_dt` 매핑 성공→`filing_date` 세팅; 조인 실패→`filing_date=NULL`(`partial`).
4. **repository**: fake/in-memory로 upsert 멱등(동일 (market,symbol,fiscal_period,source) 재실행=update), `latest_periods_for_symbols` 반환.
5. **derive (핵심)**: report_date 게이트 — `report_date < filing_date`인 행은 미가시(지표 `unavailable`); `>=`면 가시. `revenue_growth_3y_avg`/`earnings_*` 계산값; 음수 base 가드; `earnings_increase_streak`/배당 streak 카운트; **missing≠zero**(미신고 배당→빈 시리즈→streak 미산출, 0-streak 날조 금지); gross_margin IFRS 단일식→`partial`; dividend_growth split-미조정 caveat 표식.
6. **freshness**: 당분기→fresh; 오래됨→stale; filing_date NULL→partial; 0행→unavailable.
7. **CLI dry-run**: `--commit` 없으면 write 0(샘플/분포만 출력), `--commit`만 repository upsert.
8. **회귀**: `market_valuation_snapshots`/`fundamentals_evidence.py` 경로 무변경(full-3 preset 비회귀); 신규 source CHECK가 기존 테이블에 영향 없음.

## 9. Migration 동기

- additive: 신규 테이블 1개 생성(`alembic revision --autogenerate` 후 검토). `down_revision` = 구현 시점 `alembic heads` 실값(2-head 시 merge 우선). **operator가 `alembic upgrade head` 별도 실행**(PR1은 apply 안 함).
- `app/models/financial_fundamentals_snapshot.py` ORM 신규.
- `INVEST_DATA_SOURCE_CONTRACT`(`invest_data_source_contract.py`)에 `financial_fundamentals_snapshots` 엔트리 추가 검토(authority_tier 등) — contract drift-guard 테스트 충족. doc matrix 재렌더 필요 시 `docs/invest/data-source-contract.md` 동기.

## 10. 후속 (PR2/PR3 — 이 spec 범위 아님)

- **PR2 (read-path/catalog)**: `derive.py`를 screener read-path에 배선 → 4 missing preset(저평가 성장주/돈 잘버는 회사/미래의 배당왕/안정 성장주) 구현 + `cheap_value`/`steady_dividend` partial 보강 + `oversold_recovery`/`growth_expectation` mismatch 의미 정정. `parityStatus`/`parityNote`/`dataState` 정직 갱신. `docs/invest-screener-toss-parity-matrix.md` 갱신.
- **PR3 (consumption)**: `screen_stocks(market="kr")`·`/invest/reports` 후보에 `candidate_data_state`/`candidate_toss_parity_status`/missing-condition note 보존(ROB-363).
- **operator-gated(별도)**: 프로덕션 backfill(연간 우선 페이싱) + scheduler/Prefect 등록.

## 11. 비목표 (YAGNI)

- KIS/Naver 재무 배선(DART로 충분; KIS는 PR2+ 선택적 cross-check, Naver는 회피).
- 집계 지표 컬럼 저장(lookahead 위험 — read-path 파생).
- 별도 dividend 테이블(단일 테이블로 충분).
- US 적재(컬럼은 market-agnostic이나 PR1은 kr만; US는 Finnhub `filed_date` 보유로 후속).
- screener/리포트/프론트 배선, 프로덕션 backfill, scheduler 활성화.
