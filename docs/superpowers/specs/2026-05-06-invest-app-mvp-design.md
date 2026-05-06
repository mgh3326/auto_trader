# ROB-123 — `/invest/app` 토스식 모바일 투자 앱 MVP

- Linear: <https://linear.app/mgh3326/issue/ROB-123/auto-trader-investapp-토스식-모바일-투자-앱-mvp>
- Status: design ready for implementation plan
- Author: Claude (paired with robin)
- Date: 2026-05-06

## Background

기존 `/trading/decisions/*` (React SPA) 와 `/portfolio/decision` (Jinja) 은 운영자/리서치/의사결정 콘솔 성격이다. ROB-123은 사용자-facing 투자 앱 화면을 별도 제품면으로 시작한다. URL은 `/invest/app` (모바일), `/invest/web` 은 예약. 토스증권 모바일 앱을 참고한 read-only 통합 홈 MVP.

기존 read-only API 와 `MergedPortfolioService` 검증 결과:

- `/portfolio/api/merged` 는 KIS + manual(toss) holdings 를 *종목 단위* 로 머지하지만, **Upbit 보유분이 빠져 있고**, 계좌별 합계/현금/buyingPower 도 별도 노출되지 않는다.
- `BrokerAccount.is_mock` 으로 mock/paper 분리는 가능. `BrokerType` enum 은 `kis | toss | upbit | samsung` 이며, 본 MVP 가 정의하는 source 코드(`pension_manual` 등)는 enum 에 추가하지 않고 frontend 타입에서만 정의한다(이번 범위에서 DB 변경 없음).

따라서 본 이슈에서 read-only `/invest/api/home` 1개 신규를 추가한다. 기존 콘솔/Jinja 화면은 건드리지 않고 병행 유지한다.

## Goals

- `/invest/app` URL 에 토스식 모바일 read-only 통합 홈 화면 1개를 띄운다.
- 모바일 viewport 에서 자연스럽게 보이고, 데스크톱에서는 mobile-width app shell 로 보여도 OK.
- 기본 합산 대상은 **KIS 실계좌 · Upbit · Toss 수동** 3개. 퇴직연금/ISA/모의투자는 합산 제외.
- 주문/취소/정정/broker mutation/scheduler/DB write/realtime 은 전부 비범위(read-only).
- 기존 `/trading/decisions/*` 와 `/portfolio/*` 화면은 깨지지 않는다.

## Non-Goals

- `/invest/web` 데스크톱 클론
- 자동 mobile/desktop 리다이렉트
- `/trading/decisions/*` / Jinja 페이지 제거
- 사용자별 includedInHome 설정 UI (이번 MVP 는 백엔드 하드코딩)
- placeholder 탭(관심/발견/피드)의 실제 페이지
- `/invest/app/paper/*` 모의투자 화면 완성 (route 만 예약)
- 같은 종목 머지 토글 settings UI (구조만 열어둠)
- 주문/취소/정정/매수 클릭 액션 — buyingPower 는 표시 only
- realtime quote/websocket/chart, scheduler/worker 변경, DB migration/backfill/update/delete

## URL & Routing

| URL | 무엇 |
|---|---|
| `/invest/app/` | SPA index → `<HomePage />` (통합 홈) |
| `/invest/app/paper` | placeholder ("준비 중") |
| `/invest/app/paper/{kis-mock,kiwoom-mock,alpaca-paper,db-simulated}` | 같은 placeholder 컴포넌트, source 만 다름 |
| `/invest/app/*` (그 외) | SPA fallback → react-router 404 |
| `/invest/api/home` | `GET` only, read-only JSON |

- FastAPI catch-all SPA fallback 으로 새로고침/딥링크 모두 `index.html` 서빙 → react-router 가 처리.
- vite dev proxy: `/invest/api`, `/portfolio/api`, `/trading/api`, `/api`, `/auth` → `:8000`.
- 인증은 `/trading/decisions/` 와 동일한 cookie session pass-through. 별도 모델/미들웨어 변경 없음.

## Architecture

```
frontend/invest/                             ← 신규 Vite + React 19 + react-router-dom 7 패키지
  package.json (name: @auto-trader/invest)
  vite.config.ts  base: "/invest/app/"
                  proxy: /invest/api, /portfolio/api, /trading/api, /api, /auth → :8000
  index.html
  src/
    main.tsx, routes.tsx, App.tsx
    pages/HomePage.tsx
    pages/PaperPlaceholderPage.tsx           ← /paper, /paper/* 예약 라우트
    components/AppShell.tsx
    components/HeroCard.tsx
    components/AccountCardList.tsx
    components/SourceFilterBar.tsx
    components/HoldingRow.tsx
    components/BottomNav.tsx
    api/investHome.ts                        ← fetch wrapper (read-only)
    types/invest.ts                          ← §"Frontend types" 참고
    format/{currency,percent,number}.ts
    hooks/useInvestHome.ts
    __tests__/...

app/routers/invest_app_spa.py        ← SPA shell (broker/watch/redis/kis/upbit/task-queue import 금지)
app/routers/invest_api.py            ← read-only, prefix="/invest/api"; GET /home
app/services/invest_home_service.py  ← KIS holdings + Upbit balances + manual(toss) holdings 합성
app/schemas/invest_home.py           ← Pydantic response 모델
```

`invest_app_spa.py` 는 `app/routers/trading_decisions_spa.py` 를 그대로 미러링 (prefix="/invest/app", DIST=`frontend/invest/dist`, 동일한 build-missing 503 페이지). Safety pytest 도 미러링하여 broker/watch/redis/kis/upbit/task-queue 모듈 import 회귀를 막는다.

`invest_api.py` 와 `invest_home_service.py` 도 동일한 import safety 규칙. mutation 모듈은 import 하지 않으며 read-only service 만 의존:

- 기존 `MergedPortfolioService` (read-only 메서드만)
- 기존 `ManualHoldingsService.get_holdings_by_user` 등 read-only 만
- KIS/Upbit 클라이언트는 read-only 호출(잔고/시세 조회)만 사용

## `includedInHome` 정책 (백엔드 단일 소스, 하드코딩)

```python
# app/services/invest_home_service.py
HOME_INCLUDED_SOURCES: frozenset[str] = frozenset({"kis", "upbit", "toss_manual"})
# 합산 제외(이번 MVP): pension_manual, isa_manual, kis_mock, kiwoom_mock, alpaca_paper, db_simulated
```

`Account.includedInHome` 은 위 정책에서 파생. 향후 user_settings 도입 시 이 함수만 교체.

## Frontend types (`src/types/invest.ts`)

```ts
export type AccountKind = "live" | "manual" | "paper";

export type AccountSource =
  | "kis" | "upbit" | "toss_manual"
  | "pension_manual" | "isa_manual"
  | "kis_mock" | "kiwoom_mock" | "alpaca_paper" | "db_simulated";

export type Market = "KR" | "US" | "CRYPTO";
export type AssetType = "equity" | "etf" | "crypto" | "fund" | "other";

export interface Account {
  accountId: string;
  displayName: string;
  source: AccountSource;
  accountKind: AccountKind;
  includedInHome: boolean;
  valueKrw: number;
  costBasisKrw: number | null;
  pnlKrw: number | null;
  pnlRate: number | null;
  cashBalances: { krw?: number; usd?: number };
  buyingPower: { krw?: number; usd?: number };
}

export interface Holding {
  holdingId: string;
  accountId: string;
  source: AccountSource;
  accountKind: AccountKind;
  symbol: string;
  market: Market;
  assetType: AssetType;
  displayName: string;
  quantity: number;
  averageCost: number | null;
  costBasis: number | null;
  currency: "KRW" | "USD";
  valueNative: number | null;
  valueKrw: number | null;
  pnlKrw: number | null;
  pnlRate: number | null;
}

export interface GroupedHolding {
  groupId: string; // `${market}:${assetType}:${currency}:${normalized_symbol}`
  symbol: string;
  market: Market;
  assetType: AssetType;
  displayName: string;
  currency: "KRW" | "USD";
  totalQuantity: number;
  averageCost: number | null;   // 총 cost basis / 총 수량 (둘 다 있을 때만)
  costBasis: number | null;
  valueNative: number | null;
  valueKrw: number | null;
  pnlKrw: number | null;
  pnlRate: number | null;
  includedSources: AccountSource[];
  sourceBreakdown: Array<
    Pick<
      Holding,
      | "holdingId"
      | "source"
      | "accountId"
      | "quantity"
      | "averageCost"
      | "costBasis"
      | "valueNative"
      | "valueKrw"
      | "pnlKrw"
      | "pnlRate"
    >
  >;
}

export interface HomeSummary {
  includedSources: AccountSource[];
  excludedSources: AccountSource[];
  totalValueKrw: number;        // 투자 평가금액 합계만 (현금/buyingPower 제외)
  costBasisKrw: number | null;
  pnlKrw: number | null;
  pnlRate: number | null;
}

export interface InvestHomeWarning {
  source: AccountSource;
  message: string;
}

export interface InvestHomeResponse {
  homeSummary: HomeSummary;
  accounts: Account[];
  holdings: Holding[];           // raw, source 필터용
  groupedHoldings: GroupedHolding[]; // 전체 필터용
  meta?: {
    warnings?: InvestHomeWarning[];
  };
}
```

## API contract — `GET /invest/api/home`

- Auth: 기존 `get_authenticated_user` 의존.
- Method: `GET` only. 다른 메서드는 라우트하지 않음.
- Response: `200 InvestHomeResponse` (위 타입과 동일한 Pydantic schema).
- 부분 실패 정책: 어떤 source(예: Upbit) 호출이 실패해도 전체 API 를 실패시키지 않는다.
  - 가능한 source 의 데이터는 `accounts` / `holdings` / `groupedHoldings` 에 그대로 포함.
  - 실패 source 는 `accounts` 에서 제외하고 `holdings` / `groupedHoldings` 에도 해당 source 데이터를 넣지 않는다.
  - `meta.warnings` 에 `{source, message}` 항목을 추가한다.
  - `homeSummary` 는 실제 표시된 holdings 기준으로 일관되게 계산 (실패 source 의 추정값을 끼워 넣지 않는다).
- 에러: 인증 실패 401, 인증된 사용자가 계좌 0개여도 200 + 빈 배열 + 빈 summary (`totalValueKrw: 0`, costBasis/pnl null 가능).

### Hero 의미 (homeSummary)

`Account.valueKrw` 는 그 계좌의 보유 평가금액 합계 (현금/buyingPower 제외) 로 정의한다. 이를 기반으로:

- `totalValueKrw = Σ accounts[i].valueKrw` (`includedInHome === true` 인 계좌만). 현금/buyingPower 는 포함하지 않는다.
- `costBasisKrw = Σ accounts[i].costBasisKrw` (포함 계좌 모두 not null 일 때만). 하나라도 null 이면 `costBasisKrw = null`.
- `pnlKrw = totalValueKrw - costBasisKrw` (둘 다 not null), else `null`.
- `pnlRate = pnlKrw / costBasisKrw` (둘 다 not null, `costBasisKrw > 0`), else `null`.

향후 "총 자산 = 투자 평가금액 + 현금" 같은 별도 metric 이 필요해지면 다른 필드로 추가한다 (이번 MVP 범위 외).

## Grouping 규칙

`groupId` = `${market}:${assetType}:${currency}:${normalized_symbol}`.

- `normalized_symbol` 은 `trim().toUpperCase()` 만 적용. 서로 다른 시장의 종목을 억지로 매핑하지 않는다.
- KR equity, US equity, crypto 는 서로 머지하지 않는다 (market 또는 assetType 이 다르므로 자동 분리).
- currency 가 다르면 머지하지 않는다.
- crypto 는 `assetType="crypto"` 라 주식과 절대 같은 그룹이 되지 않는다.

같은 그룹 안 raw holding 들에 대한 합산:

- `totalQuantity = Σ quantity`
- `costBasis = Σ costBasis` (그룹 내 모든 holding 의 costBasis 가 not null 일 때만), else `null`
- `averageCost = costBasis / totalQuantity` (둘 다 not null), else `null`
- `valueNative = Σ valueNative` (모두 not null), else `null` — 같은 currency 안에서만 합산되므로 안전
- `valueKrw = Σ valueKrw` (모두 not null), else `null`
- `pnlKrw = valueKrw - costBasisKrw_group` (둘 다 not null), else `null`
- `pnlRate = pnlKrw / costBasisKrw_group` (둘 다 not null, costBasis>0), else `null`

이 계산은 **화면 표시용**이며 어떠한 DB write/backfill/update 도 발생시키지 않는다.

## UI

### Hero

- 상단 헤더 (서비스명/계좌 ▼/검색 affordance, MVP 에서는 정적 placeholder 가능)
- Hero 카드: "내 투자 (KIS · Upbit · Toss 수동)" 라벨 + 큰 KRW 평가금액 + 손익/수익률 한 줄 + 원금 sub line

### 계좌 카드 (가로 스크롤)

각 카드는 메인 영역(평가금액 + 수익률) + 하단 2단 grid(현금/매수가능). KIS/Upbit 에는 live badge 없음. Toss 만 차분한 `수동` badge.

| 계좌 | 메인 | 하단 grid |
|---|---|---|
| KIS 실계좌 | valueKrw, pnl/pnlRate | 원화 현금, 달러 현금, 원화 매수가능, 달러 매수가능 |
| Upbit | valueKrw, pnl/pnlRate | 원화 현금, 원화 매수가능 |
| Toss 수동 | valueKrw, pnl/pnlRate (가능 시) | 현금/매수가능 없으면 표시 안 하거나 `-` fallback |

### Source 필터

`전체 / KIS / Upbit / Toss 수동` 4개 chip. 상태 = `activeSource: AccountSource | "all"`.

### Holdings list

- `activeSource === "all"` → `groupedHoldings.map(GroupedRow)`
  - 행 안에 `includedSources` 를 작은 chip(`KIS · Toss` 같이) 로 표시
  - 같은 종목이 여러 source 에 있으면 1행으로 합산
- `activeSource !== "all"` → `holdings.filter(h => h.source === activeSource).map(RawRow)`
  - source 의 raw 값(quantity, averageCost, valueNative, pnl, pnlRate) 그대로 표시

### 하단 탭

placeholder 4개(증권/관심/발견/피드). MVP 에서는 클릭 시 `"준비 중"` 토스트만, 실제 라우트는 비워둔다.

### Fallback / 빈 상태 / 에러

| 케이스 | UI |
|---|---|
| 숫자 `null` | `-` |
| 평단/수익률 `null` 인 행 | sub line 에 `원금 정보 부족` 한 줄 |
| API 200 + accounts 빈 배열 | "연결된 계좌가 없습니다" + 기존 `/portfolio/` deeplink |
| API 5xx / 네트워크 실패 | "잠시 후 다시 시도해 주세요" + 재시도 버튼 (refetch 만, mutation 없음) |
| 부분 실패 (`meta.warnings`) | 상단 또는 list 위에 노란 경고 1줄 |
| SPA 빌드 누락 | `trading_decisions_spa` 와 동일한 build-missing 503 페이지 |

`buyingPower` 는 **display only**. 클릭 핸들러 없음. 매수/주문 플로우로 연결하지 않는다.

## Safety / Out-of-Scope (재확인)

- read-only.
- 주문 제출/취소/정정 금지.
- broker mutation 금지.
- watch/order-intent mutation 금지.
- scheduler/worker 변경 금지.
- DB migration / backfill / update / delete 금지.
- live trading smoke 금지.
- realtime quote / websocket / chart 구현 금지.

이 안전 경계는 모듈별로 다음 import-safety pytest 로 강제한다:

- `app/routers/invest_app_spa.py` — SPA static serving only.
  - broker / watch / redis / kis / upbit / task-queue 모듈 import 금지.

- `app/routers/invest_api.py` — thin router.
  - `InvestHomeService` 만 의존한다.
  - broker client / KIS client / Upbit client 직접 import 금지.
  - order / watch / scheduler / mutation 경로 import 금지.

- `app/services/invest_home_service.py` — read-only 합성 서비스.
  - KIS / Upbit / manual(toss) holdings 의 read-only service / client / adapter 는 사용 가능.
  - mutation 경로 (`submit*`, `cancel*`, `modify*`, `place_order*`, watch, order-intent, scheduler / worker mutation) import 또는 호출 금지.
  - DB write / backfill / update / delete 금지 — read-only ORM/조회 쿼리만 허용.

Safety pytest 는 read-only 의존성 차단이 아니라 mutation 경로 차단에 초점을 맞춘다.

## 테스트 전략

| 레이어 | 도구 | 무엇 |
|---|---|---|
| Frontend unit | vitest + React Testing Library | grouping/filter 동작, fallback 렌더링, source 필터, buyingPower display only(클릭 핸들러 없음) |
| Frontend build | `tsc --noEmit && vite build` | typecheck + 빌드 통과 |
| Backend unit | pytest | `InvestHomeService` grouping 규칙 (머지 안전 조건 / null fallback / crypto 분리), `includedInHome` 정책 |
| Backend router safety | pytest | `invest_app_spa.py`, `invest_api.py`, `invest_home_service.py` 가 broker/watch/redis/kis/upbit/task-queue 모듈 import 안 함 |
| Backend integration | pytest | `GET /invest/api/home` 응답 schema 검증, 합산 제외 source 가 `homeSummary`/`accounts` 에 들어가지 않음, 부분 실패 시 `meta.warnings` 채워짐 |

## Acceptance Criteria

- `/invest/app` URL 에서 React 통합 홈 화면이 열린다.
- 모바일 viewport 에서 토스식 통합 홈 (Hero + 계좌 카드 + 필터 + 보유 리스트 + 하단탭) 이 자연스럽게 보인다.
- 기존 보유/포트폴리오 데이터를 read-only 로 표시하며, 데이터 부족/일부 API 실패 시 빈 화면 대신 fallback/안내를 보인다.
- KIS/Upbit/Toss 수동 합산이 Hero 와 계좌 카드에 일관되게 반영된다.
- 같은 실제 종목이 여러 source 에 있을 때, `전체` 필터에서 1행으로 머지되어 표시된다 (cost basis 부족 시 평단/수익률 null fallback).
- `KIS / Upbit / Toss 수동` 필터 선택 시 해당 source 의 raw row 만 표시된다.
- 매수 가능 금액은 표시만 되고, 클릭 액션이 연결되지 않는다.
- 기존 `/trading/decisions/*` 및 Jinja 페이지가 깨지지 않는다.
- TypeScript typecheck, frontend tests/build 통과.
- 신규 backend read-only service/router 의 targeted pytest 와 ruff 통과.

## Open assumptions

- 인증 모델은 `/trading/decisions/` SPA 와 동일한 cookie session pass-through. 별도 미들웨어 변경 없음.
- KIS 잔고/현금/buyingPower 조회는 기존 KIS 클라이언트의 read-only 엔드포인트를 사용. 응답 매핑은 구현 단계에서 finalize.
- Upbit 잔고/매수가능은 기존 Upbit 클라이언트의 read-only 엔드포인트 사용. 동일.
