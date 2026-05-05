# ROB-115 — Research Pipeline 종목별 on-demand 뉴스 수집 + Social 페이지 제거

- Linear: <https://linear.app/mgh3326/issue/ROB-115/auto-trader-research-pipeline-종목별-on-demand-뉴스-수집-social-페이지-제거>
- Status: design ready for implementation plan
- Author: Claude (paired with robin)
- Date: 2026-05-05

## Background

ROB-112/113/114에서 Research Pipeline과 5개 stage 페이지 (`summary`, `market`, `news`, `fundamentals`, `social`)가 운영에 들어갔다. 운영 확인 결과:

- 005930 / AMZN Research Session 페이지는 정상 접근 가능하지만 `news` 데이터가 비어 있다.
- DB의 `news_articles`에 `stock_symbol`이 채워진 row가 0건 (kr 815, us 2170, crypto 284 모두 unsymbolized RSS).
- `social` 페이지는 placeholder이고 TradingAgents upstream도 실질 source 없음.

이번 이슈는 그래서 다음을 한다.

1. Research Session 실행 시점에 해당 종목만 on-demand 뉴스 수집 (전체 RSS universe-wide tagging은 안 함).
2. `social` stage/페이지/route를 제품 표면에서 제거 (과거 row 호환성은 유지).

## Goals

- `NewsStageAnalyzer`가 DB에 종목별 뉴스가 부족할 때 KR=Naver, US=Finnhub provider로 종목 단위 뉴스를 가져와 사용한다.
- 가져온 기사는 `news_articles`에 `stock_symbol` / `stock_name` 채워서 영속화 (URL dedupe 존중).
- `social` stage/route/탭을 제품 표면에서 제거하되 기존 데이터/스키마 호환성은 깨지 않는다.
- AMZN / 005930 Research Session에서 `news` 페이지가 실제 headline 기반 signals를 보여준다.

## Non-Goals

- Reddit/X/StockTwits/네이버 종목토론실 등 진짜 social 수집.
- 전체 RSS 기사에 대한 universe-wide stock_symbol 매칭/태깅.
- 운영 DB backfill / destructive migration.
- broker / order / watch / order-intent / scheduler / 자동매매 변경.
- yfinance를 이번 PR에 새 primary source로 추가하지 않는다 (필요 시 후속 fallback 후보).
- Crypto 종목별 on-demand news (후속 이슈로 분리).

## Architecture

### 새 모듈: `app/services/research_news_service.py`

Provider-agnostic interface. MCP/tooling layer에 의존하지 않고, 오히려 기존 `app/mcp_server/tooling/fundamentals/_news.py::handle_get_news`가 점진적으로 이 service에 위임하는 방향.

```python
@dataclass
class NormalizedArticle:
    url: str
    title: str
    source: str | None        # provider site (e.g., "Reuters", "한국경제")
    summary: str | None
    published_at: datetime | None
    provider: str             # "naver" | "finnhub"


async def fetch_symbol_news(
    symbol: str,
    instrument_type: str,
    *,
    limit: int = 20,
    timeout_s: float = 5.0,
) -> list[NormalizedArticle]:
    """
    Routes to KR (Naver) or US (Finnhub) and normalizes the response.

    Returns [] on:
    - timeout
    - missing API key (Finnhub)
    - network/parse failure
    - unsupported instrument_type (crypto -> [] for now)

    Never raises to the caller — research pipeline must degrade gracefully.
    """
```

내부 라우팅:

- `equity_kr` → `naver_finance.fetch_news(code, limit)` 결과를 `NormalizedArticle`로 정규화. `provider="naver"`.
- `equity_us` → `app.mcp_server.tooling.fundamentals_sources_finnhub._fetch_news_finnhub(symbol, "us", limit)` 결과를 정규화. `provider="finnhub"`.
- `crypto` 또는 unknown → 빈 list (이번 이슈 범위 외).

각 호출은 `asyncio.wait_for(..., timeout=timeout_s)`로 감싼다. 모든 예외는 catch하여 `logger.warning` + 빈 list 반환.

> Finnhub helper가 `app/mcp_server/tooling/`에 있는 게 layering 상 어색하지만, 이번 PR에서는 이동시키지 않고 service에서 import해서 호출만 한다 (이동은 별도 리팩토링 이슈로). 다만 service는 절대 MCP 패키지에 의존성 노출 안 함 — import는 service 내부에서만.

### `NewsStageAnalyzer` 흐름 변경

```
async def analyze(ctx):
    market = _market_from_instrument(ctx.instrument_type)

    # 1. DB lookup
    db_articles, _ = await get_news_articles(
        stock_symbol=ctx.symbol, market=market, hours=24, limit=20
    )

    # 2. On-demand fetch if below threshold
    MIN_THRESHOLD = 3
    if len(db_articles) < MIN_THRESHOLD:
        normalized = await fetch_symbol_news(
            ctx.symbol, ctx.instrument_type, limit=20
        )
        if normalized:
            await _persist_normalized_articles(
                normalized,
                symbol=ctx.symbol,
                stock_name=ctx.symbol_name,  # may need to plumb through StageContext
                market=market,
            )
            # Refetch to merge fresh-fetched + any pre-existing rows
            db_articles, _ = await get_news_articles(
                stock_symbol=ctx.symbol, market=market, hours=24, limit=20
            )

    # 3. Compute sentiment / themes from db_articles (existing logic unchanged)
    raw = _compute_news_signals(db_articles)

    # 4. Build StageOutput
    if raw["headline_count"] == 0:
        verdict = StageVerdict.NEUTRAL  # not UNAVAILABLE — pipeline still healthy
        # add stale_flags via SourceFreshness
    else:
        verdict = _verdict_from_sentiment(raw)
    return StageOutput(...)
```

핵심 원칙: **fetch 실패는 pipeline을 죽이지 않는다.** Stage는 NEUTRAL + warning으로 degrade.

### 영속화 (`_persist_normalized_articles`)

기존 `app/services/llm_news_service.py::bulk_create_news_articles`를 재사용. 호출 전에 `NormalizedArticle` → ingestor-shaped object로 변환:

```python
class _OnDemandArticlePayload:
    url: str
    title: str
    content: None
    summary: str | None
    source: str | None
    author: None
    stock_symbol: str           # ← 핵심: symbol-tagged
    stock_name: str | None
    published_at: datetime | None
    market: str                 # "kr" | "us"
    feed_source: str            # "research_on_demand_naver" | "research_on_demand_finnhub"
    keywords: None
```

`feed_source`를 `research_on_demand_*` 별도 namespace로 분리해서 기존 RSS 기반 dashboard (news_radar, preopen briefing)에 끼어들지 않도록 한다. URL dedupe는 `bulk_create_news_articles`의 기존 로직 그대로.

### `StageContext`에 `symbol_name` 추가 검토

현재 `StageContext`는 `symbol`만 가지고 있고 `name`이 없다. `news_articles.stock_name` 채우려면 plumbing 필요. Pipeline에서 이미 `name` 인자를 받고 있으므로 `StageContext`에 `symbol_name: str | None` 필드 추가하고 pipeline에서 set.

## Social 제거 전략

### Backend

- `app/analysis/pipeline.py::analyzers` list에서 `SocialStageAnalyzer()` 제거 → 신규 세션은 social row 생성하지 않음.
- `app/analysis/stages/social_stage.py` — **유지** (legacy 재실행 / 마이그레이션 안전성). 파일 상단에 deprecation 주석 한 줄.
- `app/schemas/research_pipeline.py`의 `Literal["market", "news", "fundamentals", "social"]` (3 군데) — **그대로 유지**. 과거 row가 schema를 통과하지 못하면 안 됨.
- `SocialSignals` Pydantic 모델 — 유지.
- `build_summary` (debate.py)는 stage 개수 가정 안 함 (`stage_outputs` dict iteration), 3-stage에서 정상 동작 확인됨.

### Frontend (`frontend/trading-decision/src/`)

**제거**:

- `routes.tsx`의 `social` route + `ResearchSocialPage` import → 직접 `/research/sessions/:id/social` 접근 시 catch-all `<ResearchSessionNotFoundPage />`로 폴백 (`{ path: "*", element: <ResearchSessionNotFoundPage /> }` 이미 존재).
- `pages/research/ResearchSessionLayout.tsx::STAGE_NAV`에서 `{ to: "social", ... }` 항목 제거.
- `pages/research/ResearchSocialPage.tsx`, `components/ResearchSocialTab.tsx` — 파일 삭제.

**유지** (legacy 호환):

- `api/types.ts::StageType` Literal에 `"social"` 유지. `SocialSignals` interface 유지.
- `components/CitedStageSidebar.tsx`의 `stage?.stage_type === "social"` 분기 유지 (과거 row가 sidebar에 인용될 때 안전 fallback).
- `i18n/ko.ts`의 `RESEARCH_TAB_LABEL.social = "소셜"` 유지 (CitedStageSidebar fallback 라벨).
- `test/fixtures/research.ts`의 social fixture 유지 (legacy 시나리오 테스트).

**테스트 변경**:

- `__tests__/routes.test.tsx`: 기존 "registers /research/sessions/:sessionId/social stage route" 테스트는 "/social URL이 not-found 처리되거나 STAGE_NAV에 없음"을 검증하는 식으로 교체.
- `__tests__/ResearchSessionRoutes.test.tsx`: "renders the social placeholder at /social" 테스트 삭제. 대신 "navigates to /summary by default and STAGE_NAV does not include social" 추가.
- 기존 4개 page (summary/market/news/fundamentals) 테스트는 그대로 유지.

## Error Handling

| 시나리오 | 동작 |
| --- | --- |
| Naver scraping 실패 (network/parse) | `logger.warning`, fetch는 `[]` 반환. DB에 기존 row 있으면 그걸로 분석, 없으면 NEUTRAL + `news_fetch_unavailable` warning. |
| Finnhub `FINNHUB_API_KEY` 없음 / quota | `ValueError`/`finnhub.FinnhubAPIException` catch → `[]` 반환. 동일하게 graceful degrade. |
| `asyncio.wait_for` timeout (5s) | catch → `[]` 반환. |
| `bulk_create_news_articles`가 모두 dedupe되어 0건 inserted | 정상. refetch 결과로 진행. |
| 전체 stage가 raise해도 | `pipeline.py`의 `asyncio.gather(..., return_exceptions=True)` + 기존 try/except가 흡수. session은 여전히 finalize 가능. |

## Testing Plan

### Backend (pytest)

- `tests/services/test_research_news_service.py`
  - KR symbol → Naver provider 호출, 정규화 결과 검증
  - US symbol → Finnhub provider 호출, 정규화 결과 검증
  - timeout / ImportError / API key 없음 / network exception → 빈 list 반환 (raise 안 함)
  - crypto / unknown instrument_type → `[]`
- `tests/test_news_stage_on_demand.py`
  - DB에 ≥ MIN_THRESHOLD row 존재 → fetch 호출 안 함 (mock으로 검증)
  - DB에 0 row → fetch 호출 → bulk_create_news_articles 호출 → refetch 결과로 signals 채워짐
  - fetch가 raise해도 stage NEUTRAL/UNAVAILABLE로 degrade, exception propagate 안 함
  - 동일 URL 중복 호출 시 dedupe 안전
- `tests/test_research_pipeline_no_social.py`
  - 신규 세션 실행 후 `stage_analysis`에 `stage_type="social"` row 미생성
  - 기존 social row가 DB에 있어도 `get_research_session` API/repo가 깨지지 않음

### Frontend (vitest)

- `__tests__/routes.test.tsx` — `/social` URL → not-found path 매칭 검증 (또는 STAGE_NAV exclusion 단언)
- `__tests__/ResearchSessionRoutes.test.tsx` — social placeholder 렌더링 테스트 삭제. STAGE_NAV에 social 미포함 단언 추가.
- 기존 ResearchSessionLayout / 4개 stage page snapshot 그대로 통과해야 함.

### Smoke (수동 / dev 환경)

- 005930 Research Session 생성 → `/summary`, `/market`, `/news`, `/fundamentals` 4페이지 모두 렌더 + `news`에 실제 headline 1개 이상.
- AMZN Research Session 동일 검증.
- `/research/sessions/:id/social` 직접 접근 → not-found UI.

## File Touch Surface (예상)

**신규**:

- `app/services/research_news_service.py`
- `tests/services/test_research_news_service.py`
- `tests/test_news_stage_on_demand.py`
- `tests/test_research_pipeline_no_social.py`

**수정**:

- `app/analysis/pipeline.py` — `SocialStageAnalyzer` 제거
- `app/analysis/stages/news_stage.py` — on-demand fetch + persist 흐름
- `app/analysis/stages/base.py` — `StageContext.symbol_name` 추가
- `app/analysis/stages/social_stage.py` — deprecation 주석
- `frontend/trading-decision/src/routes.tsx` — social route/import 제거
- `frontend/trading-decision/src/pages/research/ResearchSessionLayout.tsx` — STAGE_NAV에서 social 제거
- `frontend/trading-decision/src/__tests__/routes.test.tsx` — social route 테스트 교체
- `frontend/trading-decision/src/__tests__/ResearchSessionRoutes.test.tsx` — social placeholder 테스트 교체

**삭제**:

- `frontend/trading-decision/src/pages/research/ResearchSocialPage.tsx`
- `frontend/trading-decision/src/components/ResearchSocialTab.tsx`

**유지** (legacy 호환을 위해 손대지 않음):

- `app/schemas/research_pipeline.py` — `Literal[..., "social"]` 그대로
- `app/services/llm_news_service.py` — 기존 helper 재사용만
- `frontend/trading-decision/src/api/types.ts` — `SocialSignals`, `StageType` 유지
- `frontend/trading-decision/src/components/CitedStageSidebar.tsx` — social fallback 분기 유지
- `frontend/trading-decision/src/i18n/ko.ts` — `social: "소셜"` 라벨 유지
- `frontend/trading-decision/src/test/fixtures/research.ts` — social fixture 유지

## Acceptance Criteria

### Backend

- [ ] `NewsStageAnalyzer`가 DB에 종목별 뉴스가 부족할 때 on-demand fetch 수행
- [ ] AMZN session에서 news stage가 실제 headline ≥1 반영
- [ ] 005930 session에서 news stage가 실제 headline ≥1 반영
- [ ] fetch 실패/빈 결과/rate-limit 시 session 전체 실패 안 함
- [ ] URL dedupe 동작
- [ ] 신규/변경 테스트 포함 (3개 신규 테스트 파일)

### Frontend

- [ ] Research Session navigation에서 `social` 탭 제거
- [ ] `/summary`, `/market`, `/news`, `/fundamentals` 정상 동작
- [ ] 과거 social stage row 있는 세션 UI 깨지지 않음
- [ ] `/social` 직접 접근 정책: not-found (이 spec에서 결정)

### Smoke

- [ ] 005930 / AMZN Research Session에서 4 페이지 + news headline 표시 확인
- [ ] social 탭 미노출

## Safety / Out of Scope Reaffirmed

- broker / order / watch / order-intent / scheduler 코드 변경 없음
- DB destructive migration 없음 (alembic revision 0건)
- 운영 DB backfill 없음
- 외부 API key 코드/로그/이슈에 노출 안 함
- 새 source는 timeout + rate-limit + failure fallback 보유
