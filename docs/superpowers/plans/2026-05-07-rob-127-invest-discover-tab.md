# ROB-127: Invest Discover Tab MVP — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a Toss-style read-only "발견(Discover)" tab to the existing `/invest/app` React/Vite app. Wire BottomNav, build the AI realtime issue list and detail page, all backed by the existing read-only `GET /trading/api/news-radar` endpoint.

**Architecture:** Reuse the established `useInvestHome` pattern — fetch wrapper + `useState`/`useEffect` hook returning `{ state, reload }` with a discriminated union (`loading | error | ready`). Pages accept optional `{ state?, reload? }` props for fixture-based testing. The detail page refetches `/trading/api/news-radar` and finds the matching `id` (no shared cache, no SWR/React Query). All copy in Korean. Read-only — no broker/order/watch/scheduler/DB mutation imports anywhere.

**Tech Stack:** React 19, React Router 7 (`createBrowserRouter` with `basename: "/invest/app"`, `NavLink`), TypeScript 6, Vite 8, Vitest 4 + Testing Library. Existing CSS variables in `frontend/invest/src/styles.css` (dark mobile shell).

**Spec:** [`docs/superpowers/specs/2026-05-07-rob-127-invest-discover-tab-design.md`](../specs/2026-05-07-rob-127-invest-discover-tab-design.md)

**Branch:** `auto-trader-investapp-ai-mvp` (current worktree, no new branch).

---

## File Map

### New files (`frontend/invest/src/`)

| Path                                                      | Responsibility                                                                |
| --------------------------------------------------------- | ----------------------------------------------------------------------------- |
| `types/newsRadar.ts`                                      | TS mirror of `app/schemas/news_radar.py`                                       |
| `api/newsRadar.ts`                                        | Single fetch wrapper for `/trading/api/news-radar`                            |
| `hooks/useNewsRadar.ts`                                   | `useState`/`useEffect` hook returning `{ state, reload }`                     |
| `format/relativeTime.ts`                                  | `formatRelativeTime(date, now?)` → `5분 전` / `1시간 전` / `2일 전`            |
| `components/discover/severity.ts`                         | Severity → indicator label/color mapping + bucket-count helper                  |
| `components/discover/impactMap.ts`                        | `IMPACT_MAP: Record<NewsRadarRiskCategory, ImpactPill[]>` constant              |
| `components/discover/AiIssueCard.tsx`                     | Single card (rank, indicator, title, subtitle, related-news count, time, link) |
| `components/discover/AiIssueTicker.tsx`                   | Section heading + disclaimer for the AI issue list                             |
| `components/discover/DiscoverHeader.tsx`                  | "발견" header text                                                              |
| `components/discover/CategoryShortcutRail.tsx`            | Static disabled chips (해외주식 / 국내주식 / 옵션 / 채권)                      |
| `components/discover/TodayEventCard.tsx`                  | Static placeholder card                                                        |
| `components/discover/IssueImpactMap.tsx`                  | "어떤 영향을 줄까?" pills based on `risk_category`                              |
| `components/discover/RelatedSymbolsList.tsx`              | Symbol chips or "준비 중" fallback                                             |
| `pages/DiscoverPage.tsx`                                  | List page composition + state branching                                        |
| `pages/DiscoverIssueDetailPage.tsx`                       | Detail page (find by id, fallback, impact, symbols, disclaimer)                |

### Modified files

| Path                                                      | Change                                                                  |
| --------------------------------------------------------- | ----------------------------------------------------------------------- |
| `frontend/invest/src/routes.tsx`                          | Register `/discover` and `/discover/issues/:issueId`                     |
| `frontend/invest/src/components/BottomNav.tsx`            | Replace `alert("준비 중")` with `NavLink` (증권/발견) + disabled (관심/피드) |

### New tests (`frontend/invest/src/__tests__/`)

| Path                                       | Coverage                                                                |
| ------------------------------------------ | ----------------------------------------------------------------------- |
| `relativeTime.test.ts`                     | unit: 분/시간/일 boundaries, future fallback, null                       |
| `severity.test.ts`                         | unit: bucket count, severity → indicator mapping                         |
| `BottomNav.test.tsx`                       | 발견 NavLink href, 관심/피드 aria-disabled + click no-op                 |
| `AiIssueCard.test.tsx`                     | rendering: title/subtitle/related-news count/time/indicator/link href    |
| `IssueImpactMap.test.tsx`                  | known category → pills; unknown/null → 안내 문구                          |
| `RelatedSymbolsList.test.tsx`              | symbols present → chips; empty → 준비 중                                 |
| `DiscoverPage.test.tsx`                    | list rendering (sorted), loading, error, empty, stale-readiness banner   |
| `DiscoverIssueDetailPage.test.tsx`         | id match → detail; not-found → 안내 + back link; loading/error           |
| `routes.test.tsx`                          | router exposes `/discover` and `/discover/issues/:issueId`                |

---

## Conventions

- **Imports**: relative paths, mirroring `HomePage.tsx`.
- **Styles**: prefer CSS variables (`var(--bg)`, `var(--surface)`, `var(--text)`, `var(--muted)`, `var(--gain)`, `var(--loss)`, `var(--warn)`). Inline `style={{}}` is fine — matches existing pattern. New ad-hoc class names allowed if reused.
- **Korean copy**: never overstate causality. Disclaimer line on every detail-screen impact area: `뉴스 기반 참고 정보이며 매매 추천이 아닙니다.`
- **No broker/order/watch/scheduler/LLM imports** anywhere.
- **Commits**: Conventional Commits style matching repo (`feat(rob-127): ...`, `test(rob-127): ...`, `refactor(rob-127): ...`). One small focused commit per task. The `Co-Authored-By` trailer used in this repo is **not** required for these commits — match the recent style (no trailer, e.g. commit `af4d2384`).

---

## Task 1: TypeScript types for NewsRadar

**Files:**
- Create: `frontend/invest/src/types/newsRadar.ts`

- [ ] **Step 1: Write the type definitions**

```ts
// frontend/invest/src/types/newsRadar.ts
export type NewsRadarReadinessStatus = "ready" | "stale" | "unavailable";
export type NewsRadarSeverity = "high" | "medium" | "low";
export type NewsRadarRiskCategory =
  | "geopolitical_oil"
  | "macro_policy"
  | "crypto_security"
  | "earnings_bigtech"
  | "korea_market";
export type NewsRadarMarket = "all" | "kr" | "us" | "crypto";

export interface NewsRadarReadiness {
  status: NewsRadarReadinessStatus;
  latest_scraped_at: string | null;
  latest_published_at: string | null;
  recent_6h_count: number;
  recent_24h_count: number;
  source_count: number;
  stale: boolean;
  max_age_minutes: number;
  warnings: string[];
}

export interface NewsRadarSummary {
  high_risk_count: number;
  total_count: number;
  included_in_briefing_count: number;
  excluded_but_collected_count: number;
}

export interface NewsRadarSourceCoverage {
  feed_source: string;
  recent_6h: number;
  recent_24h: number;
  latest_published_at: string | null;
  latest_scraped_at: string | null;
  status: string;
}

export interface NewsRadarItem {
  id: string;
  title: string;
  source: string | null;
  feed_source: string | null;
  url: string;
  published_at: string | null;
  market: string;
  risk_category: NewsRadarRiskCategory | null;
  severity: NewsRadarSeverity;
  themes: string[];
  symbols: string[];
  included_in_briefing: boolean;
  briefing_reason: string | null;
  briefing_score: number;
  snippet: string | null;
  matched_terms: string[];
}

export interface NewsRadarSection {
  section_id: NewsRadarRiskCategory;
  title: string;
  severity: NewsRadarSeverity;
  items: NewsRadarItem[];
}

export interface NewsRadarResponse {
  market: NewsRadarMarket;
  as_of: string;
  readiness: NewsRadarReadiness;
  summary: NewsRadarSummary;
  sections: NewsRadarSection[];
  items: NewsRadarItem[];
  excluded_items: NewsRadarItem[];
  source_coverage: NewsRadarSourceCoverage[];
}
```

- [ ] **Step 2: Typecheck**

Run: `cd frontend/invest && npm run typecheck`
Expected: PASS (no output beyond tsc summary).

- [ ] **Step 3: Commit**

```bash
git add frontend/invest/src/types/newsRadar.ts
git commit -m "feat(rob-127): add NewsRadar TypeScript types"
```

---

## Task 2: API client `fetchNewsRadar`

**Files:**
- Create: `frontend/invest/src/api/newsRadar.ts`
- Test: `frontend/invest/src/__tests__/newsRadar.api.test.ts`

- [ ] **Step 1: Write the failing test**

```ts
// frontend/invest/src/__tests__/newsRadar.api.test.ts
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { fetchNewsRadar } from "../api/newsRadar";
import type { NewsRadarResponse } from "../types/newsRadar";

const baseResponse: NewsRadarResponse = {
  market: "all",
  as_of: "2026-05-07T00:00:00Z",
  readiness: {
    status: "ready",
    latest_scraped_at: null,
    latest_published_at: null,
    recent_6h_count: 0,
    recent_24h_count: 0,
    source_count: 0,
    stale: false,
    max_age_minutes: 0,
    warnings: [],
  },
  summary: {
    high_risk_count: 0,
    total_count: 0,
    included_in_briefing_count: 0,
    excluded_but_collected_count: 0,
  },
  sections: [],
  items: [],
  excluded_items: [],
  source_coverage: [],
};

const fetchMock = vi.fn();

beforeEach(() => {
  fetchMock.mockReset();
  vi.stubGlobal("fetch", fetchMock);
});

afterEach(() => {
  vi.unstubAllGlobals();
});

test("fetchNewsRadar uses default query params and credentials", async () => {
  fetchMock.mockResolvedValueOnce({ ok: true, json: async () => baseResponse });

  await fetchNewsRadar();

  expect(fetchMock).toHaveBeenCalledTimes(1);
  const [url, init] = fetchMock.mock.calls[0];
  expect(url).toBe(
    "/trading/api/news-radar?market=all&hours=24&include_excluded=true&limit=20",
  );
  expect(init).toMatchObject({ credentials: "include" });
});

test("fetchNewsRadar throws on non-ok response", async () => {
  fetchMock.mockResolvedValueOnce({ ok: false, status: 503, json: async () => ({}) });
  await expect(fetchNewsRadar()).rejects.toThrow(/503/);
});

test("fetchNewsRadar overrides params", async () => {
  fetchMock.mockResolvedValueOnce({ ok: true, json: async () => baseResponse });
  await fetchNewsRadar({ market: "kr", hours: 12, limit: 5, includeExcluded: false });
  const [url] = fetchMock.mock.calls[0];
  expect(url).toBe(
    "/trading/api/news-radar?market=kr&hours=12&include_excluded=false&limit=5",
  );
});
```

- [ ] **Step 2: Run test, expect failure**

Run: `cd frontend/invest && npm test -- newsRadar.api`
Expected: FAIL — `Cannot find module '../api/newsRadar'`.

- [ ] **Step 3: Implement the API client**

```ts
// frontend/invest/src/api/newsRadar.ts
import type { NewsRadarMarket, NewsRadarResponse } from "../types/newsRadar";

export interface FetchNewsRadarParams {
  market?: NewsRadarMarket;
  hours?: number;
  limit?: number;
  includeExcluded?: boolean;
}

export async function fetchNewsRadar(
  params: FetchNewsRadarParams = {},
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
  if (!res.ok) {
    throw new Error(`/trading/api/news-radar ${res.status}`);
  }
  return (await res.json()) as NewsRadarResponse;
}
```

- [ ] **Step 4: Run test, expect pass**

Run: `cd frontend/invest && npm test -- newsRadar.api`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add frontend/invest/src/api/newsRadar.ts frontend/invest/src/__tests__/newsRadar.api.test.ts
git commit -m "feat(rob-127): add /trading/api/news-radar fetch wrapper"
```

---

## Task 3: `useNewsRadar` hook

**Files:**
- Create: `frontend/invest/src/hooks/useNewsRadar.ts`

(Hook is mirror of `useInvestHome.ts`. We rely on the API client tests + page-level tests to exercise behavior; per repo convention `useInvestHome` does not have its own dedicated test, so we skip a dedicated hook unit test here.)

- [ ] **Step 1: Implement the hook**

```ts
// frontend/invest/src/hooks/useNewsRadar.ts
import { useEffect, useState } from "react";
import { fetchNewsRadar, type FetchNewsRadarParams } from "../api/newsRadar";
import type { NewsRadarResponse } from "../types/newsRadar";

export type NewsRadarState =
  | { status: "loading" }
  | { status: "error"; message: string }
  | { status: "ready"; data: NewsRadarResponse };

export function useNewsRadar(params: FetchNewsRadarParams = {}) {
  const [state, setState] = useState<NewsRadarState>({ status: "loading" });
  const [tick, setTick] = useState(0);

  useEffect(() => {
    const controller = new AbortController();
    setState({ status: "loading" });
    fetchNewsRadar(params, controller.signal)
      .then((data) => setState({ status: "ready", data }))
      .catch((e) => {
        if (controller.signal.aborted) return;
        setState({ status: "error", message: e?.message ?? "failed" });
      });
    return () => controller.abort();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tick]);

  return { state, reload: () => setTick((t) => t + 1) };
}
```

- [ ] **Step 2: Typecheck**

Run: `cd frontend/invest && npm run typecheck`
Expected: PASS.

- [ ] **Step 3: Commit**

```bash
git add frontend/invest/src/hooks/useNewsRadar.ts
git commit -m "feat(rob-127): add useNewsRadar hook"
```

---

## Task 4: `formatRelativeTime` utility

**Files:**
- Create: `frontend/invest/src/format/relativeTime.ts`
- Test: `frontend/invest/src/__tests__/relativeTime.test.ts`

- [ ] **Step 1: Write the failing test**

```ts
// frontend/invest/src/__tests__/relativeTime.test.ts
import { expect, test } from "vitest";
import { formatRelativeTime } from "../format/relativeTime";

const NOW = new Date("2026-05-07T12:00:00Z");

test("returns '방금' for under 1 minute", () => {
  expect(formatRelativeTime("2026-05-07T11:59:30Z", NOW)).toBe("방금");
});

test("returns minutes for under 1 hour", () => {
  expect(formatRelativeTime("2026-05-07T11:55:00Z", NOW)).toBe("5분 전");
});

test("returns hours for under 1 day", () => {
  expect(formatRelativeTime("2026-05-07T09:00:00Z", NOW)).toBe("3시간 전");
});

test("returns days for over 24 hours", () => {
  expect(formatRelativeTime("2026-05-05T12:00:00Z", NOW)).toBe("2일 전");
});

test("returns null when input is null", () => {
  expect(formatRelativeTime(null, NOW)).toBeNull();
});

test("returns null when input is in the future", () => {
  expect(formatRelativeTime("2026-05-07T12:30:00Z", NOW)).toBeNull();
});
```

- [ ] **Step 2: Run test, expect failure**

Run: `cd frontend/invest && npm test -- relativeTime`
Expected: FAIL — module not found.

- [ ] **Step 3: Implement**

```ts
// frontend/invest/src/format/relativeTime.ts
export function formatRelativeTime(
  iso: string | null | undefined,
  now: Date = new Date(),
): string | null {
  if (!iso) return null;
  const then = new Date(iso);
  if (Number.isNaN(then.getTime())) return null;
  const deltaMs = now.getTime() - then.getTime();
  if (deltaMs < 0) return null;
  const minutes = Math.floor(deltaMs / 60_000);
  if (minutes < 1) return "방금";
  if (minutes < 60) return `${minutes}분 전`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}시간 전`;
  const days = Math.floor(hours / 24);
  return `${days}일 전`;
}
```

- [ ] **Step 4: Run test, expect pass**

Run: `cd frontend/invest && npm test -- relativeTime`
Expected: PASS (6 tests).

- [ ] **Step 5: Commit**

```bash
git add frontend/invest/src/format/relativeTime.ts frontend/invest/src/__tests__/relativeTime.test.ts
git commit -m "feat(rob-127): add formatRelativeTime utility"
```

---

## Task 5: Severity indicator + bucket-count helpers

**Files:**
- Create: `frontend/invest/src/components/discover/severity.ts`
- Test: `frontend/invest/src/__tests__/severity.test.ts`

- [ ] **Step 1: Write the failing test**

```ts
// frontend/invest/src/__tests__/severity.test.ts
import { expect, test } from "vitest";
import {
  countByRiskCategory,
  describeSeverity,
  sortIssueItems,
} from "../components/discover/severity";
import type { NewsRadarItem } from "../types/newsRadar";

function makeItem(overrides: Partial<NewsRadarItem>): NewsRadarItem {
  return {
    id: "x",
    title: "t",
    source: null,
    feed_source: null,
    url: "",
    published_at: null,
    market: "all",
    risk_category: null,
    severity: "low",
    themes: [],
    symbols: [],
    included_in_briefing: false,
    briefing_reason: null,
    briefing_score: 0,
    snippet: null,
    matched_terms: [],
    ...overrides,
  };
}

test("describeSeverity maps to indicator label", () => {
  expect(describeSeverity("high").label).toBe("강한 이슈");
  expect(describeSeverity("medium").label).toBe("관심 이슈");
  expect(describeSeverity("low").label).toBe("참고");
});

test("countByRiskCategory groups items by risk_category", () => {
  const items = [
    makeItem({ id: "1", risk_category: "macro_policy" }),
    makeItem({ id: "2", risk_category: "macro_policy" }),
    makeItem({ id: "3", risk_category: "geopolitical_oil" }),
    makeItem({ id: "4", risk_category: null }),
  ];
  const counts = countByRiskCategory(items);
  expect(counts.macro_policy).toBe(2);
  expect(counts.geopolitical_oil).toBe(1);
  expect(counts.uncategorized).toBe(1);
});

test("sortIssueItems orders by severity then briefing_score then published_at", () => {
  const items = [
    makeItem({ id: "a", severity: "low", briefing_score: 10, published_at: "2026-05-07T10:00:00Z" }),
    makeItem({ id: "b", severity: "high", briefing_score: 5, published_at: "2026-05-07T08:00:00Z" }),
    makeItem({ id: "c", severity: "high", briefing_score: 9, published_at: "2026-05-07T08:00:00Z" }),
    makeItem({ id: "d", severity: "medium", briefing_score: 0, published_at: "2026-05-07T11:00:00Z" }),
  ];
  expect(sortIssueItems(items).map((i) => i.id)).toEqual(["c", "b", "d", "a"]);
});
```

- [ ] **Step 2: Run test, expect failure**

Run: `cd frontend/invest && npm test -- severity`
Expected: FAIL — module not found.

- [ ] **Step 3: Implement**

```ts
// frontend/invest/src/components/discover/severity.ts
import type { NewsRadarItem, NewsRadarSeverity } from "../../types/newsRadar";

export interface SeverityDescriptor {
  label: string;
  color: string;
  glyph: "▲" | "■" | "·";
}

export function describeSeverity(severity: NewsRadarSeverity): SeverityDescriptor {
  switch (severity) {
    case "high":
      return { label: "강한 이슈", color: "var(--gain)", glyph: "▲" };
    case "medium":
      return { label: "관심 이슈", color: "var(--muted)", glyph: "■" };
    case "low":
    default:
      return { label: "참고", color: "var(--muted)", glyph: "·" };
  }
}

export type RiskBucketKey =
  | "geopolitical_oil"
  | "macro_policy"
  | "crypto_security"
  | "earnings_bigtech"
  | "korea_market"
  | "uncategorized";

export function countByRiskCategory(items: NewsRadarItem[]): Record<RiskBucketKey, number> {
  const out: Record<RiskBucketKey, number> = {
    geopolitical_oil: 0,
    macro_policy: 0,
    crypto_security: 0,
    earnings_bigtech: 0,
    korea_market: 0,
    uncategorized: 0,
  };
  for (const item of items) {
    const key = (item.risk_category ?? "uncategorized") as RiskBucketKey;
    out[key] = (out[key] ?? 0) + 1;
  }
  return out;
}

const SEVERITY_RANK: Record<NewsRadarSeverity, number> = { high: 3, medium: 2, low: 1 };

export function sortIssueItems(items: NewsRadarItem[]): NewsRadarItem[] {
  return [...items].sort((a, b) => {
    const sev = SEVERITY_RANK[b.severity] - SEVERITY_RANK[a.severity];
    if (sev !== 0) return sev;
    if (b.briefing_score !== a.briefing_score) return b.briefing_score - a.briefing_score;
    const at = a.published_at ? Date.parse(a.published_at) : 0;
    const bt = b.published_at ? Date.parse(b.published_at) : 0;
    return bt - at;
  });
}

export function relatedNewsCount(
  item: NewsRadarItem,
  buckets: Record<RiskBucketKey, number>,
): number {
  const key = (item.risk_category ?? "uncategorized") as RiskBucketKey;
  return buckets[key] ?? 0;
}
```

- [ ] **Step 4: Run test, expect pass**

Run: `cd frontend/invest && npm test -- severity`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add frontend/invest/src/components/discover/severity.ts frontend/invest/src/__tests__/severity.test.ts
git commit -m "feat(rob-127): add severity descriptor and bucket-count helpers"
```

---

## Task 6: Impact map constant

**Files:**
- Create: `frontend/invest/src/components/discover/impactMap.ts`

- [ ] **Step 1: Implement (no test — pure data, will be exercised by IssueImpactMap test in Task 11)**

```ts
// frontend/invest/src/components/discover/impactMap.ts
import type { NewsRadarRiskCategory } from "../../types/newsRadar";

export type ImpactTone = "positive" | "negative" | "watch";

export interface ImpactPill {
  theme: string;
  tone: ImpactTone;
  note: string;
}

export const IMPACT_MAP: Record<NewsRadarRiskCategory, ImpactPill[]> = {
  geopolitical_oil: [
    { theme: "원유/에너지", tone: "watch", note: "변동성/수혜 가능" },
    { theme: "항공/운송", tone: "negative", note: "비용 압박 가능" },
    { theme: "금/방산", tone: "positive", note: "방어적 선호 가능" },
  ],
  macro_policy: [
    { theme: "금리 민감 성장주", tone: "negative", note: "부담 가능" },
    { theme: "금융", tone: "watch", note: "금리/스프레드 영향" },
  ],
  earnings_bigtech: [
    { theme: "AI/반도체", tone: "watch", note: "수요/실적 민감" },
    { theme: "나스닥", tone: "watch", note: "투자심리 영향" },
  ],
  crypto_security: [
    { theme: "가상자산", tone: "negative", note: "보안/규제 리스크" },
  ],
  korea_market: [
    { theme: "국내 증시", tone: "watch", note: "수급/정책/환율 영향" },
  ],
};

export function lookupImpact(category: NewsRadarRiskCategory | null): ImpactPill[] | null {
  if (!category) return null;
  return IMPACT_MAP[category] ?? null;
}
```

- [ ] **Step 2: Typecheck**

Run: `cd frontend/invest && npm run typecheck`
Expected: PASS.

- [ ] **Step 3: Commit**

```bash
git add frontend/invest/src/components/discover/impactMap.ts
git commit -m "feat(rob-127): add deterministic risk-category impact map"
```

---

## Task 7: `BottomNav` rework with NavLink + disabled tabs

**Files:**
- Modify: `frontend/invest/src/components/BottomNav.tsx`
- Test: `frontend/invest/src/__tests__/BottomNav.test.tsx`

- [ ] **Step 1: Write the failing test**

```tsx
// frontend/invest/src/__tests__/BottomNav.test.tsx
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { createMemoryRouter, RouterProvider } from "react-router-dom";
import { expect, test, vi } from "vitest";
import { BottomNav } from "../components/BottomNav";

function renderAt(path: string) {
  const router = createMemoryRouter(
    [{ path: "*", element: <BottomNav /> }],
    { initialEntries: [path], basename: "/invest/app" },
  );
  return render(<RouterProvider router={router} />);
}

test("발견 link points to /invest/app/discover", () => {
  renderAt("/");
  const link = screen.getByRole("link", { name: "발견" });
  expect(link).toHaveAttribute("href", "/invest/app/discover");
});

test("증권 link points to /invest/app", () => {
  renderAt("/");
  const link = screen.getByRole("link", { name: "증권" });
  expect(link).toHaveAttribute("href", "/invest/app/");
});

test("관심 and 피드 are aria-disabled and do not call alert when clicked", async () => {
  const user = userEvent.setup();
  const alertSpy = vi.spyOn(window, "alert").mockImplementation(() => {});
  renderAt("/");

  const watch = screen.getByRole("button", { name: "관심" });
  const feed = screen.getByRole("button", { name: "피드" });
  expect(watch).toHaveAttribute("aria-disabled", "true");
  expect(feed).toHaveAttribute("aria-disabled", "true");

  await user.click(watch);
  await user.click(feed);
  expect(alertSpy).not.toHaveBeenCalled();
  alertSpy.mockRestore();
});

test("BottomNav highlights active tab", () => {
  renderAt("/discover");
  const link = screen.getByRole("link", { name: "발견" });
  // active uses var(--text), inactive uses var(--muted)
  expect(link.getAttribute("style") ?? "").toContain("color: var(--text)");
});
```

- [ ] **Step 2: Run test, expect failure**

Run: `cd frontend/invest && npm test -- BottomNav`
Expected: FAIL — current `BottomNav` renders `<button>` only, no links.

- [ ] **Step 3: Replace `BottomNav.tsx`**

```tsx
// frontend/invest/src/components/BottomNav.tsx
import type { CSSProperties } from "react";
import { NavLink } from "react-router-dom";

const ROW_STYLE: CSSProperties = {
  display: "flex",
  justifyContent: "space-around",
  paddingTop: 8,
  borderTop: "1px solid var(--surface-2)",
  color: "var(--muted)",
  fontSize: 10,
  position: "sticky",
  bottom: 0,
  background: "var(--bg)",
};

const TAB_BASE: CSSProperties = {
  background: "none",
  border: "none",
  cursor: "pointer",
  padding: 8,
  fontSize: 10,
  textDecoration: "none",
};

const DISABLED_STYLE: CSSProperties = {
  ...TAB_BASE,
  color: "var(--muted)",
  opacity: 0.5,
  cursor: "not-allowed",
};

function activeStyle(isActive: boolean): CSSProperties {
  return {
    ...TAB_BASE,
    color: isActive ? "var(--text)" : "var(--muted)",
  };
}

export function BottomNav() {
  return (
    <div style={ROW_STYLE}>
      <NavLink to="/" end style={({ isActive }) => activeStyle(isActive)}>
        증권
      </NavLink>
      <button
        type="button"
        aria-disabled="true"
        disabled
        style={DISABLED_STYLE}
        tabIndex={-1}
      >
        관심
      </button>
      <NavLink to="/discover" style={({ isActive }) => activeStyle(isActive)}>
        발견
      </NavLink>
      <button
        type="button"
        aria-disabled="true"
        disabled
        style={DISABLED_STYLE}
        tabIndex={-1}
      >
        피드
      </button>
    </div>
  );
}
```

- [ ] **Step 4: Run test, expect pass**

Run: `cd frontend/invest && npm test -- BottomNav`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add frontend/invest/src/components/BottomNav.tsx frontend/invest/src/__tests__/BottomNav.test.tsx
git commit -m "feat(rob-127): wire BottomNav 발견 to /discover and disable inactive tabs"
```

---

## Task 8: Static discover sub-components (header, category rail, today event, ticker)

**Files:**
- Create: `frontend/invest/src/components/discover/DiscoverHeader.tsx`
- Create: `frontend/invest/src/components/discover/CategoryShortcutRail.tsx`
- Create: `frontend/invest/src/components/discover/TodayEventCard.tsx`
- Create: `frontend/invest/src/components/discover/AiIssueTicker.tsx`

(These are static presentational components. They are exercised by the page-level test in Task 12 — no dedicated unit tests.)

- [ ] **Step 1: Implement DiscoverHeader**

```tsx
// frontend/invest/src/components/discover/DiscoverHeader.tsx
export function DiscoverHeader() {
  return (
    <div style={{ paddingTop: 4 }}>
      <h1 style={{ margin: 0, fontSize: 22, fontWeight: 800 }}>발견</h1>
      <div className="subtle" style={{ marginTop: 4 }}>
        뉴스 기반 참고 정보입니다. 매매 추천이 아닙니다.
      </div>
    </div>
  );
}
```

- [ ] **Step 2: Implement CategoryShortcutRail**

```tsx
// frontend/invest/src/components/discover/CategoryShortcutRail.tsx
const CATEGORIES = ["해외주식", "국내주식", "옵션", "채권"] as const;

export function CategoryShortcutRail() {
  return (
    <div
      role="list"
      aria-label="카테고리"
      style={{
        display: "grid",
        gridTemplateColumns: "repeat(4, 1fr)",
        gap: 8,
      }}
    >
      {CATEGORIES.map((label) => (
        <div
          key={label}
          role="listitem"
          aria-disabled="true"
          style={{
            padding: 12,
            background: "var(--surface)",
            border: "1px solid var(--surface-2)",
            borderRadius: 12,
            color: "var(--muted)",
            fontSize: 12,
            textAlign: "center",
            opacity: 0.6,
          }}
        >
          {label}
        </div>
      ))}
    </div>
  );
}
```

- [ ] **Step 3: Implement TodayEventCard (placeholder per design — does not consume news-radar data)**

```tsx
// frontend/invest/src/components/discover/TodayEventCard.tsx
export function TodayEventCard() {
  return (
    <section
      aria-labelledby="today-event-heading"
      style={{
        padding: 16,
        background: "var(--surface)",
        border: "1px solid var(--surface-2)",
        borderRadius: 14,
      }}
    >
      <h2
        id="today-event-heading"
        style={{ margin: 0, fontSize: 14, fontWeight: 700 }}
      >
        오늘의 주요 이벤트
      </h2>
      <div className="subtle" style={{ marginTop: 6 }}>
        경제 캘린더는 준비 중입니다.
      </div>
      <div className="subtle" style={{ marginTop: 4 }}>
        실적/지표 일정은 후속 업데이트에서 제공됩니다.
      </div>
    </section>
  );
}
```

- [ ] **Step 4: Implement AiIssueTicker (section header above the issue list)**

```tsx
// frontend/invest/src/components/discover/AiIssueTicker.tsx
export function AiIssueTicker({ asOf }: { asOf?: string | null }) {
  return (
    <header style={{ marginTop: 8 }}>
      <h2 style={{ margin: 0, fontSize: 16, fontWeight: 700 }}>
        AI 실시간 이슈
      </h2>
      <div className="subtle" style={{ marginTop: 4 }}>
        뉴스 기반으로 정리된 참고 정보입니다.
        {asOf ? ` 기준: ${new Date(asOf).toLocaleString("ko-KR")}` : ""}
      </div>
    </header>
  );
}
```

- [ ] **Step 5: Typecheck**

Run: `cd frontend/invest && npm run typecheck`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add frontend/invest/src/components/discover/DiscoverHeader.tsx \
        frontend/invest/src/components/discover/CategoryShortcutRail.tsx \
        frontend/invest/src/components/discover/TodayEventCard.tsx \
        frontend/invest/src/components/discover/AiIssueTicker.tsx
git commit -m "feat(rob-127): add static discover header, category rail, today-event, ticker"
```

---

## Task 9: `AiIssueCard` component

**Files:**
- Create: `frontend/invest/src/components/discover/AiIssueCard.tsx`
- Test: `frontend/invest/src/__tests__/AiIssueCard.test.tsx`

- [ ] **Step 1: Write the failing test**

```tsx
// frontend/invest/src/__tests__/AiIssueCard.test.tsx
import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { expect, test } from "vitest";
import { AiIssueCard } from "../components/discover/AiIssueCard";
import type { NewsRadarItem } from "../types/newsRadar";

const item: NewsRadarItem = {
  id: "abc",
  title: "Fed 금리 동결 시사",
  source: "Reuters",
  feed_source: "reuters",
  url: "https://example.com/news/1",
  published_at: "2026-05-07T11:30:00Z",
  market: "us",
  risk_category: "macro_policy",
  severity: "high",
  themes: ["rates"],
  symbols: ["SPY"],
  included_in_briefing: true,
  briefing_reason: null,
  briefing_score: 80,
  snippet: "위원회는 현재 정책 유지를 시사",
  matched_terms: ["fomc"],
};

test("renders rank, title, snippet, related news count, indicator and link", () => {
  render(
    <MemoryRouter basename="/invest/app">
      <AiIssueCard
        rank={1}
        item={item}
        relatedCount={3}
        now={new Date("2026-05-07T12:00:00Z")}
      />
    </MemoryRouter>,
  );

  expect(screen.getByText("1")).toBeInTheDocument();
  expect(screen.getByText("Fed 금리 동결 시사")).toBeInTheDocument();
  expect(screen.getByText(/위원회는 현재 정책 유지를 시사/)).toBeInTheDocument();
  expect(screen.getByText("관련 뉴스 3개")).toBeInTheDocument();
  expect(screen.getByText("30분 전")).toBeInTheDocument();
  expect(screen.getByLabelText("강한 이슈")).toBeInTheDocument();
  expect(screen.getByRole("link")).toHaveAttribute(
    "href",
    "/invest/app/discover/issues/abc",
  );
});

test("falls back to themes when snippet is missing", () => {
  render(
    <MemoryRouter basename="/invest/app">
      <AiIssueCard
        rank={2}
        item={{ ...item, snippet: null, themes: ["fomc", "rates"] }}
        relatedCount={1}
        now={new Date("2026-05-07T12:00:00Z")}
      />
    </MemoryRouter>,
  );
  expect(screen.getByText(/fomc, rates/)).toBeInTheDocument();
});
```

- [ ] **Step 2: Run test, expect failure**

Run: `cd frontend/invest && npm test -- AiIssueCard`
Expected: FAIL — module not found.

- [ ] **Step 3: Implement**

```tsx
// frontend/invest/src/components/discover/AiIssueCard.tsx
import { Link } from "react-router-dom";
import { formatRelativeTime } from "../../format/relativeTime";
import type { NewsRadarItem } from "../../types/newsRadar";
import { describeSeverity } from "./severity";

export interface AiIssueCardProps {
  rank: number;
  item: NewsRadarItem;
  relatedCount: number;
  now?: Date;
}

function buildSubtitle(item: NewsRadarItem): string {
  if (item.snippet && item.snippet.trim().length > 0) return item.snippet;
  if (item.themes.length > 0) return item.themes.join(", ");
  if (item.matched_terms.length > 0) return item.matched_terms.join(", ");
  return "";
}

export function AiIssueCard({ rank, item, relatedCount, now }: AiIssueCardProps) {
  const indicator = describeSeverity(item.severity);
  const time = formatRelativeTime(item.published_at, now);
  const subtitle = buildSubtitle(item);
  return (
    <Link
      to={`/discover/issues/${item.id}`}
      style={{
        display: "flex",
        gap: 12,
        padding: 14,
        background: "var(--surface)",
        border: "1px solid var(--surface-2)",
        borderRadius: 14,
        color: "var(--text)",
        textDecoration: "none",
      }}
    >
      <div
        style={{
          minWidth: 24,
          fontWeight: 800,
          color: "var(--muted)",
          fontSize: 16,
        }}
      >
        {rank}
      </div>
      <div style={{ flex: 1, minWidth: 0 }}>
        <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
          <span
            aria-label={indicator.label}
            role="img"
            style={{ color: indicator.color, fontSize: 12 }}
          >
            {indicator.glyph}
          </span>
          <span style={{ fontWeight: 700, fontSize: 14 }}>{item.title}</span>
        </div>
        {subtitle && (
          <div
            className="subtle"
            style={{
              marginTop: 4,
              overflow: "hidden",
              textOverflow: "ellipsis",
              whiteSpace: "nowrap",
            }}
          >
            {subtitle}
          </div>
        )}
        <div
          className="subtle"
          style={{ marginTop: 6, display: "flex", gap: 8, fontSize: 11 }}
        >
          <span>관련 뉴스 {relatedCount}개</span>
          {time && <span>· {time}</span>}
        </div>
      </div>
    </Link>
  );
}
```

- [ ] **Step 4: Run test, expect pass**

Run: `cd frontend/invest && npm test -- AiIssueCard`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add frontend/invest/src/components/discover/AiIssueCard.tsx \
        frontend/invest/src/__tests__/AiIssueCard.test.tsx
git commit -m "feat(rob-127): add AiIssueCard with related-news count and severity indicator"
```

---

## Task 10: `IssueImpactMap` component

**Files:**
- Create: `frontend/invest/src/components/discover/IssueImpactMap.tsx`
- Test: `frontend/invest/src/__tests__/IssueImpactMap.test.tsx`

- [ ] **Step 1: Write the failing test**

```tsx
// frontend/invest/src/__tests__/IssueImpactMap.test.tsx
import { render, screen } from "@testing-library/react";
import { expect, test } from "vitest";
import { IssueImpactMap } from "../components/discover/IssueImpactMap";

test("renders impact pills for known risk_category", () => {
  render(<IssueImpactMap category="geopolitical_oil" />);
  expect(screen.getByText("원유/에너지")).toBeInTheDocument();
  expect(screen.getByText("항공/운송")).toBeInTheDocument();
  expect(screen.getByText("금/방산")).toBeInTheDocument();
});

test("renders fallback notice when category is null", () => {
  render(<IssueImpactMap category={null} />);
  expect(
    screen.getByText("이 이슈에 대한 영향 분석은 준비 중입니다."),
  ).toBeInTheDocument();
});

test("includes disclaimer", () => {
  render(<IssueImpactMap category="macro_policy" />);
  expect(
    screen.getByText("뉴스 기반 참고 정보이며 매매 추천이 아닙니다."),
  ).toBeInTheDocument();
});
```

- [ ] **Step 2: Run test, expect failure**

Run: `cd frontend/invest && npm test -- IssueImpactMap`
Expected: FAIL — module not found.

- [ ] **Step 3: Implement**

```tsx
// frontend/invest/src/components/discover/IssueImpactMap.tsx
import type { CSSProperties } from "react";
import type { NewsRadarRiskCategory } from "../../types/newsRadar";
import { lookupImpact, type ImpactPill, type ImpactTone } from "./impactMap";

const TONE_STYLES: Record<ImpactTone, CSSProperties> = {
  positive: { background: "var(--pill-mix)", color: "var(--pill-mix-fg)" },
  negative: { background: "var(--pill-toss)", color: "var(--pill-toss-fg)" },
  watch: { background: "var(--pill-up)", color: "var(--pill-up-fg)" },
};

export function IssueImpactMap({ category }: { category: NewsRadarRiskCategory | null }) {
  const pills = lookupImpact(category);
  return (
    <section aria-labelledby="impact-heading" style={{ marginTop: 16 }}>
      <h2 id="impact-heading" style={{ margin: 0, fontSize: 14, fontWeight: 700 }}>
        어떤 영향을 줄까?
      </h2>
      {pills && pills.length > 0 ? (
        <ul
          style={{
            listStyle: "none",
            padding: 0,
            margin: "8px 0 0",
            display: "flex",
            flexDirection: "column",
            gap: 8,
          }}
        >
          {pills.map((pill) => (
            <ImpactRow key={pill.theme} pill={pill} />
          ))}
        </ul>
      ) : (
        <div className="subtle" style={{ marginTop: 8 }}>
          이 이슈에 대한 영향 분석은 준비 중입니다.
        </div>
      )}
      <div className="subtle" style={{ marginTop: 12, fontSize: 11 }}>
        뉴스 기반 참고 정보이며 매매 추천이 아닙니다.
      </div>
    </section>
  );
}

function ImpactRow({ pill }: { pill: ImpactPill }) {
  return (
    <li
      style={{
        display: "flex",
        gap: 8,
        alignItems: "center",
        padding: "8px 12px",
        borderRadius: 999,
        ...TONE_STYLES[pill.tone],
        fontSize: 12,
      }}
    >
      <strong>{pill.theme}</strong>
      <span style={{ opacity: 0.85 }}>{pill.note}</span>
    </li>
  );
}
```

- [ ] **Step 4: Run test, expect pass**

Run: `cd frontend/invest && npm test -- IssueImpactMap`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add frontend/invest/src/components/discover/IssueImpactMap.tsx \
        frontend/invest/src/__tests__/IssueImpactMap.test.tsx
git commit -m "feat(rob-127): add IssueImpactMap with deterministic risk-category mapping"
```

---

## Task 11: `RelatedSymbolsList` component

**Files:**
- Create: `frontend/invest/src/components/discover/RelatedSymbolsList.tsx`
- Test: `frontend/invest/src/__tests__/RelatedSymbolsList.test.tsx`

- [ ] **Step 1: Write the failing test**

```tsx
// frontend/invest/src/__tests__/RelatedSymbolsList.test.tsx
import { render, screen } from "@testing-library/react";
import { expect, test } from "vitest";
import { RelatedSymbolsList } from "../components/discover/RelatedSymbolsList";

test("renders symbol chips when symbols are present", () => {
  render(<RelatedSymbolsList symbols={["AAPL", "MSFT"]} />);
  expect(screen.getByText("AAPL")).toBeInTheDocument();
  expect(screen.getByText("MSFT")).toBeInTheDocument();
});

test("renders prep notice when symbols are empty", () => {
  render(<RelatedSymbolsList symbols={[]} />);
  expect(screen.getByText("관련 종목 분석은 준비 중입니다.")).toBeInTheDocument();
});
```

- [ ] **Step 2: Run test, expect failure**

Run: `cd frontend/invest && npm test -- RelatedSymbolsList`
Expected: FAIL — module not found.

- [ ] **Step 3: Implement**

```tsx
// frontend/invest/src/components/discover/RelatedSymbolsList.tsx
export function RelatedSymbolsList({ symbols }: { symbols: string[] }) {
  return (
    <section aria-labelledby="symbols-heading" style={{ marginTop: 16 }}>
      <h2 id="symbols-heading" style={{ margin: 0, fontSize: 14, fontWeight: 700 }}>
        관련 종목
      </h2>
      {symbols.length > 0 ? (
        <ul
          style={{
            listStyle: "none",
            padding: 0,
            margin: "8px 0 0",
            display: "flex",
            flexWrap: "wrap",
            gap: 6,
          }}
        >
          {symbols.map((sym) => (
            <li
              key={sym}
              style={{
                padding: "4px 10px",
                background: "var(--surface-2)",
                color: "var(--text)",
                borderRadius: 999,
                fontSize: 12,
                fontWeight: 600,
              }}
            >
              {sym}
            </li>
          ))}
        </ul>
      ) : (
        <div className="subtle" style={{ marginTop: 8 }}>
          관련 종목 분석은 준비 중입니다.
        </div>
      )}
    </section>
  );
}
```

- [ ] **Step 4: Run test, expect pass**

Run: `cd frontend/invest && npm test -- RelatedSymbolsList`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add frontend/invest/src/components/discover/RelatedSymbolsList.tsx \
        frontend/invest/src/__tests__/RelatedSymbolsList.test.tsx
git commit -m "feat(rob-127): add RelatedSymbolsList with empty-state fallback"
```

---

## Task 12: `DiscoverPage`

**Files:**
- Create: `frontend/invest/src/pages/DiscoverPage.tsx`
- Test: `frontend/invest/src/__tests__/DiscoverPage.test.tsx`

- [ ] **Step 1: Write the failing test**

```tsx
// frontend/invest/src/__tests__/DiscoverPage.test.tsx
import type { ReactNode } from "react";
import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { expect, test, vi } from "vitest";
import { DiscoverPage } from "../pages/DiscoverPage";
import type { NewsRadarItem, NewsRadarResponse } from "../types/newsRadar";

function makeItem(over: Partial<NewsRadarItem>): NewsRadarItem {
  return {
    id: "i",
    title: "t",
    source: null,
    feed_source: null,
    url: "",
    published_at: null,
    market: "all",
    risk_category: null,
    severity: "low",
    themes: [],
    symbols: [],
    included_in_briefing: false,
    briefing_reason: null,
    briefing_score: 0,
    snippet: null,
    matched_terms: [],
    ...over,
  };
}

function makeResponse(items: NewsRadarItem[], over: Partial<NewsRadarResponse> = {}): NewsRadarResponse {
  return {
    market: "all",
    as_of: "2026-05-07T12:00:00Z",
    readiness: {
      status: "ready",
      latest_scraped_at: null,
      latest_published_at: null,
      recent_6h_count: 0,
      recent_24h_count: 0,
      source_count: 0,
      stale: false,
      max_age_minutes: 0,
      warnings: [],
    },
    summary: {
      high_risk_count: 0,
      total_count: items.length,
      included_in_briefing_count: 0,
      excluded_but_collected_count: 0,
    },
    sections: [],
    items,
    excluded_items: [],
    source_coverage: [],
    ...over,
  };
}

function renderWith(node: ReactNode) {
  return render(<MemoryRouter basename="/invest/app">{node}</MemoryRouter>);
}

test("renders sorted issue cards with related-news counts", () => {
  const items = [
    makeItem({ id: "a", title: "낮은 이슈", severity: "low",
               risk_category: "macro_policy", briefing_score: 1 }),
    makeItem({ id: "b", title: "높은 이슈", severity: "high",
               risk_category: "macro_policy", briefing_score: 2,
               published_at: "2026-05-07T11:55:00Z" }),
    makeItem({ id: "c", title: "지정학", severity: "high",
               risk_category: "geopolitical_oil", briefing_score: 5,
               published_at: "2026-05-07T11:00:00Z" }),
  ];
  renderWith(
    <DiscoverPage state={{ status: "ready", data: makeResponse(items) }} reload={() => {}} />,
  );

  const titles = screen.getAllByRole("link").map((a) => a.textContent ?? "");
  expect(titles[0]).toContain("지정학");
  expect(titles[1]).toContain("높은 이슈");
  expect(titles[2]).toContain("낮은 이슈");
  // macro_policy bucket has 2 items, geopolitical_oil has 1.
  expect(screen.getAllByText("관련 뉴스 2개").length).toBeGreaterThanOrEqual(2);
  expect(screen.getByText("관련 뉴스 1개")).toBeInTheDocument();
});

test("renders loading state", () => {
  renderWith(<DiscoverPage state={{ status: "loading" }} reload={() => {}} />);
  expect(screen.getByText("불러오는 중…")).toBeInTheDocument();
});

test("renders error state with retry", () => {
  const reload = vi.fn();
  renderWith(<DiscoverPage state={{ status: "error", message: "boom" }} reload={reload} />);
  expect(screen.getByText("잠시 후 다시 시도해 주세요.")).toBeInTheDocument();
  expect(screen.getByText(/boom/)).toBeInTheDocument();
  screen.getByRole("button", { name: "재시도" }).click();
  expect(reload).toHaveBeenCalled();
});

test("renders empty state when items list is empty", () => {
  renderWith(
    <DiscoverPage
      state={{ status: "ready", data: makeResponse([]) }}
      reload={() => {}}
    />,
  );
  expect(screen.getByText("표시할 이슈가 없습니다.")).toBeInTheDocument();
});

test("renders stale readiness banner", () => {
  renderWith(
    <DiscoverPage
      state={{
        status: "ready",
        data: makeResponse([], {
          readiness: {
            status: "stale",
            latest_scraped_at: null,
            latest_published_at: null,
            recent_6h_count: 0,
            recent_24h_count: 0,
            source_count: 0,
            stale: true,
            max_age_minutes: 120,
            warnings: [],
          },
        }),
      }}
      reload={() => {}}
    />,
  );
  expect(
    screen.getByText("데이터가 최신이 아닐 수 있습니다."),
  ).toBeInTheDocument();
});
```

- [ ] **Step 2: Run test, expect failure**

Run: `cd frontend/invest && npm test -- DiscoverPage`
Expected: FAIL — module not found.

- [ ] **Step 3: Implement DiscoverPage**

```tsx
// frontend/invest/src/pages/DiscoverPage.tsx
import { AppShell } from "../components/AppShell";
import { BottomNav } from "../components/BottomNav";
import { AiIssueCard } from "../components/discover/AiIssueCard";
import { AiIssueTicker } from "../components/discover/AiIssueTicker";
import { CategoryShortcutRail } from "../components/discover/CategoryShortcutRail";
import { DiscoverHeader } from "../components/discover/DiscoverHeader";
import { TodayEventCard } from "../components/discover/TodayEventCard";
import {
  countByRiskCategory,
  relatedNewsCount,
  sortIssueItems,
} from "../components/discover/severity";
import { useNewsRadar, type NewsRadarState } from "../hooks/useNewsRadar";

export interface DiscoverPageProps {
  state?: NewsRadarState;
  reload?: () => void;
}

export function DiscoverPage(props: DiscoverPageProps = {}) {
  const live = useNewsRadar({
    market: "all",
    hours: 24,
    includeExcluded: true,
    limit: 20,
  });
  const state = props.state ?? live.state;
  const reload = props.reload ?? live.reload;

  if (state.status === "loading") {
    return (
      <AppShell>
        <div className="subtle">불러오는 중…</div>
        <BottomNav />
      </AppShell>
    );
  }
  if (state.status === "error") {
    return (
      <AppShell>
        <div>잠시 후 다시 시도해 주세요.</div>
        <button type="button" onClick={reload}>
          재시도
        </button>
        <div className="subtle">{state.message}</div>
        <BottomNav />
      </AppShell>
    );
  }

  const { data } = state;
  const sorted = sortIssueItems(data.items);
  const buckets = countByRiskCategory(data.items);
  const isStale = data.readiness.status === "stale";

  return (
    <AppShell>
      <DiscoverHeader />
      <CategoryShortcutRail />
      <TodayEventCard />
      <AiIssueTicker asOf={data.as_of} />
      {isStale && (
        <div
          role="status"
          style={{
            padding: 8,
            background: "rgba(246,193,119,0.08)",
            border: "1px solid rgba(246,193,119,0.27)",
            color: "var(--warn)",
            borderRadius: 10,
            fontSize: 11,
          }}
        >
          데이터가 최신이 아닐 수 있습니다.
        </div>
      )}
      {sorted.length === 0 ? (
        <div className="subtle">표시할 이슈가 없습니다.</div>
      ) : (
        <div
          style={{
            display: "flex",
            flexDirection: "column",
            gap: 10,
            flex: 1,
            overflowY: "auto",
          }}
        >
          {sorted.map((item, idx) => (
            <AiIssueCard
              key={item.id}
              rank={idx + 1}
              item={item}
              relatedCount={relatedNewsCount(item, buckets)}
            />
          ))}
        </div>
      )}
      <BottomNav />
    </AppShell>
  );
}
```

- [ ] **Step 4: Run test, expect pass**

Run: `cd frontend/invest && npm test -- DiscoverPage`
Expected: PASS (5 tests).

- [ ] **Step 5: Commit**

```bash
git add frontend/invest/src/pages/DiscoverPage.tsx \
        frontend/invest/src/__tests__/DiscoverPage.test.tsx
git commit -m "feat(rob-127): add DiscoverPage with sorted issue list and state branches"
```

---

## Task 13: `DiscoverIssueDetailPage`

**Files:**
- Create: `frontend/invest/src/pages/DiscoverIssueDetailPage.tsx`
- Test: `frontend/invest/src/__tests__/DiscoverIssueDetailPage.test.tsx`

- [ ] **Step 1: Write the failing test**

```tsx
// frontend/invest/src/__tests__/DiscoverIssueDetailPage.test.tsx
import type { ComponentProps } from "react";
import { render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { expect, test } from "vitest";
import { DiscoverIssueDetailPage } from "../pages/DiscoverIssueDetailPage";
import type { NewsRadarItem, NewsRadarResponse } from "../types/newsRadar";

function makeItem(over: Partial<NewsRadarItem>): NewsRadarItem {
  return {
    id: "i",
    title: "t",
    source: null,
    feed_source: null,
    url: "",
    published_at: null,
    market: "all",
    risk_category: null,
    severity: "low",
    themes: [],
    symbols: [],
    included_in_briefing: false,
    briefing_reason: null,
    briefing_score: 0,
    snippet: null,
    matched_terms: [],
    ...over,
  };
}

function response(items: NewsRadarItem[]): NewsRadarResponse {
  return {
    market: "all",
    as_of: "2026-05-07T12:00:00Z",
    readiness: {
      status: "ready",
      latest_scraped_at: null,
      latest_published_at: null,
      recent_6h_count: 0,
      recent_24h_count: 0,
      source_count: 0,
      stale: false,
      max_age_minutes: 0,
      warnings: [],
    },
    summary: {
      high_risk_count: 0,
      total_count: items.length,
      included_in_briefing_count: 0,
      excluded_but_collected_count: 0,
    },
    sections: [],
    items,
    excluded_items: [],
    source_coverage: [],
  };
}

function renderAt(path: string, state: ComponentProps<typeof DiscoverIssueDetailPage>["state"]) {
  return render(
    <MemoryRouter initialEntries={[path]} basename="/invest/app">
      <Routes>
        <Route
          path="/discover/issues/:issueId"
          element={<DiscoverIssueDetailPage state={state} reload={() => {}} />}
        />
      </Routes>
    </MemoryRouter>,
  );
}

test("renders matched issue with impact map and related symbols", () => {
  const matched = makeItem({
    id: "abc",
    title: "Fed 금리",
    snippet: "정책 유지",
    risk_category: "macro_policy",
    symbols: ["SPY"],
    severity: "high",
    published_at: "2026-05-07T11:30:00Z",
    source: "Reuters",
  });
  renderAt("/discover/issues/abc", { status: "ready", data: response([matched]) });

  expect(screen.getByText("Fed 금리")).toBeInTheDocument();
  expect(screen.getByText("정책 유지")).toBeInTheDocument();
  expect(screen.getByText("금리 민감 성장주")).toBeInTheDocument();
  expect(screen.getByText("SPY")).toBeInTheDocument();
  expect(
    screen.getByText("뉴스 기반 참고 정보이며 매매 추천이 아닙니다."),
  ).toBeInTheDocument();
});

test("renders not-found state when id is missing", () => {
  renderAt("/discover/issues/missing", { status: "ready", data: response([]) });
  expect(
    screen.getByText("이슈를 찾을 수 없습니다. 시간이 지나 목록에서 빠졌을 수 있어요."),
  ).toBeInTheDocument();
  expect(screen.getByRole("link", { name: "발견으로 돌아가기" })).toHaveAttribute(
    "href",
    "/invest/app/discover",
  );
});

test("renders symbols-empty notice when item has no symbols", () => {
  const matched = makeItem({ id: "abc", title: "T", symbols: [] });
  renderAt("/discover/issues/abc", { status: "ready", data: response([matched]) });
  expect(screen.getByText("관련 종목 분석은 준비 중입니다.")).toBeInTheDocument();
});

test("renders loading and error states", () => {
  renderAt("/discover/issues/abc", { status: "loading" });
  expect(screen.getByText("불러오는 중…")).toBeInTheDocument();

  // re-render with error
  renderAt("/discover/issues/abc", { status: "error", message: "boom" });
  expect(screen.getByText("잠시 후 다시 시도해 주세요.")).toBeInTheDocument();
  expect(screen.getByText(/boom/)).toBeInTheDocument();
});
```

- [ ] **Step 2: Run test, expect failure**

Run: `cd frontend/invest && npm test -- DiscoverIssueDetailPage`
Expected: FAIL — module not found.

- [ ] **Step 3: Implement**

```tsx
// frontend/invest/src/pages/DiscoverIssueDetailPage.tsx
import { Link, useParams } from "react-router-dom";
import { AppShell } from "../components/AppShell";
import { BottomNav } from "../components/BottomNav";
import { IssueImpactMap } from "../components/discover/IssueImpactMap";
import { RelatedSymbolsList } from "../components/discover/RelatedSymbolsList";
import { describeSeverity } from "../components/discover/severity";
import { formatRelativeTime } from "../format/relativeTime";
import { useNewsRadar, type NewsRadarState } from "../hooks/useNewsRadar";

export interface DiscoverIssueDetailPageProps {
  state?: NewsRadarState;
  reload?: () => void;
}

export function DiscoverIssueDetailPage(props: DiscoverIssueDetailPageProps = {}) {
  const params = useParams<{ issueId: string }>();
  const live = useNewsRadar({
    market: "all",
    hours: 24,
    includeExcluded: true,
    limit: 20,
  });
  const state = props.state ?? live.state;
  const reload = props.reload ?? live.reload;
  const issueId = params.issueId ?? "";

  if (state.status === "loading") {
    return (
      <AppShell>
        <div className="subtle">불러오는 중…</div>
        <BottomNav />
      </AppShell>
    );
  }
  if (state.status === "error") {
    return (
      <AppShell>
        <div>잠시 후 다시 시도해 주세요.</div>
        <button type="button" onClick={reload}>
          재시도
        </button>
        <div className="subtle">{state.message}</div>
        <BottomNav />
      </AppShell>
    );
  }

  const item = state.data.items.find((i) => i.id === issueId);
  if (!item) {
    return (
      <AppShell>
        <div>이슈를 찾을 수 없습니다. 시간이 지나 목록에서 빠졌을 수 있어요.</div>
        <Link
          to="/discover"
          style={{ color: "var(--accent, #7eb6ff)", fontWeight: 700 }}
        >
          발견으로 돌아가기
        </Link>
        <BottomNav />
      </AppShell>
    );
  }

  const indicator = describeSeverity(item.severity);
  const time = formatRelativeTime(item.published_at);

  return (
    <AppShell>
      <Link to="/discover" className="subtle" style={{ textDecoration: "none" }}>
        ← 발견
      </Link>
      <header style={{ display: "flex", flexDirection: "column", gap: 6 }}>
        <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
          <span
            aria-label={indicator.label}
            role="img"
            style={{ color: indicator.color }}
          >
            {indicator.glyph}
          </span>
          <h1 style={{ margin: 0, fontSize: 18, fontWeight: 800 }}>{item.title}</h1>
        </div>
        {item.snippet && (
          <p className="subtle" style={{ margin: 0 }}>{item.snippet}</p>
        )}
        <div className="subtle" style={{ display: "flex", gap: 8, fontSize: 11 }}>
          {item.source && <span>{item.source}</span>}
          {time && <span>· {time}</span>}
        </div>
      </header>
      <IssueImpactMap category={item.risk_category} />
      <RelatedSymbolsList symbols={item.symbols} />
      <section
        style={{
          marginTop: 16,
          padding: 12,
          background: "var(--surface)",
          border: "1px solid var(--surface-2)",
          borderRadius: 12,
          fontSize: 12,
        }}
      >
        <strong style={{ display: "block", marginBottom: 4 }}>꼭 알아두세요</strong>
        <span className="subtle">
          이 화면은 read-only 정보입니다. 매수/매도 주문이나 자동 추천을 제공하지 않습니다.
        </span>
      </section>
      <BottomNav />
    </AppShell>
  );
}
```

- [ ] **Step 4: Run test, expect pass**

Run: `cd frontend/invest && npm test -- DiscoverIssueDetailPage`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add frontend/invest/src/pages/DiscoverIssueDetailPage.tsx \
        frontend/invest/src/__tests__/DiscoverIssueDetailPage.test.tsx
git commit -m "feat(rob-127): add DiscoverIssueDetailPage with id matching and fallback"
```

---

## Task 14: Register routes

**Files:**
- Modify: `frontend/invest/src/routes.tsx`
- Test: `frontend/invest/src/__tests__/routes.test.tsx`

- [ ] **Step 1: Write the failing test**

```tsx
// frontend/invest/src/__tests__/routes.test.tsx
import { expect, test } from "vitest";
import { router } from "../routes";

function pathsOf(routes: any[]): string[] {
  const out: string[] = [];
  for (const r of routes) {
    if (r.path) out.push(r.path);
    if (r.children) out.push(...pathsOf(r.children));
  }
  return out;
}

test("router exposes /discover and /discover/issues/:issueId", () => {
  const paths = pathsOf((router as any).routes);
  expect(paths).toContain("/discover");
  expect(paths).toContain("/discover/issues/:issueId");
});

test("router still exposes / and /paper", () => {
  const paths = pathsOf((router as any).routes);
  expect(paths).toContain("/");
  expect(paths).toContain("/paper");
  expect(paths).toContain("/paper/:variant");
});
```

- [ ] **Step 2: Run test, expect failure**

Run: `cd frontend/invest && npm test -- routes`
Expected: FAIL — `/discover` not present.

- [ ] **Step 3: Update `routes.tsx`**

```tsx
// frontend/invest/src/routes.tsx
import { createBrowserRouter, Navigate } from "react-router-dom";
import { DiscoverIssueDetailPage } from "./pages/DiscoverIssueDetailPage";
import { DiscoverPage } from "./pages/DiscoverPage";
import { HomePage } from "./pages/HomePage";
import { PaperPlaceholderPage } from "./pages/PaperPlaceholderPage";

export const router = createBrowserRouter(
  [
    { path: "/", element: <HomePage /> },
    { path: "/paper", element: <PaperPlaceholderPage /> },
    { path: "/paper/:variant", element: <PaperPlaceholderPage /> },
    { path: "/discover", element: <DiscoverPage /> },
    { path: "/discover/issues/:issueId", element: <DiscoverIssueDetailPage /> },
    { path: "*", element: <Navigate to="/" replace /> },
  ],
  { basename: "/invest/app" },
);
```

- [ ] **Step 4: Run test, expect pass**

Run: `cd frontend/invest && npm test -- routes`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add frontend/invest/src/routes.tsx frontend/invest/src/__tests__/routes.test.tsx
git commit -m "feat(rob-127): register /discover and /discover/issues/:issueId routes"
```

---

## Task 15: Full verification + read-only safety check

**Files:** none (verification only)

- [ ] **Step 1: Typecheck**

Run: `cd frontend/invest && npm run typecheck`
Expected: PASS (no errors).

- [ ] **Step 2: Run full test suite**

Run: `cd frontend/invest && npm test`
Expected: ALL test files pass. Take note of total counts.

- [ ] **Step 3: Build**

Run: `cd frontend/invest && npm run build`
Expected: Build completes; bundle written to `frontend/invest/dist/`.

- [ ] **Step 4: Read-only safety grep**

Run from repo root:
```bash
grep -rE "broker_|order_intent|watch_service|scheduler|app/workers|llm_" frontend/invest/src/components/discover frontend/invest/src/pages/Discover* frontend/invest/src/api/newsRadar.ts frontend/invest/src/hooks/useNewsRadar.ts || echo "OK: no forbidden imports"
```
Expected: prints `OK: no forbidden imports`.

- [ ] **Step 5: Backend lint sanity (no Python files were added, but confirm clean tree)**

Run from repo root:
```bash
git diff --check
```
Expected: no whitespace errors.

- [ ] **Step 6: Smoke check in dev server (optional, manual)**

Run: `cd frontend/invest && npm run dev`
- Open `http://localhost:<port>/invest/app/`
- Tap `발견` in BottomNav → `/invest/app/discover` loads.
- Card click → `/invest/app/discover/issues/<id>` loads.
- Refresh detail URL directly → still loads (refetch path).
- Confirm `관심`, `피드` buttons are dim and not clickable.

- [ ] **Step 7: Final commit (if any cleanup remained)**

If steps 1–6 produced no diff, skip. Otherwise:
```bash
git add -A
git commit -m "chore(rob-127): final cleanup after verification"
```

---

## Spec Coverage Map

| Spec section / AC                                                                                  | Task(s)              |
| -------------------------------------------------------------------------------------------------- | -------------------- |
| `/invest/app/discover` route renders mobile dark UI                                                | 12, 14               |
| BottomNav 발견 navigates and shows active state                                                     | 7                    |
| BottomNav 관심/피드 disabled, no alert                                                              | 7                    |
| `오늘 이벤트` placeholder card                                                                      | 8 (TodayEventCard)    |
| `AI 실시간 이슈` list with title/subtitle/related-news count/time/severity                          | 5, 9, 12              |
| Loading / error / empty states do not break                                                        | 12 (DiscoverPage tests) |
| Card click → `/discover/issues/:issueId`                                                            | 9 (link), 14         |
| Detail: summary, source/time, deterministic impact map, symbols-if-present, disclaimer              | 6, 10, 11, 13         |
| `RelatedSymbolsList` empty fallback (no fake symbols)                                              | 11, 13                |
| Tests cover route registration, list rendering, detail rendering, empty/error                      | 7, 9, 10, 11, 12, 13, 14 |
| Read-only safety boundary maintained (no broker/order/watch/scheduler/LLM imports)                  | 15 (grep)             |
| Spec decision: bucket-count display labeled "관련 뉴스 n개"                                          | 5, 9, 12              |
| Spec decision: detail page refetches news-radar (no shared cache)                                  | 13                    |
| Spec decision: Today Event = pure placeholder, no news-radar coupling                               | 8                    |
| Spec decision: BottomNav inactive tabs disabled (not alert)                                        | 7                    |

## Out-of-scope reminder

Do **not** add in this plan or any task above:
- broker submit / cancel / modify / replace
- order preview / approval / order-intent / watch creation
- scheduler / worker activation
- DB migration / backfill / direct SQL
- new LLM call / new scheduled job
- economic calendar provider integration
- realtime websocket / chart
- news clustering / source merging
- automated related-symbol recommendation
