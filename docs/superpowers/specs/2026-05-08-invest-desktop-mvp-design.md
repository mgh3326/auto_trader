# /invest Desktop MVP — ROB-141 / 142 / 143 / 144

**Date:** 2026-05-08
**Branch:** `rob-142144-ai-mvp`
**Linear issues:**
- ROB-141 — `/invest` 웹 공통 RightAccountPanel
- ROB-142 — `/invest/feed/news` 데스크톱 뉴스 피드 MVP
- ROB-143 — `/invest/signals` 데스크톱 AI 시그널 MVP
- ROB-144 — `/invest/calendar` 데스크톱 증시 캘린더 + AI 요약 MVP

## 1. Goal & non-goals

**Goal.** Toss Invest 웹 IA를 참고삼아 `/invest`에 데스크톱 read-only 투자 surface를 추가한다. 4개 화면이 공유하는 3-column shell + RightAccountPanel을 한 번에 만들고, news / signals / calendar 페이지를 그 위에 얹는다. 1 spec → 1 plan → 1 PR.

**Non-goals (이번 PR 범위 밖, follow-up):**
- broker 주문 submit/cancel/modify/replace, watch-order-intent, 모든 mutation
- 새 news/signals/event/AI ingestion 파이프라인 (기존 데이터만 화면화)
- 새 `report_type='weekly_summary'` 생성 — 기존 daily brief를 주간 범위로 조합만
- 매수/매도 CTA — disabled 버튼조차 두지 않음 (read-only 링크만)
- 관심 종목 add/edit/remove UI — read-only 표시만
- 모바일 `/invest/app` 레이아웃 리팩터링 (회귀 방지만)
- 디자인 시스템 도입(CSS-in-JS 등). 기존 inline + CSS variables 패턴 유지.

## 2. Routing & SPA 구조

### 2.1 Backend mount

새 라우터 `app/routers/invest_web_spa.py` 추가:
- prefix `/invest`, SPA fallback 전용 (read-only HTML/asset)
- `/invest/api/*`(`invest_api.router`)와 `/invest/app/*`(`invest_app_spa.router`)는 **이미 등록된 상태에서** 그 다음에 등록 → FastAPI route ordering으로 shadow 방지
- 추가 방어: catch-all 핸들러가 `path.startswith("api/") or path.startswith("app/")` 이면 404 반환
- broker / watch / redis / kis / upbit / task-queue import 금지 → `tests/test_invest_web_spa_router_safety.py` 추가 (기존 SPA safety test와 동일 패턴)

`app/main.py` 등록 순서:
```python
app.include_router(invest_api.router)        # /invest/api/*  (already first)
app.include_router(invest_app_spa.router)    # /invest/app/*  (already)
app.include_router(invest_web_spa.router)    # /invest/{path}  (NEW, last)
```

### 2.2 Frontend bundle

`frontend/invest/`(단일 Vite 번들)을 확장:
- React Router basename: `/invest/app` → **`/invest`**
- 모바일 routes: `/app/`, `/app/paper`, `/app/discover`, `/app/discover/issues/:id` 등 (기존 path 앞에 `/app` prefix 추가)
- 데스크톱 routes (신규):
  - `/` — DesktopHomePage (랜딩, MVP에서는 RightAccountPanel + summary placeholder)
  - `/feed/news` — DesktopFeedNewsPage
  - `/signals` — DesktopSignalsPage
  - `/calendar` — DesktopCalendarPage
- `*` fallback은 데스크톱 root `/`로 redirect

`frontend/invest-web/`, `/invest/web`, `/invest/app/mobile`은 만들지 않는다.

### 2.3 Layout 분리 원칙

모바일 surface는 **그대로 둔다** — 데스크톱 페이지가 모바일 컴포넌트를 강제로 재사용하지 않는다. 단, 다음 공통 자산은 추출/재사용:
- `src/types/invest.ts` 등 view-model 타입
- `src/api/*.ts` HTTP fetch wrapper (새 endpoint마다 추가)
- `src/format/*.ts` (currency / percent / relative time)
- 계좌 데이터 shaping 헬퍼 (account source → 색상/배지 매핑은 데스크톱 전용으로 신규 추가)
- 기존 `useAuth`/`useFetch` 류 훅이 있다면 공유

데스크톱 전용:
- `src/desktop/DesktopShell.tsx` — 3-column grid (left nav / center content / right RightAccountPanel)
- `src/desktop/RightAccountPanel.tsx` — sticky panel
- `src/desktop/components/*` — 데스크톱 전용 카드/리스트
- `src/pages/desktop/*` — 4개 페이지

모바일 전용:
- 기존 `src/components/*`, `src/pages/HomePage.tsx`, `DiscoverPage.tsx` 등은 `src/mobile/`로 이동하지 않고 **현 위치에서 그대로 동작**시킨다 (테스트 회귀 최소화). 후속 정리는 별 issue.

## 3. Backend view-model API

모든 신규 endpoint는 `/invest/api/*` namespace, GET only, `get_authenticated_user` 의존, `extra="forbid"` Pydantic.

### 3.1 `GET /invest/api/account-panel`

RightAccountPanel용 view-model. 내부적으로 `InvestHomeService.get_home()` + `user_watch_items` 조회.

**Response shape (camelCase, 신규 schema `app/schemas/invest_account_panel.py`):**
```python
class AccountPanelResponse(BaseModel):
    homeSummary: HomeSummary           # 재사용 (invest_home에서 import)
    accounts: list[Account]            # 재사용
    groupedHoldings: list[GroupedHolding]  # 재사용
    watchSymbols: list[WatchSymbol]    # 신규
    sourceVisuals: list[AccountSourceVisual]  # 신규 (FE가 색/배지 lookup)
    meta: AccountPanelMeta
```

`WatchSymbol` 최소 필드: `symbol`, `market`, `displayName`, `note`. (가격/desired_buy_px 등 mutation 관련 필드는 view-model에 노출하지 않음.)

`AccountSourceVisual`: `source`, `tone` (`navy|gray|purple|green|dashed`), `badge` (`Live|Mock|Crypto|Paper|Manual`), `displayName`. 서버에서 한 번 결정해 내려보내면 FE가 일관된 색을 쓰기 쉬움.

### 3.2 `GET /invest/api/feed/news`

ROB-142 — 자체 수집 뉴스 + 이슈 묶음 + 보유/관심 relevance.

**Query params:**
- `tab`: `top|latest|hot|holdings|watchlist|kr|us|crypto` (기본 `top`)
- `limit`: 기본 30, 최대 100
- `cursor`: 다음 페이지용 (서버 발급, opaque string)

**Response (`app/schemas/invest_feed_news.py`):**
```python
class FeedNewsResponse(BaseModel):
    tab: str
    asOf: datetime
    issues: list[FeedIssueCard]        # 상단 hot 이슈 묶음 (best-effort)
    items: list[FeedNewsItem]
    nextCursor: str | None
    meta: FeedNewsMeta                  # warnings, empty 사유

class FeedNewsItem(BaseModel):
    id: int                             # news_articles.id
    title: str
    publisher: str | None               # NewsArticle.source
    feedSource: str | None
    publishedAt: datetime | None
    market: Literal["kr","us","crypto"]
    relatedSymbols: list[NewsRelatedSymbol]   # symbol, market, displayName
    issueId: str | None                 # market_issue cluster id (있으면)
    summarySnippet: str | None          # NewsAnalysisResult.summary가 있으면, 없으면 NewsArticle.summary
    relation: Literal["held","watchlist","both","none"]
```

**구현 노트:**
- `news_articles` 직접 쿼리 + `news_analysis_results.summary`/`sentiment` LEFT JOIN
- `issues`는 `build_market_issues()`(기존 ROB-130) 재사용
- `holdings`/`watchlist` 필터는 user의 held + watch symbol 집합을 한 번 가져와서 service layer에서 in-memory match (per-row JOIN 금지)
- `tab=holdings` 인데 holdings 없으면 `meta.empty_reason="no_holdings"` 명시
- `tab=watchlist` 인데 watchlist 없으면 `meta.empty_reason="no_watchlist"` 명시 — 보유 기반 relevance는 별도 카드에서 계속 동작

### 3.3 `GET /invest/api/signals`

ROB-143 — AI 분석 + 이슈 + 시장 brief 조합.

**Query params:**
- `tab`: `mine|kr|us|crypto` (기본 `mine` = 보유+관심)
- `limit`: 기본 20

**Response (`app/schemas/invest_signals.py`):**
```python
class SignalsResponse(BaseModel):
    tab: str
    asOf: datetime
    items: list[SignalCard]
    meta: SignalsMeta

class SignalCard(BaseModel):
    id: str                             # 합성 (e.g., "analysis:{stock_analysis_results.id}")
    source: Literal["analysis","issue","brief"]
    title: str
    market: Literal["kr","us","crypto"]
    decisionLabel: Literal["buy","hold","sell","watch","neutral"] | None
    confidence: int | None              # 0-100
    severity: Literal["low","medium","high"] | None
    summary: str | None
    generatedAt: datetime
    relatedSymbols: list[NewsRelatedSymbol]
    relatedIssueIds: list[str]
    supportingNewsIds: list[int]
    rationale: str | None               # detailed_text 또는 reasons join
    relation: Literal["held","watchlist","both","none"]
```

**구현 노트:**
- 주된 source는 `stock_analysis_results` (latest per symbol)
- `market_issues`는 source=`issue`인 카드로 섞어서 노출 (cluster-level signal)
- `market_reports.daily_brief`는 source=`brief`인 single card (오늘자만)
- 상세 패널은 list/detail split — 라우트는 `/signals?selected=<id>` query param으로 (별도 route 안 만듦)

### 3.4 `GET /invest/api/calendar` + `/invest/api/calendar/weekly-summary`

ROB-144.

**`GET /invest/api/calendar`:**
- params: `from_date`, `to_date`, `tab` (`all|economic|earnings|disclosure|crypto`)
- 내부: `MarketEventsQueryService.list_for_range()` + portfolio relevance overlay
- 우선순위/limit: 한 날에 raw earnings 200개라면 cluster (예: "S&P500 어닝 23건" 묶음 + top 5만 펼침). cluster 임계값은 기본 10개.
- response: `app/schemas/invest_calendar.py`의 `CalendarResponse` — `days: list[CalendarDay]`, 각 day마다 `events: list[CalendarEvent]`, `clusters: list[CalendarCluster]`

```python
class CalendarEvent(BaseModel):
    eventId: str
    title: str
    market: Literal["kr","us","crypto","global"]
    eventType: Literal["earnings","economic","disclosure","crypto","other"]
    eventTimeLocal: datetime | None
    source: str
    actual: str | None
    forecast: str | None
    previous: str | None
    relatedSymbols: list[NewsRelatedSymbol]
    relation: Literal["held","watchlist","both","none"]
    badges: list[Literal["holdings","watchlist","major"]]
```

**`GET /invest/api/calendar/weekly-summary`:**
- params: `week_start` (월요일 ISO date)
- 내부: `market_report_service.get_market_reports(report_type=...)`로 해당 주 daily_brief / kr_morning / crypto_scan 조회 + 텍스트 결합/요약 (요약은 텍스트 concat + 헤딩, **request-time LLM 호출 없음**)
- 데이터가 부족한 날은 `partial=True` + `missingDates` 명시
- response: `WeeklySummaryResponse(weekStart, asOf, sections: list[WeeklySection], partial: bool, missingDates: list[date])`

### 3.5 공통 view-model 헬퍼

`app/services/invest_view_model/` (신규 패키지):
- `relation_resolver.py` — user_id → (held_symbols set, watchlist_symbols set) 한 번 캐시, `resolve_relation(symbol, market)` 헬퍼
- `account_visual.py` — source → tone/badge 매핑 (FE도 같은 매핑 받음)
- `news_query.py` — feed/news 용 쿼리 + DTO 변환
- `signals_query.py` — analysis_results 조회 + DTO 변환
- `calendar_query.py` — events + cluster 로직
- `weekly_summary_query.py` — market_reports composition

각 query 모듈은 `AsyncSession` + 사전 계산된 relation_resolver를 받음. 라우터는 thin.

## 4. Frontend 구성

### 4.1 새 디렉토리 구조

```
frontend/invest/src/
├── api/
│   ├── investHome.ts            # 기존
│   ├── newsIssues.ts            # 기존
│   ├── marketEvents.ts          # 기존
│   ├── accountPanel.ts          # NEW
│   ├── feedNews.ts              # NEW
│   ├── signals.ts               # NEW
│   └── calendar.ts              # NEW
├── types/
│   ├── invest.ts                # 기존, AccountPanel/Watch/Visual 타입 확장
│   ├── newsIssues.ts            # 기존
│   ├── marketEvents.ts          # 기존
│   ├── feedNews.ts              # NEW
│   ├── signals.ts               # NEW
│   └── calendar.ts              # NEW
├── desktop/
│   ├── DesktopShell.tsx         # 3-column grid + sticky right panel
│   ├── DesktopHeader.tsx        # 상단 nav (홈 / 뉴스 / 시그널 / 캘린더)
│   ├── RightAccountPanel.tsx    # 공통 panel
│   ├── AccountSourceTone.ts     # source → color/badge 룩업
│   ├── components/
│   │   ├── feed/                # NewsCard, IssueCluster, FilterTabs
│   │   ├── signals/             # SignalListItem, SignalDetail
│   │   └── calendar/            # WeekRail, EventRow, EventCluster, WeeklySummaryModal
├── pages/
│   ├── desktop/
│   │   ├── DesktopHomePage.tsx
│   │   ├── DesktopFeedNewsPage.tsx
│   │   ├── DesktopSignalsPage.tsx
│   │   └── DesktopCalendarPage.tsx
│   └── (기존 모바일 페이지 그대로)
└── routes.tsx                    # basename 변경 + 데스크톱 routes 추가
```

### 4.2 RightAccountPanel UX

- sticky, 우측 280–320px 폭, viewport height
- 상단: 합산 total (KRW 기준), pnlRate · pnlKrw
- accounts 리스트 (source별 tone/badge): 색상은 `AccountSourceTone.ts`의 단일 source-of-truth
  - `kis` (live) → `--accent-navy`, `Live` badge
  - `kis_mock`, `kiwoom_mock` → `--muted-gray`, `Mock` badge
  - `upbit` → `--accent-purple`, 24h 변동률 노출
  - `alpaca_paper` → `--accent-green`, 전략명 badge (없으면 `Paper`)
  - `toss_manual`, `pension_manual`, `isa_manual`, `db_simulated` → 점선 border, `Manual` badge
- 보유 종목 요약: 상위 N개 (5) + "더보기"는 home 링크
- 관심 종목 섹션: watchSymbols.length === 0이면 explicit empty state (CTA 없음)
- 계좌 source가 응답에 아예 없으면 그 source 카드는 렌더하지 않는다. 응답에는 있는데 holdings/value가 0이면 카드는 렌더하되 본문에 "잔고 없음" empty state. API warnings는 카드 안 inline 표시.
- 민감 정보(계좌번호 전체)는 노출 금지 — 표시는 마스킹된 displayName만 (이미 backend에서 마스킹됨)

`account selector`(계좌 단일 선택 토글)와 `asset category filter`(KR/US/Crypto)는 패널 내부에서 별도 구간으로 명확히 분리한다 — 한 줄에 섞지 않는다.

### 4.3 페이지별 UX 메모

**DesktopFeedNewsPage:**
- left col: tabs (주요/최신/핫이슈/보유/관심) + market filter (KR/US/Crypto)
- center: top에 issue cluster strip (issues 있으면) + 그 아래 news list
- 카드 hover 시 oneline summary 노출. 클릭 시 RightAccountPanel은 sticky로 유지하고 center column에서 in-place expand (선택된 카드 아래에 본문/relatedSymbols/sentiment 펼침). 모달은 쓰지 않는다.
- 무한 스크롤은 안 넣고 cursor pagination "더 보기" 버튼

**DesktopSignalsPage:**
- left col: signal list (tab 필터 + 최신순)
- center: 선택된 signal detail — title / decisionLabel / confidence / summary / rationale / 관련 뉴스 / 관련 종목
- 매수/매도 버튼 없음. `리서치 보기 / 관련 뉴스 / 저널 보기 / 상세 분석 / 포지션 보기` 같은 read-only 링크만 (해당 데이터가 있으면 활성, 없으면 비노출)
- 선택 안 됨 → empty default ("시그널을 선택하세요")

**DesktopCalendarPage:**
- top: 주간 date rail (월~금 + 주말) + 선택 날짜
- main: 선택 날짜의 events list (cluster된 earnings는 collapse됨, 펼치기 버튼)
- right: RightAccountPanel
- 상단 우측: "이번주 AI 요약" 카드 → 클릭 시 `WeeklySummaryModal`
- partial 데이터일 때 explicit indicator + missingDates 표시

### 4.4 스타일

기존 `src/styles.css`의 CSS variables (`--surface`, `--accent`, `--muted` 등) 재사용. 새 토큰 추가:
- `--accent-navy`, `--accent-purple`, `--accent-green`, `--muted-gray`, `--surface-paper`
- breakpoint: 데스크톱은 `min-width: 1024px` 가정. 그 이하에서는 모바일 layout으로 떨어지지 않고 데스크톱 grid가 single-column으로 stack (RightAccountPanel은 하단으로). 진정한 반응형은 후속.

## 5. Data flow & relation 계산

요청당 단 한 번 user의 held + watchlist symbol 집합을 가져온다:

```
Request handler
  → resolver = await build_relation_resolver(db, user_id)
  → resolver.held: set[(market, symbol)]
  → resolver.watch: set[(market, symbol)]
  → resolver.relation(market, symbol) -> "held" | "watchlist" | "both" | "none"

  → query_service.fetch_data(...)
  → for row in rows: row.relation = resolver.relation(...)
```

held는 `InvestHomeService`(또는 그 내부 reader들이 쓰는 holdings) 결과에서 추출. watch는 `user_watch_items` JOIN `instruments`.

심볼 정규화는 기존 `app/core/symbol.py` (`to_db_symbol`) 사용. KIS 보유는 `BRK.B` 형식이고 watch는 instruments 테이블 거치므로 양쪽 정규화 필수.

## 6. Error / loading / empty 상태

각 view-model 응답은 `meta`에 `warnings: list[{source, code, message}]`를 둔다. FE는:
- API 5xx → 페이지 단위 error banner + retry 버튼
- 빈 결과 → meta.empty_reason 또는 데이터 없음 안내 (탭별 메시지 차별화)
- loading → skeleton (RightAccountPanel은 항상 skeleton 우선 — 4개 페이지 공유)
- partial weekly-summary → "이번주 일부 brief가 없습니다" + missingDates

## 7. Safety & 안전 경계

- `app/routers/invest_web_spa.py`는 broker / kis / upbit / redis / task / watch_order_intent import 금지. `tests/test_invest_web_spa_router_safety.py`(신규) 강제.
- `app/routers/invest_api.py` 안에 추가되는 핸들러도 같은 import 제약. 기존 `tests/test_invest_api_router_safety.py`의 forbidden prefix 리스트가 다음을 모두 커버하는지 plan 단계에서 확인하고 빠진 항목은 추가: `app.services.kis*`, `app.services.upbit*`, `app.services.brokers*`, `app.services.order_service*`, `app.services.fill_notification`, `app.services.execution_event`, `app.services.redis_token_manager`, `app.services.kis_websocket*`, `app.tasks*`.
- `app/services/invest_view_model/*`도 broker / order / mutation 경로 import 금지. 신규 safety test 1개 추가 (`test_invest_view_model_safety.py`).
- Toss API, Toss cookie, Toss token 코드 0줄 — grep CI 가드는 추가하지 않지만 PR 본문에 명시.
- 개인 계좌번호/민감 식별자: backend는 이미 displayName 마스킹. 새 필드에서도 raw account_no 노출 금지.

## 8. 테스트

**Backend (pytest):**
- `tests/test_invest_account_panel_router.py` — 200/401/빈 응답
- `tests/test_invest_feed_news_router.py` — tab별 필터, holdings/watchlist empty state, cursor pagination
- `tests/test_invest_signals_router.py` — analysis_results+issue+brief 조합, tab 필터
- `tests/test_invest_calendar_router.py` — date range, cluster threshold, relation
- `tests/test_invest_calendar_weekly_summary_router.py` — partial / missingDates, no LLM call
- `tests/test_invest_web_spa_router_safety.py` — forbidden import 가드
- `tests/test_invest_view_model_safety.py` — view_model 패키지 forbidden import
- `tests/test_invest_view_model_relation_resolver.py` — held/watch/both/none 매핑 단위 테스트

**Frontend (vitest):**
- `RightAccountPanel.test.tsx` — source별 tone/badge, watchSymbols empty, accounts empty
- `DesktopShell.test.tsx` — 3-column grid 렌더, sticky panel slot
- `DesktopFeedNewsPage.test.tsx` — tab 전환, empty state, issue strip
- `DesktopSignalsPage.test.tsx` — list/detail 선택, CTA 버튼 없음 단언
- `DesktopCalendarPage.test.tsx` — date rail, cluster 펼침, weekly summary modal
- `routes.test.tsx` — basename `/invest`, `/`/`/feed/news`/`/signals`/`/calendar`/`/app/*` 라우팅 모두 확인
- 기존 모바일 테스트는 basename + path 변경 따라 1줄씩 fix (`/invest/app/...` → 그대로 유지, basename만 `/invest` + path `/app/*` 으로 표현)

**Lint / typecheck:**
- `make lint` (Ruff + ty), `make typecheck`
- `pnpm --filter @auto-trader/invest typecheck && pnpm --filter @auto-trader/invest test`

## 9. 회귀 방지 체크리스트

- 기존 `/invest/app/*` 모바일 라우트 모두 동작 (HomePage/PaperPlaceholderPage/DiscoverPage/DiscoverIssueDetailPage)
- 기존 `GET /invest/api/home` 응답 변경 없음
- 기존 `/trading/api/news-issues`, `/trading/api/market-events/*` 변경 없음 (read-only consumer만 추가)
- 기존 모바일 테스트(`HomePage.test.tsx` 등) 통과
- `make test` 전체 통과, frontend `pnpm test` 통과
- broker/order/watch mutation 경로는 어느 신규 파일에서도 import되지 않음 (safety tests)

## 10. 구현 순서 가이드 (writing-plans가 plan으로 구체화)

1. backend safety test scaffolding (`invest_web_spa_router_safety`, `invest_view_model_safety`)
2. `relation_resolver` + `account_visual` 헬퍼 + 단위 테스트
3. `/invest/api/account-panel` schema + service + router + 테스트
4. `frontend/invest` basename 변경 + 모바일 path `/app/*` 이전 + 기존 테스트 fix
5. `DesktopShell` + `RightAccountPanel` + `account-panel` API hook + 컴포넌트 테스트
6. `/feed/news` backend + frontend (가장 큰 surface) — issue cluster 포함
7. `/signals` backend + frontend
8. `/calendar` backend + `/calendar/weekly-summary` + frontend (cluster + modal)
9. SPA fallback 라우터 (`invest_web_spa`) + safety test 통과 확인
10. PR 본문: ROB-141~144 모두 close, 회귀 체크리스트, 안전 경계 명시

## 11. Open questions / future follow-ups

- 데스크톱 브레이크포인트(`min-width: 1024px`) 미만 처리는 stacked fallback만 — 진짜 반응형은 별 issue
- weekly summary를 진짜 LLM-summarize하려면 새 `report_type='weekly_summary'` 파이프라인 — 별 issue (ROB-144는 composition만)
- 매수/매도 / preview / journal-link / approval-workflow CTA — 별 issue로 각 범위 결정 후 도입
- 모바일 surface를 `src/mobile/`로 옮기는 정리 — 별 issue
- Toss UI 시각 디테일(애니메이션, micro-interaction)은 디자인 follow-up
