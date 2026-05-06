# ROB-127: /invest/app 토스 발견탭 스타일 뉴스 기반 AI 실시간 이슈 MVP — Design

- **Linear**: [ROB-127](https://linear.app/mgh3326/issue/ROB-127/auto-trader-investapp-토스-발견탭-스타일-뉴스-기반-ai-실시간-이슈-mvp)
- **Branch**: `auto-trader-investapp-ai-mvp` (현재 워크트리, 신규 브랜치 생성하지 않음)
- **Status**: Design approved (2026-05-07)
- **Owner**: 광현 (Claude Code) — Hermes/AoE 자동 구현 없음

## 1. 목적

`/invest/app`에 토스증권 `발견` 탭을 참고한 **read-only MVP**를 추가한다. 이번 이슈에서는 신규 AI/이벤트 생성 기능을 만들지 않고, 이미 존재하는 read-only 엔드포인트 `GET /trading/api/news-radar`를 단일 데이터 소스로 사용해 화면 뼈대를 우선 만든다.

## 2. 안전 경계 (Non-goals)

이번 이슈에서 **하지 않는다**:

- broker submit / cancel / modify / replace
- order preview / approval / order-intent / watch 생성
- scheduler / worker 활성화 또는 주기 변경
- DB migration / backfill / direct row update
- 신규 LLM 호출 / 새로운 AI 요약 생성 job
- 경제지표 / 실적 캘린더 provider 연동
- 실시간 websocket / chart 구현
- 뉴스 클러스터링 / 출처 병합 정확도 보장
- 관련 종목 자동 추천 / 분석 기능

신규 import 금지: `app/services/broker_*`, `app/services/order_*`, `app/services/watch_*`, `app/scheduler/*`, `app/workers/*`, `app/services/llm_*` 류.

## 3. 데이터 소스

### 3.1 백엔드 엔드포인트

`GET /trading/api/news-radar?market=all&hours=24&include_excluded=true&limit=20`

- **인증**: 기존 invest 화면과 동일하게 cookie 기반 (`credentials: "include"`).
- **응답 스키마**: `app/schemas/news_radar.py::NewsRadarResponse`
- **주요 필드**: `readiness`, `summary`, `sections`, `items`, `excluded_items`, `source_coverage`, `as_of`
- **신규 backend API 만들지 않는다.** invest 화면에 맞는 view-model 매핑은 프론트 표시층에서만 처리.

### 3.2 상세 페이지 데이터 페칭 결정

상세 페이지 `/discover/issues/:issueId`는 **항상 `/trading/api/news-radar`를 다시 호출**해서 `id` 매칭으로 항목을 찾는다.

- 직접 URL 진입 / 새로고침 / 공유 링크 안전.
- React Router `state` 전달 / Context / SWR / React Query 도입하지 않는다.
- 리스트와 상세는 동일 hook (`useNewsRadar`)을 재사용. 상세 페이지에서 받은 응답에서 `id === issueId`인 항목을 찾고, 없으면 빈/오류 상태로 표시.

## 4. Routes

`frontend/invest/src/routes.tsx`는 `basename: "/invest/app"`을 유지하고 다음 경로 추가:

| Path                                       | Element                       | Note                              |
| ------------------------------------------ | ----------------------------- | --------------------------------- |
| `/`                                        | `HomePage`                    | 기존 ROB-123 홈 (변경 없음)       |
| `/paper`                                   | `PaperPlaceholderPage`        | 기존 (변경 없음)                  |
| `/paper/:variant`                          | `PaperPlaceholderPage`        | 기존 (변경 없음)                  |
| `/discover`                                | **`DiscoverPage` (신규)**     | AI 실시간 이슈 리스트              |
| `/discover/issues/:issueId`                | **`DiscoverIssueDetailPage` (신규)** | 이슈 상세                  |
| `*`                                        | `Navigate to="/" replace`     | 기존 catch-all (변경 없음)        |

## 5. BottomNav 변경

`frontend/invest/src/components/BottomNav.tsx` 4개 탭 처리:

| 라벨   | 동작                                                       |
| ------ | ---------------------------------------------------------- |
| 증권   | `NavLink to="/"` (active 시 강조)                           |
| 관심   | `<button disabled aria-disabled="true">` + dim 스타일       |
| 발견   | `NavLink to="/discover"` (active 시 강조)                   |
| 피드   | `<button disabled aria-disabled="true">` + dim 스타일       |

- `alert("준비 중")` 호출은 모두 제거.
- active 표시는 `NavLink`의 `isActive` 콜백으로 색상만 강조 (`var(--text)` vs `var(--muted)`).
- disabled 탭은 `cursor: "not-allowed"` + `opacity: 0.5`, 클릭 핸들러 없음.

## 6. 파일 구조

신규/수정 파일 (Linear spec 기준 + 본 design doc에서 확정한 항목):

### 6.1 신규 파일

```
frontend/invest/src/
├── api/
│   └── newsRadar.ts                          # fetch wrapper
├── hooks/
│   └── useNewsRadar.ts                       # state hook (loading/error/ready)
├── types/
│   └── newsRadar.ts                          # NewsRadarResponse / NewsRadarItem TS mirror
├── pages/
│   ├── DiscoverPage.tsx                      # 발견 메인
│   └── DiscoverIssueDetailPage.tsx           # 이슈 상세
├── components/discover/
│   ├── DiscoverHeader.tsx                    # 상단 "발견" 헤더
│   ├── CategoryShortcutRail.tsx              # 해외주식/국내주식/옵션/채권 (모두 비활성)
│   ├── TodayEventCard.tsx                    # placeholder ("경제 캘린더 준비 중")
│   ├── AiIssueTicker.tsx                     # "AI 실시간 이슈" 섹션 헤더 + 안내
│   ├── AiIssueCard.tsx                       # 카드 1개 (rank, indicator, 제목, 부제, 관련 뉴스 n개, 시간)
│   ├── IssueImpactMap.tsx                    # "어떤 영향을 줄까?" deterministic mapping
│   └── RelatedSymbolsList.tsx                # 관련 종목 (item.symbols 있을 때만)
└── format/
    └── relativeTime.ts                       # published_at → "5분 전" 류 (선택, 새로 만들거나 기존 utils에 추가)
```

### 6.2 수정 파일

```
frontend/invest/src/
├── routes.tsx                                # /discover, /discover/issues/:issueId 추가
└── components/
    └── BottomNav.tsx                         # NavLink + disabled tabs
```

### 6.3 신규 테스트

```
frontend/invest/src/__tests__/
├── DiscoverPage.test.tsx                     # list rendering, empty, error, loading
├── DiscoverIssueDetailPage.test.tsx          # detail rendering, not-found, related symbols absent → 안내
├── BottomNav.test.tsx                        # 발견 NavLink 동작, disabled 탭 클릭 무시
└── newsRadar.mapping.test.tsx                # bucket count, severity → indicator, impact map mapping (옵션)
```

## 7. View-model 매핑

### 7.1 카드 매핑 (`NewsRadarItem` → `AiIssueCardProps`)

| 카드 필드          | 소스                                                                 |
| ------------------ | -------------------------------------------------------------------- |
| `rank`             | 정렬 후 인덱스 (1-base). 정렬키: `severity` (high>medium>low) → `briefing_score` desc → `published_at` desc |
| `title`            | `item.title`                                                          |
| `subtitle`         | `item.snippet` (없으면 `item.themes.join(", ") || item.matched_terms.join(", ")`) |
| `relatedNewsCount` | **같은 `risk_category` bucket의 item count** (응답 전체 `items` 기준) |
| `relativeTime`     | `published_at` → `5분 전` / `1시간 전` 류                              |
| `indicator`        | `severity`: `high` → 빨강 삼각형, `medium` → 회색 indicator, `low` → 약한 회색 dot. **상승/하락 의미가 아니라 "뉴스 강도"로 라벨링.** |
| `detailHref`       | `/discover/issues/${item.id}`                                         |

**UI 문구**: 카드 하단에 `관련 뉴스 ${n}개` (또는 `관련 이슈 ${n}개`). "출처 ${n}개"처럼 source merging이 일어난 듯한 표현은 피한다.

**정렬 안정성**: 동일 severity / score / published_at일 때 입력 순서 유지 (Array.prototype.sort는 stable).

### 7.2 Today Event card (placeholder)

`TodayEventCard`는 본 MVP에서 **순수 placeholder**:

- 제목: `오늘의 주요 이벤트`
- 본문: `경제 캘린더는 준비 중입니다.`
- subtle 안내: `이번 분기 실적/지표 일정은 후속 업데이트에서 제공됩니다.`
- AI 실시간 이슈 리스트와 의미 중복을 피하기 위해 `news-radar` 데이터를 끌어쓰지 않는다.

### 7.3 Impact map (deterministic)

`IssueImpactMap`은 `item.risk_category`로 키를 잡아 정해진 영향 테마 pill 리스트를 보여준다. 매핑은 frontend constant로 둔다 (LLM 호출 없음):

```ts
// 예: frontend/invest/src/components/discover/impactMap.ts (또는 IssueImpactMap.tsx 내부)
const IMPACT_MAP: Record<NewsRadarRiskCategory, ImpactPill[]> = {
  geopolitical_oil: [
    { theme: "원유/에너지", tone: "watch", note: "변동성/수혜 가능" },
    { theme: "항공/운송",   tone: "negative", note: "비용 압박 가능" },
    { theme: "금/방산",     tone: "positive", note: "방어적 선호 가능" },
  ],
  macro_policy: [
    { theme: "금리 민감 성장주", tone: "negative", note: "부담 가능" },
    { theme: "금융",            tone: "watch",    note: "금리/스프레드 영향" },
  ],
  earnings_bigtech: [
    { theme: "AI/반도체", tone: "watch",    note: "수요/실적 민감" },
    { theme: "나스닥",    tone: "watch",    note: "투자심리 영향" },
  ],
  crypto_security: [
    { theme: "가상자산", tone: "negative", note: "보안/규제 리스크" },
  ],
  korea_market: [
    { theme: "국내 증시", tone: "watch", note: "수급/정책/환율 영향" },
  ],
};
```

`risk_category`가 `null` 또는 매핑에 없는 값이면 영향 맵 대신 안내 문구만 표시: `이 이슈에 대한 영향 분석은 준비 중입니다.`

각 카드 하단에 면책 표시: `뉴스 기반 참고 정보이며 매매 추천이 아닙니다.`

### 7.4 RelatedSymbolsList

- `item.symbols.length > 0`이면 심볼 ticker 리스트로 표시 (단순 chip, 클릭 동작은 이번 이슈에서 만들지 않거나 placeholder no-op).
- `item.symbols.length === 0`이면 `관련 종목 분석은 준비 중입니다.` 문구 한 줄로 대체.
- 임의 종목을 만들지 않는다. analyzer 호출도 하지 않는다.

## 8. Hook / API 디자인

### 8.1 `frontend/invest/src/api/newsRadar.ts`

```ts
import type { NewsRadarResponse } from "../types/newsRadar";

export async function fetchNewsRadar(
  params: { market?: "all" | "kr" | "us" | "crypto"; hours?: number; limit?: number; includeExcluded?: boolean } = {},
  signal?: AbortSignal,
): Promise<NewsRadarResponse> {
  const qs = new URLSearchParams({
    market: params.market ?? "all",
    hours: String(params.hours ?? 24),
    include_excluded: String(params.includeExcluded ?? true),
    limit: String(params.limit ?? 20),
  });
  const res = await fetch(`/trading/api/news-radar?${qs.toString()}`, {
    credentials: "include",
    signal,
  });
  if (!res.ok) throw new Error(`/trading/api/news-radar ${res.status}`);
  return (await res.json()) as NewsRadarResponse;
}
```

### 8.2 `frontend/invest/src/hooks/useNewsRadar.ts`

`useInvestHome`와 동일한 형태:

```ts
export type NewsRadarState =
  | { status: "loading" }
  | { status: "error"; message: string }
  | { status: "ready"; data: NewsRadarResponse };

export function useNewsRadar(): { state: NewsRadarState; reload: () => void };
```

DiscoverPage / DiscoverIssueDetailPage 모두 동일 hook을 사용. 상세 페이지는 `state.data.items.find((i) => i.id === issueId)`로 항목을 찾고, 없으면 "이슈를 찾을 수 없습니다." 빈 상태 표시.

### 8.3 페이지 testability

`DiscoverPage`와 `DiscoverIssueDetailPage`는 `HomePage`처럼 optional props (`{ state?, reload? }`)를 받는다. 테스트에서 hook을 우회하고 fixture를 직접 주입하기 위함.

## 9. 상태 처리

| 상태       | 화면                                                                    |
| ---------- | ----------------------------------------------------------------------- |
| loading    | "불러오는 중…" subtle 텍스트 (AppShell 안)                                |
| error      | 사용자에게 "잠시 후 다시 시도해 주세요." + 재시도 버튼 + `state.message` |
| empty      | `items.length === 0` → "표시할 이슈가 없습니다." + readiness 안내 (선택)   |
| ready      | 카드 리스트                                                              |
| readiness=stale | 리스트 위에 "데이터가 최신이 아닐 수 있습니다." 작은 배지            |
| readiness=unavailable | 에러와 동일 처리                                              |

상세 페이지에서 `find` 결과가 없을 때:

- `이슈를 찾을 수 없습니다. 시간이 지나 목록에서 빠졌을 수 있어요.` + `발견으로 돌아가기` 버튼 (`Link to="/discover"`).

## 10. 테스트 계획

Vitest + Testing Library. 모든 fetch는 monkeypatch / `vi.mock` 또는 page-level prop 주입으로 처리.

**필수 커버리지**:

1. **routes 등록**: `/discover`, `/discover/issues/:issueId` 가 router에 존재하고 catch-all 보다 먼저 매칭됨.
2. **BottomNav**: `발견`이 `<a href="/invest/app/discover">`로 렌더 / 클릭. `관심`, `피드`는 `aria-disabled="true"` 보유 + 클릭해도 `alert`/navigate 없음.
3. **DiscoverPage list rendering**: fixture 주입 → 카드 N개 / 정렬 / 관련 뉴스 카운트 / 상대시간 표시 확인.
4. **DiscoverPage 상태**: loading / error / empty 각 한 케이스.
5. **DiscoverIssueDetailPage rendering**: id 매칭 성공, impact map pill 렌더, related symbols (있는 케이스 / 없는 케이스 모두).
6. **DiscoverIssueDetailPage not-found**: id 매칭 실패 → 안내 문구 + 돌아가기 링크.
7. **(선택) 매핑 단위 테스트**: severity → indicator, risk_category → impact pills.

## 11. Acceptance criteria 매핑

| Linear AC                                                                                            | 구현 위치                                                                       |
| ---------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------- |
| `/invest/app/discover` route가 열리고 모바일 다크 UI로 렌더                                          | `routes.tsx` + `DiscoverPage` (AppShell 사용)                                   |
| BottomNav `발견`이 `/discover`로 이동하고 active 표시                                                 | `BottomNav.tsx` (NavLink isActive)                                              |
| `오늘 이벤트` 카드 존재, 캘린더 미연동 시 명확히 표시                                                | `TodayEventCard` placeholder                                                    |
| `AI 실시간 이슈` 리스트, news-radar 기반 제목/부제/출처/시간/강도                                     | `DiscoverPage` + `AiIssueCard` (관련 뉴스 n개, 상대시간, severity indicator)    |
| API loading / error / empty 깨지지 않음                                                              | `useNewsRadar` 상태 분기 + 테스트                                                |
| 카드 클릭 → `/discover/issues/:issueId` 진입                                                         | `AiIssueCard` `<Link>`                                                           |
| 상세: 요약, 출처/시간, 영향 맵, 관련 symbols(있을 때만), 안내 문구                                    | `DiscoverIssueDetailPage` + `IssueImpactMap` + `RelatedSymbolsList`             |
| `RelatedSymbolsList`: symbols 없으면 "관련 종목 분석은 준비 중입니다"                                  | `RelatedSymbolsList` 분기                                                        |
| 프론트 테스트 route / list / detail / empty / error 커버                                              | `__tests__/DiscoverPage.test.tsx`, `DiscoverIssueDetailPage.test.tsx`, `BottomNav.test.tsx` |
| read-only 안전 경계 유지, 신규 order/broker/watch/scheduler import 없음                              | grep 검증 + 신규 파일 review                                                     |

## 12. 검증 명령

```bash
cd frontend/invest
npm run typecheck
npm test
npm run build
```

선택적 backend safety check:

```bash
# repo root에서
uv run ruff check app/ tests/
uv run ruff format --check app/ tests/
git diff --check
```

## 13. 결정 요약 (브레인스토밍 응답)

| 결정                              | 선택                                                                                  |
| --------------------------------- | ------------------------------------------------------------------------------------- |
| 카드 "출처 수" 표시               | (b) 같은 `risk_category` bucket의 item count. UI 라벨은 `관련 뉴스 n개`로 표현.        |
| 상세 페이지 데이터 소스           | (b) 항상 `/trading/api/news-radar` 재호출 후 `id` 매칭. SWR/React Query 미도입.        |
| `오늘 이벤트` 카드                | (a) 순수 placeholder. news-radar 데이터 끌어오지 않음.                                  |
| BottomNav 비활성 탭               | (b) `disabled` + `aria-disabled` + dim. `alert` 제거, 클릭 무시.                        |
| 브랜치                            | 신규 브랜치 생성하지 않음. 현재 워크트리 `auto-trader-investapp-ai-mvp` 위에서 진행. |

## 14. Out-of-scope (후속 이슈 후보)

- 경제지표 / 실적 캘린더 provider 연동
- 뉴스 클러스터 (출처 병합) 백엔드 기능
- 관련 종목 자동 분석 / 추천
- 실시간 시세 / 차트 / websocket
- `관심`, `피드` 탭 기능
- `해외주식/국내주식/옵션/채권` 카테고리 카드 라우팅
