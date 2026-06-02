# ROB-423 — invest_report get_news 기반 뉴스 citation 저장/표시 (실행 spec)

> 상태: spec 확정 (브레인스토밍 → /spec). 구현은 후속(plan → 구현).
> 검증: 아래 모든 코드 앵커는 2026-06-02 기준 15-에이전트 적대적 검증으로 확인됨.
> 로케이트 결정 4건은 `## 확정된 설계 결정` 참고.

## Context

`/invest/reports` 사용자가 "어떤 뉴스가 buy/sell/watch 판단에 영향을 줬는지" 확인할 수 있게,
report 생성 시 종목별 뉴스를 **on-demand로 가져오고 실제 판단에 쓰인 기사만 citation으로 영속화**한다.
ROB-398 검토에서 news-ingestor는 당분간 동결하고 invest_report 뉴스 근거는 `get_news` 경로를 쓰기로 정리됐다.
뉴스는 단독 매수/매도 신호가 아니라 가격/수급/보유/유동성/기술 지표를 보강·약화하는 overlay evidence로 취급한다.

## 확정된 설계 결정 (locked)

| # | 결정 | 선택 |
|---|---|---|
| D1 | citation 판단성 필드(`role`/`decision_impact`/`selection_reason`)와 "실제 사용됨" 판정 주체 | **Hermes 작성**. auto_trader는 결정적 fetch+evidence 제공 + fetch_run audit. 판단성 필드는 `create_from_hermes_composition` ingest 시 Hermes annotation으로만 작성. (no-internal-LLM 가드 준수) |
| D2 | invest_report 뉴스 evidence 소스 | **on-demand `get_news` 경로** (`symbol_news_service`, 종목별 실시간 fetch). 전역 archive 비의존. |
| D3 | 정규화 news seam 통합 범위 | **`symbol_news_service` 신설 + `get_news`도 경유**. public MCP news tool은 `get_news` 하나 유지. `llm_news_service`(DB)는 타 소비자용 불변. |
| D4 | citation 대상 리포트 | **Hermes 합성 advisory 중심**. mock_preview는 live citation을 참조/복사(재fetch/재판정 없음). 결정적 auto-emit(비-Hermes) 리포트는 fetch_run+evidence만. |

배포: **2-PR 분할**. PR1 = 정규화 seam(`symbol_news_service` + `get_news` 재배선 + collector 전환 + 회귀 테스트). PR2 = 2 테이블 + Hermes 스키마 + ingest 영속 + detail API + mock 복사 + smoke.

## Current State (검증일 2026-06-02, branch rob-423)

코드 대조로 확인한 사실. 일부는 원 이슈 본문 가정과 다르다.

| 항목 | 현재 상태 | 근거 (검증됨) |
|---|---|---|
| `get_news` public MCP tool | 존재. KR→Naver, US/crypto→Finnhub | `app/mcp_server/tooling/fundamentals_handlers.py:73-74` (`@mcp.tool(name="get_news"`), 핸들러 `fundamentals/_news.py:24` `handle_get_news`, dispatch `_news.py:45-47` |
| `get_news` 출력 envelope | 최상위 `{symbol, market, source, count, news}`; per-article `{title, source, datetime, url, summary, sentiment, related}` | shaping은 provider 측: `app/services/finnhub_news.py:85-91`, `app/mcp_server/tooling/fundamentals_sources_naver.py:24-30` |
| report의 뉴스 수집 | **이미 활성**. `NewsSnapshotCollector`가 derived focus symbols 받아 필터, soft-fail | `collectors/registry.py:290` production, `collectors/news.py:99-114` 필터, `generator.py:327-341` derive→ensure |
| report 뉴스 **소스** | get_news가 아니라 **DB `news_articles`**(뉴스스탠드/RSS) via `llm_news_service.get_news_articles` | `collectors/registry.py:237-273` `_build_news_fetch_fn`, type `collectors/news.py:38` `NewsFetchFn = Callable[[str,int,int], ...]` (market-scoped) |
| 정규화 경로 | **3개 공존**: `fundamentals/_news.py`(get_news), `research_news_service`(NormalizedArticle), `llm_news_service`(DB) | 3 files |
| 판단성 citation | 없음. citation은 snapshot `payload_json`에 암묵 임베드 | `investment_snapshots.payload_json` |
| in-process LLM | `investment_stages/` + `action_report/snapshot_backed/` 전체 금지(가드) | `tests/services/action_report/snapshot_backed/test_no_internal_llm_imports.py:62-65` |
| Hermes 합성 | Hermes가 `HermesCompositionResult.items` 작성→`create_from_hermes_composition`가 검증+영속 | `hermes_ingest.py:413`, ingest는 `ingestion.py:246` `insert_report`(flush+refresh `repository.py:43-44`) |
| Hermes 스키마 확장성 | `HermesCompositionResult.model_config = ConfigDict(extra="forbid")` (`:122`)지만 default 있는 optional 필드는 additive 안전. `metadata:dict`(`:133`), `cited_snapshot_uuids`(`:134`), `dimension_report_uuids`(`:142`) 존재 | `app/schemas/hermes_composition.py` |
| report 모델 | `review.investment_reports.report_uuid`(PG_UUID, unique, default uuid4). item은 `report_id`(BigInt FK)+`item_uuid` | `models/investment_reports.py:121-127, 297-304` |
| detail API | `GET .../investment-reports/{report_uuid}` → `InvestmentReportBundle`. `service.get_bundle()`→`_serialise_bundle` | `routers/investment_reports.py:199-217` |
| `InvestmentReportBundle` | optional `review_sections`(`:827`), `action_packet`(`:830`) → 추가 optional 필드 additive 가능 | `schemas/investment_reports.py:806-830` |
| `research_news_service` | `fetch_symbol_news(symbol, instrument_type, *, limit=20, timeout_s=5.0) -> list[NormalizedArticle]`. NormalizedArticle 6필드(url,title,source,summary,published_at,provider). fail-soft. **external_article_id/related_symbols/market/symbol/fetched_at/provider_metadata 없음** | `app/services/research_news_service.py:17-24, 89-112` |
| **alembic head** | **`20260602_rob412_main_merge` (단일 head)** | `alembic heads` 실측. ⚠ 원 가정 `rob337_rob403_merge_heads`는 main 전진으로 superseded |

**핵심 함의**
1. "get_news를 report에 배선"은 실제로는 report 뉴스 소스를 **DB archive → on-demand per-symbol fetch로 전환**하는 일이다.
2. `role`/`decision_impact`/`selection_reason`/"실제 사용됨"은 LLM 판단이라 가드상 auto_trader가 만들 수 없다 → **Hermes가 composition에 실어 보내고, ingest 시점에만 citation row 작성**.
3. 신규 테이블/스키마 확장/detail 필드는 전부 additive로 안전하게 얹힌다.

## Proposed Change

### 데이터 흐름

```
[bundle prep · 결정적 · report_uuid 아직 없음]
  derive focus symbols (symbol_derivation.derive: seed+holdings+journals+watch+candidates)
    → (개편) NewsSnapshotCollector: symbol별 symbol_news_service.fetch_symbol_news()
    → news snapshot payload_json 적재:
        articles[]      (external_article_id, canonical_url, title, source, published_at, summary, provider, symbol, related_symbols)
        fetch_records[] (symbol, provider, requested_limit, returned_count, status, error_code)
  → Hermes context export 가 articles[] 노출

[Hermes 합성 · 레포 밖 LLM]
  HermesCompositionResult.items + news_citations[]  ← Hermes가 사용한 기사만 annotation
        (article ref by external_article_id|canonical_url + relevance/role/decision_impact/selection_reason/confidence
         + optional item ref / section_key)

[report 생성 · create_from_hermes_composition · insert_report 직후 report_uuid 확정]
  InvestmentReportNewsService.persist(report_uuid, news_snapshot, hermes_news_citations):
    1) fetch_runs 작성(report_uuid 키): returned_count=수집분, used_count=매칭된 Hermes citation 수
    2) Hermes citation을 bundle news snapshot articles와 매칭(external_article_id/canonical_url)
       - unknown ref → drop + fetch_run.status/unavailable 기록 (fail-open, 날조 금지)
    3) citations 작성(매칭/사용분만): 기사 스냅샷 필드 복사 + Hermes 판단 필드
  ↑ ingest_composition 내부, insert_report(ingestion.py:246) 이후 같은 트랜잭션에서 호출

[mock_preview · runner.run() · live bundle 재사용]
  run()이 live report+items 로드(runner.py:83-90) → mock report 영속 후
  live report citation을 mock report_uuid로 복사 (재fetch·재판정 없음)
```

### 1. 저장 모델 (신규 2테이블, `review` 스키마, additive/nullable/append-only)

`app/models/investment_reports.py`에 추가, `app/models/__init__.py` import + `__all__` 등록.

```sql
-- review.investment_report_news_fetch_runs : 소형 audit (fetched-but-unused 전체 저장 안 함)
id              BIGSERIAL PK
run_uuid        UUID UNIQUE NOT NULL
report_uuid     UUID NOT NULL              -- review.investment_reports.report_uuid 논리참조(멤버십). FK는 report_id(int) 패턴과 불일치라 비강제
market          TEXT NOT NULL
symbol          TEXT NOT NULL
instrument_type TEXT NOT NULL
provider        TEXT NOT NULL              -- 'get_news'|'naver_finance'|'finnhub'
requested_limit INT  NOT NULL
returned_count  INT  NOT NULL DEFAULT 0    -- 수집 시점
used_count      INT  NOT NULL DEFAULT 0    -- Hermes citation 매칭 수
fetched_at      TIMESTAMPTZ NOT NULL
freshness_policy TEXT NULL
ttl_seconds     INT NULL
status          TEXT NOT NULL CHECK (status IN ('ok','empty','unavailable','error'))
error_code      TEXT NULL
error_message   TEXT NULL                  -- secret-free
raw_response_stored BOOLEAN NOT NULL DEFAULT false
created_at      TIMESTAMPTZ NOT NULL DEFAULT now()

-- review.investment_report_news_citations : 실제 사용된 기사 snapshot만
id               BIGSERIAL PK
citation_uuid    UUID UNIQUE NOT NULL
report_uuid      UUID NOT NULL
report_item_uuid UUID NULL                 -- 특정 buy/sell/watch item에 붙는 경우
section_key      TEXT NULL                 -- report-level 인용. 'news_overview'|'risk_overlay' 등
fetch_run_id     BIGINT NULL REFERENCES review.investment_report_news_fetch_runs(id)
market           TEXT NOT NULL
symbol           TEXT NOT NULL
provider         TEXT NOT NULL
external_article_id TEXT NULL              -- Naver officeId:articleId / Finnhub id|url-hash
canonical_url    TEXT NOT NULL
source_name      TEXT NULL
title            TEXT NOT NULL
summary_snapshot TEXT NULL                 -- ≤1000자 truncate
published_at     TIMESTAMPTZ NULL
fetched_at       TIMESTAMPTZ NOT NULL
relevance        TEXT NOT NULL CHECK (relevance IN ('direct','related','market_context','crypto_context'))
role             TEXT NOT NULL CHECK (role IN ('catalyst','risk','confirmation','contradiction','neutral','noise'))
decision_impact  TEXT NOT NULL CHECK (decision_impact IN ('strengthen_buy','weaken_buy','strengthen_sell','weaken_sell','hold_watch','no_action'))
selection_reason TEXT NULL
confidence       NUMERIC NULL
metadata_json    JSONB NOT NULL DEFAULT '{}'::jsonb   -- safe metadata only, no secrets/raw dump
created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
```

마이그레이션: `alembic/versions/20260602_rob423_add_investment_report_news_tables.py`,
**down_revision = `20260602_rob412_main_merge`** (구현 시점 `uv run alembic heads`로 재확인; main 전진 시 갱신).
operator가 별도 `alembic upgrade head` 실행(prod cutover gate).

### 2. Service boundary — 단일 정규화 seam (D3)

`app/services/research_news_service.py` → `app/services/symbol_news_service.py`로 승격(내부 service, public MCP 미노출).

```python
@dataclass(frozen=True)
class SymbolNewsArticle:
    provider: str; market: str; symbol: str
    external_article_id: str | None        # 신규: Naver officeId:articleId / Finnhub id|url-hash
    title: str; source_name: str | None; canonical_url: str
    summary: str | None; published_at: datetime | None; fetched_at: datetime
    related_symbols: list[str]; provider_metadata: dict   # secret-free

@dataclass(frozen=True)
class SymbolNewsFetchResult:
    articles: list[SymbolNewsArticle]
    status: str            # ok|empty|unavailable|error
    provider: str; requested_limit: int; returned_count: int
    error_code: str | None

async def fetch_symbol_news(symbol, market, instrument_type, *, limit=20, timeout_s=5.0) -> SymbolNewsFetchResult
    # provider dispatch: KR→Naver, US→Finnhub, crypto→Finnhub
    # fail-soft: timeout/예외 → status=error, articles=[]
    # provider seam은 닫지 않음(추후 KR Naver-JSON / US+Naver보조 / crypto+Finnhub보조). 보조 구현은 범위 밖.
```

- `get_news` MCP 핸들러(`fundamentals/_news.py`)를 `symbol_news_service` 경유로 재배선.
  **출력 envelope는 그대로** 보존: 최상위 `{symbol, market, source, count, news}`, per-article `{title, source, datetime, url, summary, sentiment, related}` (회귀 테스트로 고정).
- `NewsSnapshotCollector` fetch fn을 `llm_news_service.get_news_articles`(DB, market-scoped) → `symbol_news_service.fetch_symbol_news`(per-symbol on-demand)로 교체.
  시그니처가 `(market,hours,limit)` → per-symbol로 바뀌므로 collector가 focus symbols를 루프. `fetch_records[]`를 news snapshot payload에 기록.
- **Blast radius 확인됨(V14)**: market-scoped `NewsFetchFn`의 유일 소비자는 `NewsSnapshotCollector`(`registry.py:316`). 그 외 `llm_news_service.get_news_articles` 직접 호출처(`routers/news_analysis.py:146`, `mcp_server/tooling/news_handlers.py:98`, `services/news_radar_service.py:310`, `services/n8n_news_service.py:88`)는 **무영향**.

### 3. Report generation mapping (D1, D4)

- 수집 evidence는 위 흐름대로 Hermes context에 `articles[]`로 제공.
  (news snapshot articles가 `HermesContextPayload`에 아직 노출 안 되면 additive optional 필드 추가; `extra="forbid"`라도 default 있으면 안전.)
- `HermesCompositionResult`(`schemas/hermes_composition.py`)에 additive 필드:
  ```python
  news_citations: list[HermesNewsCitation] = Field(default_factory=list)
  # HermesNewsCitation: external_article_id|canonical_url(ref), symbol, relevance, role,
  #   decision_impact, selection_reason?, confidence?, report_item_ref?(item index/uuid), section_key?
  ```
  default empty → 기존 Hermes client 미전송 시 back-compat (extra="forbid" 무관).
- `app/services/investment_stages/hermes_ingest.py` `ingest_composition`에서 `insert_report`(ingestion.py:246) 이후
  `InvestmentReportNewsService.persist`로 fetch_runs+citations 영속(가드 준수: LLM 호출 없음, 순수 검증+DB write).
- fetch 실패/empty는 report 생성을 막지 않음 → fetch_run.status + `unavailable_sources`에 사유.
- mock_preview(`MockPreviewReportRunner.run`, runner.py:83-90 live 로드)는 mock report 영속 후 live citation을 mock report_uuid로 복사.

### 4. Detail API

`InvestmentReportBundle`(`schemas/investment_reports.py:806`)에 additive:
```python
news_citations: list[InvestmentReportNewsCitationResponse] = []   # review_sections/action_packet 옆
```
`service.get_bundle()`/`_serialise_bundle`에서 report_id로 citations 로드(`routers/investment_reports.py:199-217` 경로).
응답 필드 최소: `title, source_name, canonical_url, published_at, provider, symbol, role, decision_impact, relevance, summary_snapshot, report_item_uuid?, section_key?`.
list API는 미포함(detail만 필수).

## Acceptance Criteria

1. 마이그레이션이 `review.investment_report_news_fetch_runs` + `review.investment_report_news_citations`를 추가하고, 기존 report와 nullable/append-only 관계다. down_revision은 구현 시점 단일 head.
2. report 생성 시 selected symbol에 대해 `symbol_news_service`(get_news와 동일 경로)로 on-demand 뉴스를 가져와 Hermes context evidence에 포함한다.
3. public MCP 종목/뉴스 tool surface는 `get_news` 하나로 유지(신규 public MCP news tool 0). `symbol_news_service`/mapping helper는 내부 service.
4. `get_news` MCP 출력 envelope(최상위·per-article 키)는 재배선 후에도 불변(회귀 테스트 통과).
5. citation은 **Hermes가 composition에서 사용 표시한 기사만** 저장. fetched-but-unused 전체 목록 미저장. 전역 `symbol_news_articles` archive 테이블 미도입.
6. `role`/`decision_impact`/`selection_reason`/`confidence`는 Hermes annotation에서만 채워지고, auto_trader 코드 경로는 in-process LLM 미호출(`test_no_internal_llm_imports` 가드 통과).
7. Hermes가 bundle에 없는 기사를 인용하면 drop하고 fetch_run.status/`unavailable_sources`에 기록(fail-open, 날조 0).
8. fetch 실패/empty 시 report 생성 fail-open, fetch_run status 또는 `unavailable_sources`에 사유.
9. detail API가 citation 목록을 반환하고, 각 citation에 `title, source_name, canonical_url, published_at, provider, symbol, role, decision_impact` 포함.
10. mock_preview report는 live citation을 복사하며 재fetch/재판정하지 않는다.
11. `NewsSnapshotCollector`가 `symbol_news_service` 단일 경로를 쓰고 중복 fetch/normalize 로직을 만들지 않는다.
12. advisory-only focused smoke로 report 생성 후 detail에서 참고 뉴스 citation 확인.

## Testing Plan

| Layer | What | Count |
|---|---|---|
| Unit | `symbol_news_service.fetch_symbol_news` 정규화/external_article_id/fail-soft(timeout·error·empty) | +5 |
| Unit | `get_news` envelope 불변(재배선 회귀) | +2 |
| Unit | `InvestmentReportNewsService.persist` 매칭/unknown-ref drop/used_count/truncate(≤1000) | +5 |
| Unit | NewsSnapshotCollector per-symbol fetch + fetch_records 기록 | +3 |
| Unit | no-internal-LLM 가드 (기존 parametrized가 신규 모듈 자동 커버) | 0 |
| Integration | Hermes composition(news_citations) → ingest → fetch_runs+citations 영속 | +2 |
| Integration | empty/failure → fail-open, report 생성 성공 + unavailable 기록 | +2 |
| Integration | global archive write 0 (news_articles INSERT 없음) | +1 |
| Integration | detail API citation 노출; mock_preview citation 복사 | +2 |
| Smoke | advisory-only report 생성 + detail citation 확인(default-off, operator) | +1 |

## Files Reference

| File | Change |
|---|---|
| `app/models/investment_reports.py` | `InvestmentReportNewsFetchRun`, `InvestmentReportNewsCitation` ORM 추가 |
| `app/models/__init__.py:16` | 두 모델 import + `__all__` 등록 |
| `alembic/versions/20260602_rob423_add_investment_report_news_tables.py` | 신규 마이그레이션 (down=`20260602_rob412_main_merge`) |
| `app/services/symbol_news_service.py` | `research_news_service` 승격 + `SymbolNewsArticle`/`SymbolNewsFetchResult` + external_article_id + provider seam |
| `app/mcp_server/tooling/fundamentals/_news.py` | `symbol_news_service` 경유 재배선(출력 envelope 불변) |
| `app/services/finnhub_news.py`, `fundamentals_sources_naver.py` | envelope shaping 유지/연계(키 보존) |
| `app/services/action_report/snapshot_backed/collectors/registry.py:237-273,316` | `_build_news_fetch_fn` per-symbol on-demand로 교체 |
| `app/services/action_report/snapshot_backed/collectors/news.py:38,99-114` | `NewsFetchFn` per-symbol + `fetch_records[]` 기록 |
| `app/schemas/hermes_composition.py:122` | `HermesCompositionResult.news_citations` additive + `HermesNewsCitation` |
| `app/services/investment_stages/hermes_ingest.py:413` | `ingest_composition`에서 `insert_report` 이후 `InvestmentReportNewsService.persist` |
| `app/services/investment_reports/investment_report_news_service.py` | (신규) fetch_runs+citations write/repository |
| `app/services/investment_reports/mock_preview/runner.py:83-90` | `run()`에서 live citation → mock 복사 |
| `app/schemas/investment_reports.py:806` | `InvestmentReportBundle.news_citations` + response 스키마 |
| `app/routers/investment_reports.py:199-217` | detail 핸들러 citation 로드 |

## Rollback Plan

마이그레이션 additive(2 신규 테이블, 기존 컬럼 무변경) → `alembic downgrade -1`로 drop. 코드는 PR revert.
Hermes `news_citations` 미전송 시 citations 0건(report 정상). collector 소스 전환 회귀 시 `_build_news_fetch_fn`을 DB 경로로 되돌리는 1줄.

## Effort Estimate

symbol_news_service 승격 + external_article_id + get_news 재배선 ~3h / collector per-symbol 전환 ~2h /
2 모델+마이그레이션 ~1.5h / Hermes 스키마+ingest 영속 ~3h / detail API+mock 복사 ~2h / 테스트 ~4h / smoke+runbook ~1.5h.
합계 ~17h. **PR1**(symbol_news_service+get_news 재배선+collector 전환, 회귀) ≈ 7h / **PR2**(테이블+Hermes+ingest+detail+mock+smoke) ≈ 10h.

## Out of Scope / Non-goals

news-ingestor 수정·스케줄, 전 종목 pre-crawl/backfill, 모든 fetched article 저장, 전역 `symbol_news_articles` archive,
provider 보조 구현, 신규 public MCP news tool, `get_symbol_news_mapping` public 노출, scheduler,
broker/order/watch/order-intent mutation, prod DB backfill, secret 출력/저장.
\+ 결정적 auto-emit(비-Hermes) report의 판단성 citation(Hermes 합성 report만 대상).

## Related

ROB-398(get_news 역할 정리), ROB-287(Hermes 합성·no-internal-LLM), ROB-373(mock/live 분리),
ROB-366 B8(현 news collector), ROB-140/207(research_reports, 별개 시스템).

## Verification note

2026-06-02 15-에이전트 적대적 검증(read-only) 결과: 13/15 confirmed, 2 수정 반영 —
(V11) alembic head `rob337_rob403_merge_heads` → `20260602_rob412_main_merge`,
(V13) mock_preview citation 복사는 `_project()`가 아니라 `run()`(live 로드 위치).
설계-결정적 항목(V6 report_uuid 생성 시점, V7 Hermes 스키마 additive 안전, V14 collector blast-radius 무영향)은 전부 confirmed.
