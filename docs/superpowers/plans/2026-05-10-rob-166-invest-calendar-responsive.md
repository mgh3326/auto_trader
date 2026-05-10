# ROB-166 — `/invest/calendar` Responsive & Mobile UX Cleanup Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Linear:** [ROB-166](https://linear.app/mgh3326/issue/ROB-166) — `auto_trader: /invest/calendar 월간 캘린더 반응형·모바일 UX 정리`
**Predecessor (merged):** ROB-165 — monthly grid foundation (PR #760).
**Blocks:** ROB-167 (calendar data-source/freshness work).
**Worktree:** `/Users/mgh3326/worktrees/auto_trader/ROB-166-calendar-responsive` (pre-created — do NOT touch `/Users/mgh3326/services/auto_trader/current` or root `~/work/auto_trader`).
**Branch:** `kanban/ROB-166-calendar-responsive` (already checked out, just fast-forwarded to `origin/main`).
**Base PR target:** `main`.

**Goal:** Make the canonical `/invest/calendar` route robust across desktop and mobile after the monthly grid foundation. Tighten dense/empty rendering, overflow, KST/Korean text, touch + keyboard affordances, and bring mobile up to month-aware UX without forking the route.

**Architecture:** Frontend-only. Reuse the existing `MonthCalendarGrid` / `SelectedDateEvents` / `EventRow` / `ClusterRow` primitives. Extract a small `calendar.css` so we can use real media queries (the components currently inline every style). Replace `MobileCalendarPage`'s UTC-bug `fmt` + ad-hoc list with `fmtLocal` + the shared `SelectedDateEvents`, plus a month header that mirrors the desktop's prev/next semantics. Backend, router, and DB are untouched.

**Tech stack:** React 19 + TypeScript + Vite + Vitest 4 + React Testing Library 16 (jsdom). Tokens already exist in `src/styles/tokens.css`; new media-query rules live in a new `src/styles/calendar.css` imported once from `src/main.tsx` (or `src/styles.css`).

---

## Scope & Non-goals

In scope (mirrors Linear acceptance criteria):
- Desktop: refine the monthly grid card so it works at the `compact` viewport (900–1199px) without overflow, plus dense-day truncation.
- Mobile/responsive: month header + compact week strip + `SelectedDateEvents` (option **b** from spec — chosen for safety; preserves the existing single `/invest/calendar` route and keeps the proven week strip).
- Long titles in `EventRow` / `ClusterRow`: 1-line ellipsis on desktop, 2-line clamp on mobile, no overflow outside cards.
- Per-cell event count and `+N`/cluster affordances do not overflow the cell.
- Distinct **loading / empty / error** states for the selected-date list (today these are conflated).
- KST / Korean date-time consistency: render `eventTimeLocal` exactly when the backend supplies it, otherwise show `발표 예정 · KST` once. Add unambiguous "오늘" / "내일" prefixes for the selected-date label.
- Touch targets ≥ 44×44 on mobile interactive surfaces (date cells, prev/next, filter pills).
- Keyboard/focus: visible focus ring, `aria-pressed` on filter pills, `aria-label` + `aria-current="date"` on grid cells.
- Filters horizontally scrollable on narrow widths (no wrap, no clipping).
- Verify no nested interactive elements (clusters/events render `<article>`, not `<button>`; the grid cell `<button>` wraps day metadata only).
- Fix UTC-drift bug in `MobileCalendarPage` (`d.toISOString().slice(0,10)` → `fmtLocal`).
- Frontend typecheck / vitest / build all green.

Out of scope (DO NOT touch — Linear safety boundary):
- New ingestion sources, backend service/router/schema changes (ROB-167 owns data-source work).
- Scheduler changes, broker / order / watch / order-intent / live / paper execution.
- Direct DB UPDATE/DELETE/INSERT/backfill.
- New routes (`/invest/app/...` is deprecated and stays redirected — DO NOT add any). The monthly grid route is and stays `/invest/calendar`.

---

## Files

**Create**
- `frontend/invest/src/styles/calendar.css` — media-query-driven rules for grid density, EventRow/ClusterRow overflow, focus rings, mobile filter scroller. ~80 lines.
- `frontend/invest/src/components/calendar/CalendarMonthHeader.tsx` — shared month label + prev/next button row used by both desktop and mobile.
- `frontend/invest/src/__tests__/CalendarMonthHeader.test.tsx`
- `frontend/invest/src/__tests__/SelectedDateEvents.test.tsx` — covers loading / empty / error / populated branches + long-title truncation.
- `frontend/invest/src/__tests__/MobileCalendarPage.test.tsx` — full mobile flow: month nav, week strip, selected-date list, filter scroller, KST drift gone.
- `frontend/invest/src/__tests__/calendarKstAndDateLabel.test.ts` — vm helpers for `formatKstTime`, `relativeDayPrefix`, etc.

**Modify**
- `frontend/invest/src/components/calendar/vm.ts` — add `formatKstTime`, `relativeDayPrefix`, `selectedDateLabelWithRelative`, `monthIndexOfDate`, `clampSelectedDateToMonth`. Append-only — DO NOT modify existing exports.
- `frontend/invest/src/components/calendar/MonthCalendarGrid.tsx` — add density variants (`density?: "compact" | "comfortable"`), wrap cells with `aria-label`, `aria-current`, focus ring, and `min-width: 0` so cell text never overflows. Add `loading?: boolean` to render skeleton cells.
- `frontend/invest/src/components/calendar/SelectedDateEvents.tsx` — accept `loading?: boolean` and `error?: string | null`; render distinct loading / error / empty / populated states; tighten header for mobile widths.
- `frontend/invest/src/components/calendar/EventRow.tsx` — apply `calendar-event-row` class for media-query rules; multi-line clamp on mobile; KST suffix on time when no actual/forecast/previous; ensure `<article>` (not `<button>`) and no nested interactive children.
- `frontend/invest/src/components/calendar/ClusterRow.tsx` — same `calendar-cluster-row` class; tighten preview line to 2-line clamp; include `eventCount` in the visible chip-style affordance.
- `frontend/invest/src/pages/desktop/DesktopCalendarPage.tsx` — replace inline header with `<CalendarMonthHeader>`; pipe `loading` / `error` to children; tag root `<section className="calendar-desktop">` so the new media-queries can target compact viewport.
- `frontend/invest/src/pages/mobile/MobileCalendarPage.tsx` — full rewrite: drop UTC `fmt`, switch to `fmtLocal`; add `<CalendarMonthHeader>` above the existing `<WeekDateStrip>`; render `<SelectedDateEvents>`; horizontally scrollable filter row; touch targets ≥ 44px.
- `frontend/invest/src/styles.css` — add `@import url("./styles/calendar.css");` at the top so the new rules ship with the app shell.
- `frontend/invest/src/__tests__/DesktopCalendarPage.test.tsx` — extend existing tests with three new assertions: loading skeleton, error banner, long-title truncation. Existing 9 tests stay.

**Untouched but read for context**
- `frontend/invest/src/types/calendar.ts` — types already cover everything we need.
- `frontend/invest/src/api/calendar.ts` — request shape unchanged.
- `frontend/invest/src/hooks/useViewport.ts` — `mobile <900`, `compact 900–1199`, `desktop >=1200`.
- `frontend/invest/src/components/calendar/{EmptyEventState,RegionBadge,OwnershipTag,WeekDateStrip,AIWeeklyCard,EventDetailModal,SparkleIcon}.tsx` — reused as-is.

---

## Design notes (lock these in)

1. **Single canonical route.** `/invest/calendar` resolves to `CalendarRoute`, which switches on `useViewport()`. We DO NOT introduce `/invest/app/calendar` or any new path — Linear explicitly forbids the legacy `/invest/app` drift.
2. **Mobile choice: option (b)** — month header + `WeekDateStrip` + `SelectedDateEvents`. We do NOT shrink the desktop 6×7 grid onto a phone — it's too cramped at 360px.
3. **CSS architecture.** All structural styles stay inline in components (existing convention). Only **responsive** rules (media queries, `@supports`, focus pseudo-classes, line-clamp) move to `calendar.css`. Components opt in by adding stable class names: `calendar-grid`, `calendar-grid-cell`, `calendar-event-row`, `calendar-cluster-row`, `calendar-mobile-filters`, etc.
4. **Date math: never UTC.** All ISO date strings are produced via `fmtLocal(d)` from `vm.ts`. The existing mobile `fmt(d)` (`d.toISOString().slice(0,10)`) is removed in Task 5. Helper tests already pin `fmtLocal` — DO NOT regress.
5. **KST display.** When `eventTimeLocal` is present, render it verbatim (backend already includes locale-aware text like `오후 9시 발표 예정`). When absent **and** `actual`/`forecast`/`previous` are all null, render `formatKstTime(null) === "발표 예정 · KST"`. We do not invent times.
6. **Selected-date label gets a relative prefix.** `selectedDateLabelWithRelative("2026-05-11", today="2026-05-11") === "오늘 · 5월 11일 월요일 일정"`. `today + 1` → `"내일 · …"`. Otherwise `"5월 13일 수요일 일정"` (no prefix). Keeps the bare label test from ROB-165 passing because the *suffix* still matches `/5월 13일 수요일 일정/`.
7. **Loading vs empty vs error.** Three distinct surfaces, three distinct test ids:
   - `data-testid="calendar-loading"` — skeleton rows while a fetch is in flight.
   - `data-testid="calendar-empty"` — already exists in `EmptyEventState`; keep.
   - `data-testid="calendar-error"` — red banner with the error message; replaces today's bare `오류: …` div.
8. **Touch targets.** Every clickable element on mobile gets `min-width: 44px; min-height: 44px;` enforced via `calendar.css` so the inline-style cells (which used `aspectRatio: 1/1; minHeight: 56`) automatically comply, and so do filter pills.
9. **Stable test ids preserved (no breaking changes for cross-cutting tests):**
   - Grid: `month-grid`, `month-grid-weekday-header`, `month-grid-cell-${date}` — already shipped, keep.
   - Selected-date wrapper: `selected-date-events` and inner `day-events` — already shipped, keep.
   - Nav: `calendar-prev-month`, `calendar-next-month`, plus the existing mobile `calendar-prev-week`, `calendar-next-week`. Adding shared header MUST keep these specific ids on the visible buttons it renders so existing mobile/desktop tests do not regress.
   - Loading: `calendar-loading` (new). Error: `calendar-error` (new).
10. **Density.** Desktop ≥1200px: `density="comfortable"` (existing 56px min-height cells, count badge below day number). Compact 900–1199px and mobile: `density="compact"` (44px min-height cells, count badge as a tiny dot when ≥10, or bare number when <10). The grid is only rendered on desktop today; we add the prop now so a future mobile-grid swap is a 1-line change.

---

## Task 1 — vm.ts helpers + calendar.css scaffold (TDD)

**Files:**
- Modify: `frontend/invest/src/components/calendar/vm.ts` (append-only)
- Create: `frontend/invest/src/__tests__/calendarKstAndDateLabel.test.ts`
- Create: `frontend/invest/src/styles/calendar.css` (skeleton; rules added in later tasks)
- Modify: `frontend/invest/src/styles.css` (import the new sheet)

**Helpers to add to `vm.ts` (verbatim):**

```ts
// --- ROB-166 KST + relative-date helpers ---

/**
 * Render an event time string for KST consumers.
 * - Backend `eventTimeLocal` (e.g. "오후 9시 발표 예정") is already KST and
 *   passes through unchanged.
 * - When the backend gave us nothing AND the row is unreleased, return a
 *   stable "발표 예정 · KST" placeholder so dense days don't flicker between
 *   `null` and `발표 예정`.
 */
export function formatKstTime(eventTimeLocal: string | null | undefined): string {
  const trimmed = (eventTimeLocal ?? "").trim();
  if (trimmed.length > 0) return trimmed;
  return "발표 예정 · KST";
}

/** "오늘" if dateIso === todayIso, "내일" if dateIso === todayIso + 1d, else null. */
export function relativeDayPrefix(dateIso: string, todayIso: string): string | null {
  if (dateIso === todayIso) return "오늘";
  const t = new Date(`${todayIso}T00:00:00`);
  t.setDate(t.getDate() + 1);
  if (fmtLocal(t) === dateIso) return "내일";
  return null;
}

export function selectedDateLabelWithRelative(dateIso: string, todayIso: string): string {
  const base = selectedDateLabel(dateIso); // "5월 11일 월요일 일정" — already in vm.ts
  const prefix = relativeDayPrefix(dateIso, todayIso);
  return prefix == null ? base : `${prefix} · ${base}`;
}

/** Force `selectedDate` back into the month containing `monthCursor` if it drifted. */
export function clampSelectedDateToMonth(selectedDateIso: string, monthCursor: Date): string {
  const sel = new Date(`${selectedDateIso}T00:00:00`);
  if (
    sel.getFullYear() === monthCursor.getFullYear() &&
    sel.getMonth() === monthCursor.getMonth()
  ) {
    return selectedDateIso;
  }
  return fmtLocal(startOfMonth(monthCursor));
}
```

- [ ] **Step 1.1: Write the failing tests**

Create `frontend/invest/src/__tests__/calendarKstAndDateLabel.test.ts`:

```ts
import { describe, expect, test } from "vitest";
import {
  clampSelectedDateToMonth,
  formatKstTime,
  relativeDayPrefix,
  selectedDateLabelWithRelative,
} from "../components/calendar/vm";

describe("ROB-166 KST + relative-date helpers", () => {
  test("formatKstTime returns the backend string when present", () => {
    expect(formatKstTime("오후 9시 발표 예정")).toBe("오후 9시 발표 예정");
    expect(formatKstTime("  오전 8시  ")).toBe("오전 8시");
  });

  test("formatKstTime falls back to a single stable placeholder when null/empty", () => {
    expect(formatKstTime(null)).toBe("발표 예정 · KST");
    expect(formatKstTime(undefined)).toBe("발표 예정 · KST");
    expect(formatKstTime("")).toBe("발표 예정 · KST");
    expect(formatKstTime("   ")).toBe("발표 예정 · KST");
  });

  test("relativeDayPrefix names today/tomorrow, otherwise null", () => {
    expect(relativeDayPrefix("2026-05-11", "2026-05-11")).toBe("오늘");
    expect(relativeDayPrefix("2026-05-12", "2026-05-11")).toBe("내일");
    expect(relativeDayPrefix("2026-05-13", "2026-05-11")).toBeNull();
    // crosses month boundary
    expect(relativeDayPrefix("2026-06-01", "2026-05-31")).toBe("내일");
  });

  test("selectedDateLabelWithRelative prepends 오늘/내일 when applicable, keeps suffix", () => {
    expect(selectedDateLabelWithRelative("2026-05-11", "2026-05-11")).toBe(
      "오늘 · 5월 11일 월요일 일정",
    );
    expect(selectedDateLabelWithRelative("2026-05-12", "2026-05-11")).toBe(
      "내일 · 5월 12일 화요일 일정",
    );
    // The bare-suffix /5월 13일 수요일 일정/ regex from ROB-165 must still match.
    expect(selectedDateLabelWithRelative("2026-05-13", "2026-05-11")).toMatch(
      /5월 13일 수요일 일정/,
    );
    // Same date as today's date but ROB-165 default monthFirst case (still "오늘").
    expect(selectedDateLabelWithRelative("2026-05-01", "2026-05-01")).toBe(
      "오늘 · 5월 1일 금요일 일정",
    );
  });

  test("clampSelectedDateToMonth keeps in-range, snaps out-of-range to month-first", () => {
    const may = new Date(2026, 4, 1); // May 2026
    expect(clampSelectedDateToMonth("2026-05-13", may)).toBe("2026-05-13");
    expect(clampSelectedDateToMonth("2026-04-30", may)).toBe("2026-05-01");
    expect(clampSelectedDateToMonth("2026-06-01", may)).toBe("2026-05-01");
  });
});
```

- [ ] **Step 1.2: Confirm tests FAIL**

Run: `cd frontend/invest && npx vitest run src/__tests__/calendarKstAndDateLabel.test.ts`
Expected: every test fails with `is not a function` — helpers not exported yet.

- [ ] **Step 1.3: Append the helpers to `vm.ts`**

Append the entire helper block from "Helpers to add to `vm.ts`" above to `frontend/invest/src/components/calendar/vm.ts`. Place it AFTER the existing `selectedDateLabel` export so `selectedDateLabelWithRelative` can call it. DO NOT touch any existing export.

- [ ] **Step 1.4: Confirm tests PASS**

Run: `cd frontend/invest && npx vitest run src/__tests__/calendarKstAndDateLabel.test.ts`
Expected: 5 tests passing.

- [ ] **Step 1.5: Create `frontend/invest/src/styles/calendar.css` skeleton**

Create the file with the following placeholder (later tasks fill this in):

```css
/* =========================================================
   ROB-166 — /invest/calendar responsive rules.
   Components opt in via stable class names; structural
   styles stay inline to match the existing convention.
   Order: shared > grid > rows > mobile shell.
   ========================================================= */

/* Visible focus ring for keyboard nav on every calendar
   interactive surface. Uses --shadow-focus from tokens. */
.calendar-grid-cell:focus-visible,
.calendar-pill:focus-visible,
.calendar-nav-btn:focus-visible {
  outline: none;
  box-shadow: var(--shadow-focus);
  border-radius: 10px;
}

/* Mobile filter row scrolls horizontally instead of wrapping
   so the longest pill never causes a layout jump. */
.calendar-mobile-filters {
  display: flex;
  gap: 6px;
  overflow-x: auto;
  scrollbar-width: none;
  padding-bottom: 2px;
}
.calendar-mobile-filters::-webkit-scrollbar { display: none; }
.calendar-mobile-filters > * { flex: 0 0 auto; }
```

- [ ] **Step 1.6: Wire the new sheet into the app shell**

Edit `frontend/invest/src/styles.css`. Add this line at the very top (above `* { box-sizing: border-box; }`):

```css
@import url("./styles/calendar.css");
```

- [ ] **Step 1.7: Run the full vitest suite to confirm zero regressions**

Run: `cd frontend/invest && npm test`
Expected: every test still green; the new test file adds 5 passing tests.

- [ ] **Step 1.8: Commit**

```bash
git add frontend/invest/src/components/calendar/vm.ts \
        frontend/invest/src/__tests__/calendarKstAndDateLabel.test.ts \
        frontend/invest/src/styles/calendar.css \
        frontend/invest/src/styles.css
git commit -m "feat(invest-calendar): ROB-166 vm helpers + calendar.css scaffold"
```

---

## Task 2 — `EventRow` / `ClusterRow` mobile-safe overflow + KST time (TDD)

**Files:**
- Modify: `frontend/invest/src/components/calendar/EventRow.tsx`
- Modify: `frontend/invest/src/components/calendar/ClusterRow.tsx`
- Modify: `frontend/invest/src/styles/calendar.css` (append rules)
- Create: `frontend/invest/src/__tests__/EventRow.test.tsx`
- Create: `frontend/invest/src/__tests__/ClusterRow.test.tsx`

The two row components must (a) never overflow horizontally regardless of title length, (b) collapse the 5-column grid into a 2-line stack on mobile (<900px) so the OHLC-style numeric columns don't crowd the title to a single character, (c) render `formatKstTime` for the time line, (d) stay non-interactive (`<article>`, no nested buttons).

- [ ] **Step 2.1: Write the failing tests**

Create `frontend/invest/src/__tests__/EventRow.test.tsx`:

```tsx
import { render, screen } from "@testing-library/react";
import { describe, expect, test } from "vitest";
import { EventRow } from "../components/calendar/EventRow";
import type { CalendarEventVM } from "../components/calendar/vm";

function ev(overrides: Partial<CalendarEventVM> = {}): CalendarEventVM {
  return {
    id: "evt-1",
    date: "2026-05-13",
    dayOfMonth: 13,
    monthDay: "5/13",
    type: "earnings",
    region: "us",
    title: "AAPL Q2 earnings",
    time: "오후 9시 발표 예정",
    released: false,
    actual: null,
    forecast: null,
    previous: null,
    own: null,
    badges: [],
    ...overrides,
  };
}

describe("EventRow", () => {
  test("uses the calendar-event-row class so calendar.css media queries apply", () => {
    render(<EventRow ev={ev()} />);
    expect(screen.getByTestId("calendar-event")).toHaveClass("calendar-event-row");
  });

  test("renders no nested interactive elements (no <button>, no <a>)", () => {
    render(<EventRow ev={ev()} />);
    const row = screen.getByTestId("calendar-event");
    expect(row.tagName).toBe("ARTICLE");
    expect(row.querySelectorAll("button, a").length).toBe(0);
  });

  test("very long titles render without forcing horizontal overflow", () => {
    const longTitle = "A".repeat(200);
    render(<EventRow ev={ev({ title: longTitle })} />);
    const titleNode = screen.getByText(longTitle);
    expect(titleNode).toHaveClass("calendar-event-row__title");
  });

  test("renders formatKstTime fallback when time + actuals are all null", () => {
    render(<EventRow ev={ev({ time: null })} />);
    expect(screen.getByText("발표 예정 · KST")).toBeInTheDocument();
  });

  test("uses backend-provided eventTimeLocal verbatim when present", () => {
    render(<EventRow ev={ev({ time: "오전 8시 30분 발표" })} />);
    expect(screen.getByText("오전 8시 30분 발표")).toBeInTheDocument();
  });
});
```

Create `frontend/invest/src/__tests__/ClusterRow.test.tsx`:

```tsx
import { render, screen } from "@testing-library/react";
import { describe, expect, test } from "vitest";
import { ClusterRow } from "../components/calendar/ClusterRow";
import type { CalendarClusterVM, CalendarEventVM } from "../components/calendar/vm";

function topEvent(title: string, id: string): CalendarEventVM {
  return {
    id, date: "2026-05-13", dayOfMonth: 13, monthDay: "5/13",
    type: "earnings", region: "us", title,
    time: null, released: false, actual: null, forecast: null, previous: null,
    own: null, badges: [],
  };
}

function cluster(overrides: Partial<CalendarClusterVM> = {}): CalendarClusterVM {
  return {
    id: "c1",
    date: "2026-05-13",
    dayOfMonth: 13,
    monthDay: "5/13",
    type: "earnings",
    region: "us",
    title: "미국 실적 발표 327건",
    count: 327,
    topEvents: [topEvent("AAPL", "e1"), topEvent("MSFT", "e2"), topEvent("GOOGL", "e3")],
    ...overrides,
  };
}

describe("ClusterRow", () => {
  test("uses the calendar-cluster-row class for media-query rules", () => {
    render(<ClusterRow cluster={cluster()} />);
    expect(screen.getByTestId("calendar-cluster")).toHaveClass("calendar-cluster-row");
  });

  test("title and preview line both opt into __title / __preview classes", () => {
    render(<ClusterRow cluster={cluster()} />);
    expect(screen.getByText("미국 실적 발표 327건")).toHaveClass("calendar-cluster-row__title");
    // Preview line is the line with the dot-joined top events.
    expect(screen.getByText(/AAPL · MSFT · GOOGL 외/)).toHaveClass("calendar-cluster-row__preview");
  });

  test("count chip is visible separately from the title for narrow screens", () => {
    render(<ClusterRow cluster={cluster({ count: 327 })} />);
    expect(screen.getByTestId("calendar-cluster-count")).toHaveTextContent("+327");
  });

  test("does not nest interactive elements", () => {
    render(<ClusterRow cluster={cluster()} />);
    const row = screen.getByTestId("calendar-cluster");
    expect(row.tagName).toBe("ARTICLE");
    expect(row.querySelectorAll("button, a").length).toBe(0);
  });

  test("falls back to '상세 일정 묶음' if topEvents is empty", () => {
    render(<ClusterRow cluster={cluster({ topEvents: [] })} />);
    expect(screen.getByText("상세 일정 묶음")).toBeInTheDocument();
  });
});
```

- [ ] **Step 2.2: Confirm both test files FAIL**

Run: `cd frontend/invest && npx vitest run src/__tests__/EventRow.test.tsx src/__tests__/ClusterRow.test.tsx`
Expected: failures — classes / count chip not present yet.

- [ ] **Step 2.3: Rewrite `EventRow.tsx`**

Overwrite `frontend/invest/src/components/calendar/EventRow.tsx` with:

```tsx
import type { CalendarEventVM } from "./vm";
import { formatKstTime } from "./vm";
import { RegionBadge } from "./RegionBadge";
import { OwnershipTag } from "./OwnershipTag";

export function EventRow({ ev }: { ev: CalendarEventVM }) {
  const showFallbackTime =
    ev.time == null && ev.actual == null && ev.forecast == null && ev.previous == null;
  const timeText = ev.time ?? (ev.released ? "발표 완료" : showFallbackTime ? formatKstTime(null) : "발표 예정");

  return (
    <article
      className="calendar-event-row"
      data-testid="calendar-event"
      data-event-id={ev.id}
      data-event-type={ev.type}
      data-relation={ev.own ?? "none"}
    >
      <div className="calendar-event-row__day">{ev.monthDay}</div>
      <div className="calendar-event-row__main">
        <div className="calendar-event-row__title-line">
          <RegionBadge region={ev.region} />
          <span className="calendar-event-row__title" title={ev.title}>{ev.title}</span>
          <OwnershipTag own={ev.own} />
        </div>
        <div
          className="calendar-event-row__time"
          data-released={ev.released ? "true" : "false"}
        >
          {timeText}
        </div>
      </div>
      <div className="calendar-event-row__num calendar-event-row__num--actual" data-released={ev.released ? "true" : "false"}>
        {ev.actual ?? "—"}
      </div>
      <div className="calendar-event-row__num calendar-event-row__num--forecast">
        {ev.forecast ?? "—"}
      </div>
      <div className="calendar-event-row__num calendar-event-row__num--previous">
        {ev.previous ?? "—"}
      </div>
    </article>
  );
}
```

- [ ] **Step 2.4: Rewrite `ClusterRow.tsx`**

Overwrite `frontend/invest/src/components/calendar/ClusterRow.tsx` with:

```tsx
import type { CalendarClusterVM } from "./vm";
import { RegionBadge } from "./RegionBadge";

export function ClusterRow({ cluster }: { cluster: CalendarClusterVM }) {
  const previewText =
    cluster.topEvents.length > 0
      ? `${cluster.topEvents.map((e) => e.title).join(" · ")}${cluster.count > cluster.topEvents.length ? " 외" : ""}`
      : "상세 일정 묶음";

  return (
    <article
      className="calendar-cluster-row"
      data-testid="calendar-cluster"
      data-cluster-id={cluster.id}
      data-event-type={cluster.type}
      data-region={cluster.region}
    >
      <div className="calendar-cluster-row__day">{cluster.monthDay}</div>
      <div className="calendar-cluster-row__main">
        <div className="calendar-cluster-row__title-line">
          <RegionBadge region={cluster.region} />
          <span className="calendar-cluster-row__title" title={cluster.title}>
            {cluster.title}
          </span>
          <span data-testid="calendar-cluster-count" className="calendar-cluster-row__count">
            +{cluster.count}
          </span>
        </div>
        <div className="calendar-cluster-row__preview" title={previewText}>
          {previewText}
        </div>
      </div>
    </article>
  );
}
```

- [ ] **Step 2.5: Append rules to `calendar.css`**

Append to `frontend/invest/src/styles/calendar.css`:

```css
/* ---------- EventRow / ClusterRow shared shape ---------- */
.calendar-event-row,
.calendar-cluster-row {
  display: grid;
  grid-template-columns: 44px minmax(0, 1fr) 76px 76px 76px;
  align-items: center;
  gap: 10px;
  padding: 10px 12px;
  border-radius: 10px;
  background: transparent;
  min-width: 0;
}
.calendar-cluster-row {
  background: var(--surface-2);
  padding: 12px;
  grid-template-columns: 44px minmax(0, 1fr);
}
.calendar-event-row__day,
.calendar-cluster-row__day {
  font-size: 12px;
  font-weight: 700;
  color: var(--fg-1);
  font-feature-settings: "tnum";
}
.calendar-event-row__main,
.calendar-cluster-row__main {
  min-width: 0;
}
.calendar-event-row__title-line,
.calendar-cluster-row__title-line {
  display: flex;
  align-items: center;
  gap: 8px;
  min-width: 0;
}
.calendar-event-row__title,
.calendar-cluster-row__title {
  font-size: 14px;
  font-weight: 600;
  color: var(--fg);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  min-width: 0;
  flex: 1 1 auto;
}
.calendar-cluster-row__title { font-weight: 700; }
.calendar-cluster-row__count {
  flex: 0 0 auto;
  font-size: 11px;
  font-weight: 700;
  color: var(--accent-press);
  background: var(--accent-soft);
  border-radius: 999px;
  padding: 1px 8px;
  font-feature-settings: "tnum";
}
.calendar-event-row__time {
  font-size: 11px;
  color: var(--fg-3);
  margin-top: 2px;
}
.calendar-event-row__time[data-released="true"] { color: var(--fg-2); }
.calendar-cluster-row__preview {
  font-size: 12px;
  color: var(--fg-3);
  margin-top: 4px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.calendar-event-row__num {
  text-align: right;
  font-size: 13px;
  font-feature-settings: "tnum";
  color: var(--fg-2);
}
.calendar-event-row__num--actual {
  font-weight: 700;
  color: var(--fg);
}
.calendar-event-row__num--actual[data-released="false"] { color: var(--fg-3); }
.calendar-event-row__num--previous { color: var(--fg-3); }

/* ---------- Mobile (<900px): collapse numeric columns into a stacked detail line ---------- */
@media (max-width: 899px) {
  .calendar-event-row {
    grid-template-columns: 44px minmax(0, 1fr);
  }
  .calendar-event-row__num {
    display: none;
  }
  .calendar-event-row__title {
    white-space: normal;
    display: -webkit-box;
    -webkit-line-clamp: 2;
    -webkit-box-orient: vertical;
    overflow: hidden;
  }
  .calendar-cluster-row__title,
  .calendar-cluster-row__preview {
    white-space: normal;
    display: -webkit-box;
    -webkit-line-clamp: 2;
    -webkit-box-orient: vertical;
    overflow: hidden;
  }
}
```

(Note: jsdom does NOT apply CSS, so the row tests assert on the *class names*, not the visual `-webkit-line-clamp` outcome. Visual smoke is covered in Task 7.)

- [ ] **Step 2.6: Run row tests, confirm PASS**

Run: `cd frontend/invest && npx vitest run src/__tests__/EventRow.test.tsx src/__tests__/ClusterRow.test.tsx`
Expected: 10 tests passing (5 EventRow, 5 ClusterRow).

- [ ] **Step 2.7: Run the full suite — DesktopCalendarPage tests must still pass**

Run: `cd frontend/invest && npm test`
Expected: all green. If `DesktopCalendarPage.test.tsx` (the existing one) starts failing because `미국 실적 발표 327건` is now followed by `+327` chip text, fix the assertion only if needed (use `getByText("미국 실적 발표 327건", { exact: true })`). DO NOT change unrelated tests.

- [ ] **Step 2.8: Commit**

```bash
git add frontend/invest/src/components/calendar/EventRow.tsx \
        frontend/invest/src/components/calendar/ClusterRow.tsx \
        frontend/invest/src/styles/calendar.css \
        frontend/invest/src/__tests__/EventRow.test.tsx \
        frontend/invest/src/__tests__/ClusterRow.test.tsx
git commit -m "feat(invest-calendar): ROB-166 row overflow + KST time fallback"
```

---

## Task 3 — `SelectedDateEvents` distinct loading / empty / error states (TDD)

**Files:**
- Modify: `frontend/invest/src/components/calendar/SelectedDateEvents.tsx`
- Create: `frontend/invest/src/__tests__/SelectedDateEvents.test.tsx`
- Modify: `frontend/invest/src/styles/calendar.css` (append loading/error rules)

The wrapper currently only knows "have data" vs "empty". After this task it knows: `loading` → skeleton; `error` → red banner; populated → rows; empty → `EmptyEventState`. Header is responsive (stacked on mobile).

- [ ] **Step 3.1: Write the failing tests**

Create `frontend/invest/src/__tests__/SelectedDateEvents.test.tsx`:

```tsx
import { render, screen, within } from "@testing-library/react";
import { describe, expect, test } from "vitest";
import { SelectedDateEvents } from "../components/calendar/SelectedDateEvents";
import type { CalendarClusterVM, CalendarEventVM } from "../components/calendar/vm";

const baseProps = {
  dateLabel: "5월 11일 월요일 일정",
  dateIso: "2026-05-11",
  events: [] as CalendarEventVM[],
  clusters: [] as CalendarClusterVM[],
  emptyMessage: "선택한 날짜에 일정이 없습니다.",
};

function evt(id: string, title: string): CalendarEventVM {
  return {
    id, date: "2026-05-11", dayOfMonth: 11, monthDay: "5/11",
    type: "earnings", region: "us", title,
    time: null, released: false, actual: null, forecast: null, previous: null,
    own: null, badges: [],
  };
}

describe("SelectedDateEvents", () => {
  test("loading state renders skeleton, not empty/error/populated", () => {
    render(<SelectedDateEvents {...baseProps} loading />);
    expect(screen.getByTestId("calendar-loading")).toBeInTheDocument();
    expect(screen.queryByTestId("calendar-empty")).not.toBeInTheDocument();
    expect(screen.queryByTestId("calendar-error")).not.toBeInTheDocument();
    expect(screen.queryByTestId("calendar-event")).not.toBeInTheDocument();
  });

  test("error state renders the error banner with the message", () => {
    render(<SelectedDateEvents {...baseProps} error="서버에 연결할 수 없습니다" />);
    const banner = screen.getByTestId("calendar-error");
    expect(banner).toHaveTextContent("서버에 연결할 수 없습니다");
    expect(screen.queryByTestId("calendar-loading")).not.toBeInTheDocument();
    expect(screen.queryByTestId("calendar-empty")).not.toBeInTheDocument();
  });

  test("empty state renders EmptyEventState with the configured message", () => {
    render(<SelectedDateEvents {...baseProps} />);
    expect(screen.getByTestId("calendar-empty")).toHaveTextContent(
      "선택한 날짜에 일정이 없습니다.",
    );
  });

  test("populated state renders events and exposes day-events test id", () => {
    render(
      <SelectedDateEvents
        {...baseProps}
        events={[evt("e1", "AAPL earnings"), evt("e2", "MSFT earnings")]}
      />,
    );
    const list = screen.getByTestId("day-events");
    expect(within(list).getAllByTestId("calendar-event")).toHaveLength(2);
    expect(screen.queryByTestId("calendar-empty")).not.toBeInTheDocument();
  });

  test("header includes dateIso, dateLabel, and total count even with clusters", () => {
    render(
      <SelectedDateEvents
        {...baseProps}
        clusters={[
          {
            id: "c1", date: "2026-05-11", dayOfMonth: 11, monthDay: "5/11",
            type: "earnings", region: "us", title: "미국 실적 발표 327건",
            count: 327, topEvents: [],
          },
        ]}
      />,
    );
    const root = screen.getByTestId("selected-date-events");
    expect(root).toHaveAttribute("data-selected-date", "2026-05-11");
    expect(root).toHaveTextContent("5월 11일 월요일 일정");
    expect(root).toHaveTextContent("327건");
  });

  test("loading + populated together still shows skeleton (loading wins)", () => {
    render(
      <SelectedDateEvents
        {...baseProps}
        loading
        events={[evt("e1", "stale")]}
      />,
    );
    expect(screen.getByTestId("calendar-loading")).toBeInTheDocument();
    expect(screen.queryByText("stale")).not.toBeInTheDocument();
  });

  test("error wins over populated (so users see the failure, not stale data)", () => {
    render(
      <SelectedDateEvents
        {...baseProps}
        error="boom"
        events={[evt("e1", "stale")]}
      />,
    );
    expect(screen.getByTestId("calendar-error")).toBeInTheDocument();
    expect(screen.queryByText("stale")).not.toBeInTheDocument();
  });
});
```

- [ ] **Step 3.2: Confirm tests FAIL**

Run: `cd frontend/invest && npx vitest run src/__tests__/SelectedDateEvents.test.tsx`
Expected: failures around `loading`, `error`, and `calendar-loading` / `calendar-error` ids.

- [ ] **Step 3.3: Rewrite `SelectedDateEvents.tsx`**

Overwrite `frontend/invest/src/components/calendar/SelectedDateEvents.tsx` with:

```tsx
import { ClusterRow } from "./ClusterRow";
import { EventRow } from "./EventRow";
import { EmptyEventState } from "./EmptyEventState";
import type { CalendarClusterVM, CalendarEventVM } from "./vm";

export interface SelectedDateEventsProps {
  dateLabel: string;
  dateIso: string;
  events: CalendarEventVM[];
  clusters: CalendarClusterVM[];
  emptyMessage: string;
  loading?: boolean;
  error?: string | null;
}

export function SelectedDateEvents({
  dateLabel,
  dateIso,
  events,
  clusters,
  emptyMessage,
  loading = false,
  error = null,
}: SelectedDateEventsProps) {
  const total = events.length + clusters.reduce((s, c) => s + c.count, 0);

  return (
    <div
      className="calendar-selected-date"
      data-testid="selected-date-events"
      data-selected-date={dateIso}
    >
      <div className="calendar-selected-date__header">
        <h2 className="calendar-selected-date__label">{dateLabel}</h2>
        <span className="calendar-selected-date__meta">
          {dateIso} · {total}건
        </span>
      </div>
      <div data-testid="day-events" className="calendar-selected-date__list">
        {loading ? (
          <SkeletonRows />
        ) : error ? (
          <ErrorBanner message={error} />
        ) : events.length === 0 && clusters.length === 0 ? (
          <EmptyEventState message={emptyMessage} />
        ) : (
          <>
            {clusters.map((c) => (
              <ClusterRow key={c.id} cluster={c} />
            ))}
            {events.map((ev) => (
              <EventRow key={ev.id} ev={ev} />
            ))}
          </>
        )}
      </div>
    </div>
  );
}

function SkeletonRows() {
  return (
    <div data-testid="calendar-loading" className="calendar-loading">
      {Array.from({ length: 3 }, (_, i) => (
        <div key={i} className="calendar-loading__row" aria-hidden="true" />
      ))}
      <span className="calendar-loading__sr">일정을 불러오는 중입니다…</span>
    </div>
  );
}

function ErrorBanner({ message }: { message: string }) {
  return (
    <div data-testid="calendar-error" role="alert" className="calendar-error">
      <strong className="calendar-error__title">일정을 불러올 수 없습니다</strong>
      <span className="calendar-error__detail">{message}</span>
    </div>
  );
}
```

- [ ] **Step 3.4: Append loading/error/header rules to `calendar.css`**

Append to `frontend/invest/src/styles/calendar.css`:

```css
/* ---------- SelectedDateEvents ---------- */
.calendar-selected-date {
  display: flex;
  flex-direction: column;
  gap: 8px;
  min-width: 0;
}
.calendar-selected-date__header {
  display: flex;
  align-items: baseline;
  gap: 8px;
  padding: 0 6px 8px;
  flex-wrap: wrap;
}
.calendar-selected-date__label {
  margin: 0;
  font-size: 15px;
  font-weight: 800;
  color: var(--fg);
}
.calendar-selected-date__meta {
  font-size: 12px;
  color: var(--fg-3);
  font-feature-settings: "tnum";
}
.calendar-selected-date__list {
  display: flex;
  flex-direction: column;
  gap: 4px;
  min-width: 0;
}

/* Mobile: stack the header so the meta line wraps cleanly. */
@media (max-width: 899px) {
  .calendar-selected-date__header {
    flex-direction: column;
    align-items: flex-start;
    gap: 2px;
    padding: 0 4px 6px;
  }
}

/* ---------- Loading skeleton + error banner ---------- */
.calendar-loading { display: flex; flex-direction: column; gap: 6px; padding: 4px 0; }
.calendar-loading__row {
  height: 44px;
  border-radius: 10px;
  background: linear-gradient(90deg, var(--surface-2) 0%, var(--surface-3) 50%, var(--surface-2) 100%);
  background-size: 200% 100%;
  animation: calendar-shimmer 1.4s ease-in-out infinite;
}
@keyframes calendar-shimmer {
  0% { background-position: 200% 0; }
  100% { background-position: -200% 0; }
}
.calendar-loading__sr {
  position: absolute;
  width: 1px; height: 1px; padding: 0; margin: -1px;
  overflow: hidden; clip: rect(0 0 0 0); white-space: nowrap; border: 0;
}
.calendar-error {
  display: flex; flex-direction: column; gap: 4px;
  padding: 12px 14px;
  border: 1px solid var(--danger);
  background: var(--danger-soft);
  border-radius: 10px;
  color: var(--danger);
}
.calendar-error__title { font-size: 13px; font-weight: 700; }
.calendar-error__detail { font-size: 12px; color: var(--fg-2); }
```

- [ ] **Step 3.5: Run the new test file, confirm PASS**

Run: `cd frontend/invest && npx vitest run src/__tests__/SelectedDateEvents.test.tsx`
Expected: 7 tests passing.

- [ ] **Step 3.6: Run the full suite — confirm DesktopCalendarPage tests still pass**

Run: `cd frontend/invest && npm test`
Expected: all green. The existing `DesktopCalendarPage.test.tsx` does not pass `loading` or `error`, so the wrapper's defaults (`loading=false, error=null`) preserve current behavior — `calendar-empty` continues to render for empty-day cases.

- [ ] **Step 3.7: Commit**

```bash
git add frontend/invest/src/components/calendar/SelectedDateEvents.tsx \
        frontend/invest/src/styles/calendar.css \
        frontend/invest/src/__tests__/SelectedDateEvents.test.tsx
git commit -m "feat(invest-calendar): ROB-166 SelectedDateEvents loading/error states"
```

---

## Task 4 — `MonthCalendarGrid` density, focus, aria, count-badge overflow guard (TDD)

**Files:**
- Modify: `frontend/invest/src/components/calendar/MonthCalendarGrid.tsx`
- Modify: `frontend/invest/src/__tests__/MonthCalendarGrid.test.tsx` (append new tests)
- Modify: `frontend/invest/src/styles/calendar.css` (append grid rules)

The grid currently inlines all styles, has no focus ring, no `aria-*`, and renders `327` as plain text inside a flex column — fine on desktop, but at compact widths the digit can collide with the day number. We add `density`, opt the cells into stable classes, render `+999` truncation, and provide aria/keyboard niceties.

- [ ] **Step 4.1: Append failing tests to `MonthCalendarGrid.test.tsx`**

Add to the bottom of `frontend/invest/src/__tests__/MonthCalendarGrid.test.tsx`:

```tsx
test("each cell carries the calendar-grid-cell class for media-query rules", () => {
  render(<MonthCalendarGrid {...baseProps} onSelect={() => {}} />);
  const cell = screen.getByTestId("month-grid-cell-2026-05-13");
  expect(cell).toHaveClass("calendar-grid-cell");
});

test("cells expose aria-label and aria-current for screen readers", () => {
  render(<MonthCalendarGrid {...baseProps} onSelect={() => {}} />);
  const today = screen.getByTestId("month-grid-cell-2026-05-11");
  expect(today.getAttribute("aria-current")).toBe("date");
  // aria-label includes year/month/day in Korean and the count.
  expect(today.getAttribute("aria-label")).toMatch(/2026.*5.*11.*3/);
});

test("count badge renders +999 for any count >= 1000 to keep cells from overflowing", () => {
  render(
    <MonthCalendarGrid
      {...baseProps}
      countByDate={new Map([["2026-05-13", 1234]])}
      onSelect={() => {}}
    />,
  );
  expect(screen.getByTestId("month-grid-cell-2026-05-13")).toHaveTextContent("+999");
});

test("density='compact' stamps a data-density attribute on the root for media-query targeting", () => {
  render(<MonthCalendarGrid {...baseProps} density="compact" onSelect={() => {}} />);
  expect(screen.getByTestId("month-grid")).toHaveAttribute("data-density", "compact");
});

test("loading=true renders 42 skeleton cells with no count badges", () => {
  render(<MonthCalendarGrid {...baseProps} loading onSelect={() => {}} />);
  const skeletons = screen.getAllByTestId(/^month-grid-cell-skeleton-/);
  expect(skeletons).toHaveLength(42);
  expect(screen.queryByText("327")).not.toBeInTheDocument();
});
```

(Keep all five pre-existing tests in the file — they describe contract that has not changed.)

- [ ] **Step 4.2: Confirm new tests FAIL**

Run: `cd frontend/invest && npx vitest run src/__tests__/MonthCalendarGrid.test.tsx`
Expected: the original 5 still pass; the 5 new tests fail.

- [ ] **Step 4.3: Rewrite `MonthCalendarGrid.tsx`**

Overwrite `frontend/invest/src/components/calendar/MonthCalendarGrid.tsx` with:

```tsx
import { fmtLocal, gridStartFromMonth, startOfMonth } from "./vm";

const WEEKDAY_LABELS = ["일", "월", "화", "수", "목", "금", "토"] as const;

export type MonthGridDensity = "comfortable" | "compact";

export interface MonthCalendarGridProps {
  monthCursor: Date;
  selectedDate: string;
  today: string;
  countByDate: Map<string, number>;
  onSelect: (date: string) => void;
  density?: MonthGridDensity;
  loading?: boolean;
}

function clampCount(n: number): string {
  if (n >= 1000) return "+999";
  return String(n);
}

function ariaLabel(iso: string, count: number, isToday: boolean): string {
  const [y, m, d] = iso.split("-");
  const y2 = Number.parseInt(y ?? "0", 10);
  const m2 = Number.parseInt(m ?? "0", 10);
  const d2 = Number.parseInt(d ?? "0", 10);
  const todayPart = isToday ? " (오늘)" : "";
  const countPart = count > 0 ? `, 일정 ${count}건` : "";
  return `${y2}년 ${m2}월 ${d2}일${todayPart}${countPart}`;
}

export function MonthCalendarGrid({
  monthCursor,
  selectedDate,
  today,
  countByDate,
  onSelect,
  density = "comfortable",
  loading = false,
}: MonthCalendarGridProps) {
  const gridStart = gridStartFromMonth(monthCursor);
  const monthFirst = startOfMonth(monthCursor);
  const monthIndex = monthFirst.getMonth();

  const cells: { iso: string; day: number; outOfMonth: boolean }[] = [];
  for (let i = 0; i < 42; i += 1) {
    const d = new Date(gridStart);
    d.setDate(d.getDate() + i);
    cells.push({ iso: fmtLocal(d), day: d.getDate(), outOfMonth: d.getMonth() !== monthIndex });
  }

  return (
    <div
      className="calendar-grid"
      data-testid="month-grid"
      data-density={density}
      role="grid"
      aria-label="월간 캘린더"
    >
      <div className="calendar-grid__weekdays" data-testid="month-grid-weekday-header" role="row">
        {WEEKDAY_LABELS.map((w) => (
          <span key={w} role="columnheader" aria-label={w}>{w}</span>
        ))}
      </div>
      <div className="calendar-grid__cells" role="rowgroup">
        {cells.map((c, idx) => {
          if (loading) {
            return (
              <div
                key={c.iso}
                data-testid={`month-grid-cell-skeleton-${idx}`}
                className="calendar-grid-cell calendar-grid-cell--skeleton"
                aria-hidden="true"
              />
            );
          }
          const isToday = c.iso === today;
          const isSelected = c.iso === selectedDate;
          const count = countByDate.get(c.iso) ?? 0;
          return (
            <button
              key={c.iso}
              type="button"
              className="calendar-grid-cell"
              data-testid={`month-grid-cell-${c.iso}`}
              data-date={c.iso}
              data-today={isToday ? "true" : "false"}
              data-selected={isSelected ? "true" : "false"}
              data-out-of-month={c.outOfMonth ? "true" : "false"}
              aria-current={isToday ? "date" : undefined}
              aria-pressed={isSelected ? "true" : "false"}
              aria-label={ariaLabel(c.iso, count, isToday)}
              onClick={() => onSelect(c.iso)}
            >
              <span className="calendar-grid-cell__day">{c.day}</span>
              {count > 0 && (
                <span className="calendar-grid-cell__count" aria-hidden="true">
                  {clampCount(count)}
                </span>
              )}
            </button>
          );
        })}
      </div>
    </div>
  );
}
```

- [ ] **Step 4.4: Append grid rules to `calendar.css`**

Append to `frontend/invest/src/styles/calendar.css`:

```css
/* ---------- Month grid ---------- */
.calendar-grid {
  display: flex;
  flex-direction: column;
  gap: 4px;
  min-width: 0;
}
.calendar-grid__weekdays {
  display: grid;
  grid-template-columns: repeat(7, 1fr);
  font-size: 11px;
  font-weight: 600;
  color: var(--fg-3);
  text-align: center;
  padding: 0 2px;
}
.calendar-grid__cells {
  display: grid;
  grid-template-columns: repeat(7, 1fr);
  gap: 4px;
}
.calendar-grid-cell {
  aspect-ratio: 1 / 1;
  min-height: 56px;
  min-width: 0;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: flex-start;
  gap: 2px;
  padding: 8px 4px;
  border: none;
  border-radius: 10px;
  cursor: pointer;
  font-family: inherit;
  background: transparent;
  overflow: hidden;
}
.calendar-grid-cell[data-selected="true"] { background: var(--surface-2); }
.calendar-grid-cell[data-out-of-month="true"] { opacity: 0.35; }
.calendar-grid-cell__day {
  width: 26px;
  height: 26px;
  border-radius: 999px;
  display: grid;
  place-items: center;
  color: var(--fg-1);
  font-weight: 500;
  font-size: 13px;
  font-feature-settings: "tnum";
}
.calendar-grid-cell[data-selected="true"] .calendar-grid-cell__day {
  background: var(--accent);
  color: var(--fg-on-accent);
  font-weight: 700;
}
.calendar-grid-cell[data-today="true"]:not([data-selected="true"]) .calendar-grid-cell__day {
  color: var(--accent);
  font-weight: 700;
}
.calendar-grid-cell__count {
  font-size: 10px;
  font-weight: 600;
  color: var(--fg-3);
  font-feature-settings: "tnum";
  max-width: 100%;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.calendar-grid-cell--skeleton {
  cursor: default;
  background: linear-gradient(90deg, var(--surface-2) 0%, var(--surface-3) 50%, var(--surface-2) 100%);
  background-size: 200% 100%;
  animation: calendar-shimmer 1.4s ease-in-out infinite;
}

/* Compact density (mobile or compact viewport) shrinks cell metrics. */
.calendar-grid[data-density="compact"] .calendar-grid-cell {
  min-height: 44px;
  padding: 4px 2px;
  border-radius: 8px;
}
.calendar-grid[data-density="compact"] .calendar-grid-cell__day {
  width: 22px; height: 22px; font-size: 12px;
}
.calendar-grid[data-density="compact"] .calendar-grid-cell__count { font-size: 9px; }

/* Compact viewport (900-1199): also force compact density even if the
   parent passed comfortable. Keeps the desktop card readable. */
@media (max-width: 1199px) {
  .calendar-grid .calendar-grid-cell { min-height: 48px; }
  .calendar-grid .calendar-grid-cell__day { width: 24px; height: 24px; }
}
```

- [ ] **Step 4.5: Run grid tests, confirm all 10 PASS**

Run: `cd frontend/invest && npx vitest run src/__tests__/MonthCalendarGrid.test.tsx`
Expected: 10 tests passing.

- [ ] **Step 4.6: Run full suite — DesktopCalendarPage tests still green**

Run: `cd frontend/invest && npm test`
Expected: all green. (Existing assertions inspect `data-today`, `data-selected`, `data-out-of-month` and the inner `13` / `327` text — all unchanged.)

- [ ] **Step 4.7: Commit**

```bash
git add frontend/invest/src/components/calendar/MonthCalendarGrid.tsx \
        frontend/invest/src/styles/calendar.css \
        frontend/invest/src/__tests__/MonthCalendarGrid.test.tsx
git commit -m "feat(invest-calendar): ROB-166 grid density + a11y + count clamp"
```

---

## Task 5 — `MobileCalendarPage` rewrite: month header, fmtLocal, shared `SelectedDateEvents` (TDD)

**Files:**
- Create: `frontend/invest/src/components/calendar/CalendarMonthHeader.tsx`
- Create: `frontend/invest/src/__tests__/CalendarMonthHeader.test.tsx`
- Modify: `frontend/invest/src/pages/mobile/MobileCalendarPage.tsx` (full rewrite)
- Create: `frontend/invest/src/__tests__/MobileCalendarPage.test.tsx`
- Modify: `frontend/invest/src/styles/calendar.css` (append mobile shell rules)

This is the biggest user-visible change. We move mobile from a UTC-buggy week-only view to a month-aware view that still uses `WeekDateStrip` for the date picker (familiar, fits a phone), with a month header row that lets users skip whole months and a reused `SelectedDateEvents` for the bottom list.

### Task 5a — `CalendarMonthHeader` component

- [ ] **Step 5a.1: Write failing tests**

Create `frontend/invest/src/__tests__/CalendarMonthHeader.test.tsx`:

```tsx
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, test, vi } from "vitest";
import { CalendarMonthHeader } from "../components/calendar/CalendarMonthHeader";

describe("CalendarMonthHeader", () => {
  test("renders the title and prev/next buttons with stable test ids", () => {
    render(
      <CalendarMonthHeader
        title="2026년 5월"
        onPrev={() => {}}
        onNext={() => {}}
      />,
    );
    expect(screen.getByText("2026년 5월")).toBeInTheDocument();
    expect(screen.getByTestId("calendar-prev-month")).toHaveAttribute("aria-label", "이전 달");
    expect(screen.getByTestId("calendar-next-month")).toHaveAttribute("aria-label", "다음 달");
  });

  test("clicking prev/next fires the callbacks", async () => {
    const onPrev = vi.fn();
    const onNext = vi.fn();
    const user = userEvent.setup();
    render(<CalendarMonthHeader title="2026년 5월" onPrev={onPrev} onNext={onNext} />);
    await user.click(screen.getByTestId("calendar-prev-month"));
    await user.click(screen.getByTestId("calendar-next-month"));
    expect(onPrev).toHaveBeenCalledTimes(1);
    expect(onNext).toHaveBeenCalledTimes(1);
  });

  test("buttons opt into the calendar-nav-btn class for focus-ring rules", () => {
    render(<CalendarMonthHeader title="t" onPrev={() => {}} onNext={() => {}} />);
    expect(screen.getByTestId("calendar-prev-month")).toHaveClass("calendar-nav-btn");
    expect(screen.getByTestId("calendar-next-month")).toHaveClass("calendar-nav-btn");
  });
});
```

- [ ] **Step 5a.2: Confirm fail; create the component**

Run: `cd frontend/invest && npx vitest run src/__tests__/CalendarMonthHeader.test.tsx`
Expected: module-not-found.

Create `frontend/invest/src/components/calendar/CalendarMonthHeader.tsx`:

```tsx
import { Icon } from "../../ds";

export interface CalendarMonthHeaderProps {
  title: string;
  onPrev: () => void;
  onNext: () => void;
}

export function CalendarMonthHeader({ title, onPrev, onNext }: CalendarMonthHeaderProps) {
  return (
    <div className="calendar-month-header">
      <button
        type="button"
        className="calendar-nav-btn"
        aria-label="이전 달"
        data-testid="calendar-prev-month"
        onClick={onPrev}
      >
        <Icon name="chev" size={14} />
      </button>
      <div className="calendar-month-header__title">{title}</div>
      <button
        type="button"
        className="calendar-nav-btn calendar-nav-btn--flip"
        aria-label="다음 달"
        data-testid="calendar-next-month"
        onClick={onNext}
      >
        <Icon name="chev" size={14} />
      </button>
    </div>
  );
}
```

- [ ] **Step 5a.3: Append rules to `calendar.css`**

Append:

```css
/* ---------- CalendarMonthHeader ---------- */
.calendar-month-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}
.calendar-month-header__title {
  font-size: 14px;
  font-weight: 700;
  letter-spacing: -0.01em;
  text-align: center;
  flex: 1 1 auto;
  min-width: 0;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.calendar-nav-btn {
  width: 44px;
  height: 44px;
  border: none;
  background: transparent;
  border-radius: 8px;
  cursor: pointer;
  color: var(--fg-2);
  display: grid;
  place-items: center;
  flex: 0 0 auto;
}
.calendar-nav-btn--flip { transform: scaleX(-1); }
.calendar-nav-btn:hover { background: var(--surface-2); }

/* On desktop card, the nav buttons can be smaller — they live inside
   a tight 300px aside. */
@media (min-width: 1200px) {
  .calendar-month-header .calendar-nav-btn { width: 28px; height: 28px; }
}
```

- [ ] **Step 5a.4: Confirm header tests PASS**

Run: `cd frontend/invest && npx vitest run src/__tests__/CalendarMonthHeader.test.tsx`
Expected: 3 tests passing.

### Task 5b — `MobileCalendarPage` rewrite

- [ ] **Step 5b.1: Write failing tests**

Create `frontend/invest/src/__tests__/MobileCalendarPage.test.tsx`:

```tsx
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { vi, beforeEach, afterEach, test, expect } from "vitest";
import { MemoryRouter } from "react-router-dom";
import { MobileCalendarPage } from "../pages/mobile/MobileCalendarPage";
import * as calApi from "../api/calendar";
import type { CalendarEvent, CalendarResponse } from "../types/calendar";

function event(
  o: Partial<CalendarEvent> & Pick<CalendarEvent, "eventId" | "title" | "market" | "eventType">,
): CalendarEvent {
  return { source: "fixture", relatedSymbols: [], relation: "none", badges: [], ...o };
}

const calendarFixture: CalendarResponse = {
  tab: "all",
  fromDate: "2026-04-26",
  toDate: "2026-06-06",
  asOf: "2026-05-11T03:00:00.000Z",
  days: [
    {
      date: "2026-05-11",
      events: [event({ eventId: "e1", title: "AAPL earnings", market: "us", eventType: "earnings" })],
      clusters: [],
    },
    {
      date: "2026-05-13",
      events: [],
      clusters: [
        {
          clusterId: "c1", label: "US earnings", eventType: "earnings", market: "us",
          eventCount: 327,
          topEvents: [event({ eventId: "t1", title: "AAPL", market: "us", eventType: "earnings" })],
        },
      ],
    },
  ],
  meta: { warnings: [] },
};

function wrap(ui: React.ReactElement) {
  return (
    <MemoryRouter basename="/invest" initialEntries={["/invest/calendar"]}>
      {ui}
    </MemoryRouter>
  );
}

beforeEach(() => {
  vi.useFakeTimers({ toFake: ["Date"] });
  vi.setSystemTime(new Date("2026-05-11T12:00:00+09:00"));
  vi.spyOn(calApi, "fetchCalendar").mockResolvedValue(calendarFixture);
  vi.spyOn(calApi, "fetchWeeklySummary").mockResolvedValue({
    weekStart: "2026-05-11",
    asOf: new Date().toISOString(),
    sections: [],
    partial: false,
    missingDates: [],
  });
});

afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
});

test("requests the full month grid range (Sun-aligned 6 weeks) on mount, NOT a single week", async () => {
  render(wrap(<MobileCalendarPage />));
  await waitFor(() => {
    expect(calApi.fetchCalendar).toHaveBeenCalledWith({
      fromDate: "2026-04-26",
      toDate: "2026-06-06",
      tab: "all",
    });
  });
});

test("renders CalendarMonthHeader with the current month title and selected-date list", async () => {
  render(wrap(<MobileCalendarPage />));
  expect(await screen.findByText("2026년 5월")).toBeInTheDocument();
  expect(screen.getByTestId("calendar-prev-month")).toBeInTheDocument();
  expect(screen.getByTestId("calendar-next-month")).toBeInTheDocument();
  expect(await screen.findByTestId("selected-date-events")).toHaveAttribute(
    "data-selected-date",
    "2026-05-11",
  );
});

test("WeekDateStrip is still rendered for the week containing today", async () => {
  render(wrap(<MobileCalendarPage />));
  expect(await screen.findByTestId("week-date-strip")).toBeInTheDocument();
});

test("prev/next month re-fetches the new Sun-aligned grid range", async () => {
  const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
  render(wrap(<MobileCalendarPage />));
  await waitFor(() => expect(calApi.fetchCalendar).toHaveBeenCalledTimes(1));

  await user.click(screen.getByTestId("calendar-next-month"));
  await waitFor(() =>
    expect(calApi.fetchCalendar).toHaveBeenLastCalledWith({
      fromDate: "2026-05-31",
      toDate: "2026-07-11",
      tab: "all",
    }),
  );
});

test("does NOT use UTC fmt — selected date uses fmtLocal even in non-UTC timezone", async () => {
  // 2026-05-11 in KST equals 2026-05-10 in UTC; fmtLocal must give 2026-05-11.
  render(wrap(<MobileCalendarPage />));
  const list = await screen.findByTestId("selected-date-events");
  expect(list).toHaveAttribute("data-selected-date", "2026-05-11");
});

test("filter pills live in a horizontally-scrollable container (not flex-wrap)", async () => {
  render(wrap(<MobileCalendarPage />));
  const filters = await screen.findByTestId("calendar-mobile-filters");
  expect(filters).toHaveClass("calendar-mobile-filters");
  // 3 pills.
  expect(within(filters).getAllByRole("button")).toHaveLength(3);
});

test("error response surfaces the calendar-error banner, not the empty state", async () => {
  vi.spyOn(calApi, "fetchCalendar").mockRejectedValueOnce(new Error("boom"));
  render(wrap(<MobileCalendarPage />));
  expect(await screen.findByTestId("calendar-error")).toHaveTextContent("boom");
  expect(screen.queryByTestId("calendar-empty")).not.toBeInTheDocument();
});

test("touches an in-month date in the strip and updates the selected-date list", async () => {
  const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
  render(wrap(<MobileCalendarPage />));
  await screen.findByTestId("selected-date-events");
  // 2026-05-13 is in the same week (Mon=11) so it should be in the strip.
  await user.click(screen.getByTestId("day-2026-05-13"));
  await waitFor(() =>
    expect(screen.getByTestId("selected-date-events")).toHaveAttribute(
      "data-selected-date",
      "2026-05-13",
    ),
  );
  expect(screen.getByText("미국 실적 발표 327건")).toBeInTheDocument();
});

test("includes 오늘 prefix on the selected-date label when today is selected", async () => {
  render(wrap(<MobileCalendarPage />));
  expect(await screen.findByText(/오늘 · 5월 11일 월요일 일정/)).toBeInTheDocument();
});
```

- [ ] **Step 5b.2: Confirm tests FAIL**

Run: `cd frontend/invest && npx vitest run src/__tests__/MobileCalendarPage.test.tsx`
Expected: most tests fail because the existing mobile page fetches a 7-day range, has no month header, no `selected-date-events` wrapper, and uses UTC `fmt`.

- [ ] **Step 5b.3: Rewrite `MobileCalendarPage.tsx`**

Overwrite `frontend/invest/src/pages/mobile/MobileCalendarPage.tsx` with:

```tsx
import { useEffect, useMemo, useState } from "react";
import { MobileShell } from "../../mobile/MobileShell";
import { fetchCalendar, fetchWeeklySummary } from "../../api/calendar";
import type { CalendarResponse, WeeklySummaryResponse } from "../../types/calendar";
import { Icon } from "../../ds";
import { CalendarMonthHeader } from "../../components/calendar/CalendarMonthHeader";
import { WeekDateStrip } from "../../components/calendar/WeekDateStrip";
import { SelectedDateEvents } from "../../components/calendar/SelectedDateEvents";
import { EventDetailModal } from "../../components/calendar/EventDetailModal";
import { SparkleIcon } from "../../components/calendar/SparkleIcon";
import {
  addMonths,
  clampSelectedDateToMonth,
  fmtLocal,
  gridEndFromMonth,
  gridStartFromMonth,
  monthTitleLabel,
  selectedDateLabelWithRelative,
  startOfMonth,
  toClusterVM,
  toEventVM,
  weekStartOf,
  type CalendarClusterVM,
  type CalendarEventVM,
  type DisplayEventType,
  type DisplayRegion,
} from "../../components/calendar/vm";
import type { CalendarDay } from "../../types/calendar";

type TypeFilter = "all" | DisplayEventType;
type RegionFilter = "all" | DisplayRegion;

function weekStartDateOf(dateIso: string): Date {
  const d = new Date(`${dateIso}T00:00:00`);
  const offset = (d.getDay() + 6) % 7; // Mon=0
  d.setDate(d.getDate() - offset);
  d.setHours(0, 0, 0, 0);
  return d;
}

function buildWeekDays(weekStart: Date, calendarDays: CalendarDay[]): CalendarDay[] {
  const byDate = new Map(calendarDays.map((d) => [d.date, d]));
  const out: CalendarDay[] = [];
  for (let i = 0; i < 7; i += 1) {
    const d = new Date(weekStart);
    d.setDate(d.getDate() + i);
    const iso = fmtLocal(d);
    out.push(byDate.get(iso) ?? { date: iso, events: [], clusters: [] });
  }
  return out;
}

function matches(
  item: { type: DisplayEventType; region: DisplayRegion },
  typeFilter: TypeFilter,
  regionFilter: RegionFilter,
): boolean {
  if (typeFilter !== "all" && item.type !== typeFilter) return false;
  if (regionFilter !== "all" && item.region !== regionFilter) return false;
  return true;
}

export function MobileCalendarPage() {
  const [monthCursor, setMonthCursor] = useState<Date>(() => startOfMonth(new Date()));
  const gridStart = useMemo(() => gridStartFromMonth(monthCursor), [monthCursor]);
  const gridEnd = useMemo(() => gridEndFromMonth(monthCursor), [monthCursor]);
  const today = fmtLocal(new Date());

  const [selectedDate, setSelectedDate] = useState<string>(() => {
    const now = new Date();
    if (now.getFullYear() === monthCursor.getFullYear() && now.getMonth() === monthCursor.getMonth()) {
      return fmtLocal(now);
    }
    return fmtLocal(monthCursor);
  });

  const [calendar, setCalendar] = useState<CalendarResponse | undefined>();
  const [calendarLoading, setCalendarLoading] = useState(true);
  const [calendarErr, setCalendarErr] = useState<string | null>(null);
  const [summary, setSummary] = useState<WeeklySummaryResponse | undefined>();
  const [summaryErr, setSummaryErr] = useState<string | undefined>();
  const [summaryLoading, setSummaryLoading] = useState(false);
  const [showSummary, setShowSummary] = useState(false);
  const [typeFilter, setTypeFilter] = useState<TypeFilter>("all");
  const [regionFilter, setRegionFilter] = useState<RegionFilter>("all");

  useEffect(() => {
    let cancel = false;
    setCalendar(undefined);
    setCalendarLoading(true);
    setCalendarErr(null);
    fetchCalendar({ fromDate: fmtLocal(gridStart), toDate: fmtLocal(gridEnd), tab: "all" })
      .then((r) => {
        if (cancel) return;
        setCalendar(r);
        setCalendarLoading(false);
      })
      .catch((e) => {
        if (cancel) return;
        setCalendarErr(String(e?.message ?? e));
        setCalendarLoading(false);
      });
    return () => {
      cancel = true;
    };
  }, [gridStart, gridEnd]);

  const summaryWeekStart = useMemo(() => weekStartOf(selectedDate), [selectedDate]);
  useEffect(() => {
    if (!showSummary) return;
    if (summary && summary.weekStart === summaryWeekStart) return;
    let cancel = false;
    setSummary(undefined);
    setSummaryErr(undefined);
    setSummaryLoading(true);
    fetchWeeklySummary(summaryWeekStart)
      .then((r) => {
        if (cancel) return;
        setSummary(r);
        setSummaryLoading(false);
      })
      .catch((e) => {
        if (cancel) return;
        setSummaryErr(String(e?.message ?? e));
        setSummaryLoading(false);
      });
    return () => {
      cancel = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [showSummary, summaryWeekStart]);

  const weekDays = useMemo(
    () => buildWeekDays(weekStartDateOf(selectedDate), calendar?.days ?? []),
    [calendar?.days, selectedDate],
  );

  const filteredSelected = useMemo(() => {
    const day = (calendar?.days ?? []).find((d) => d.date === selectedDate);
    if (!day) return { events: [] as CalendarEventVM[], clusters: [] as CalendarClusterVM[] };
    const events = day.events
      .map((e) => toEventVM(e, day.date))
      .filter((e) => matches(e, typeFilter, regionFilter));
    const clusters = day.clusters
      .map((c) => toClusterVM(c, day.date))
      .filter((c) => matches(c, typeFilter, regionFilter));
    return { events, clusters };
  }, [calendar?.days, selectedDate, typeFilter, regionFilter]);

  const goPrevMonth = () => {
    setMonthCursor((m) => {
      const next = addMonths(m, -1);
      setSelectedDate((sel) => clampSelectedDateToMonth(sel, next));
      return next;
    });
  };
  const goNextMonth = () => {
    setMonthCursor((m) => {
      const next = addMonths(m, 1);
      setSelectedDate((sel) => clampSelectedDateToMonth(sel, next));
      return next;
    });
  };

  return (
    <>
      <MobileShell title="캘린더">
        <div className="calendar-mobile">
          <CalendarMonthHeader
            title={monthTitleLabel(fmtLocal(monthCursor))}
            onPrev={goPrevMonth}
            onNext={goNextMonth}
          />

          <WeekDateStrip
            days={weekDays}
            selectedDate={selectedDate}
            onSelect={setSelectedDate}
            today={today}
          />

          <button
            type="button"
            data-testid="open-weekly-summary"
            onClick={() => setShowSummary(true)}
            className="calendar-mobile__ai-btn"
          >
            <SparkleIcon size={14} />
            이번주 AI 요약
            <Icon name="chev" size={12} />
          </button>

          <div data-testid="calendar-mobile-filters" className="calendar-mobile-filters">
            {(
              [
                ["all", "전체"],
                ["macro", "경제지표"],
                ["earnings", "실적"],
              ] as const
            ).map(([k, l]) => {
              const on = typeFilter === k;
              return (
                <button
                  key={k}
                  type="button"
                  className="calendar-pill"
                  data-on={on ? "true" : "false"}
                  aria-pressed={on}
                  onClick={() => setTypeFilter(k)}
                >
                  {l}
                </button>
              );
            })}
          </div>

          <SelectedDateEvents
            dateLabel={selectedDateLabelWithRelative(selectedDate, today)}
            dateIso={selectedDate}
            events={filteredSelected.events}
            clusters={filteredSelected.clusters}
            emptyMessage="해당 날짜에는 일정이 없습니다."
            loading={calendarLoading}
            error={calendarErr}
          />
        </div>
      </MobileShell>
      {showSummary && (
        <EventDetailModal
          summary={summary}
          loading={summaryLoading}
          error={summaryErr}
          onClose={() => setShowSummary(false)}
        />
      )}
    </>
  );
}
```

- [ ] **Step 5b.4: Append mobile-shell rules to `calendar.css`**

Append:

```css
/* ---------- Mobile calendar shell ---------- */
.calendar-mobile {
  padding: 12px 16px 24px;
  display: flex;
  flex-direction: column;
  gap: 14px;
  min-width: 0;
}
.calendar-mobile__ai-btn {
  border: none;
  background: var(--surface-2);
  padding: 12px 16px;
  border-radius: 12px;
  cursor: pointer;
  display: inline-flex;
  align-items: center;
  gap: 8px;
  font-family: inherit;
  font-size: 13px;
  font-weight: 700;
  color: var(--accent-press);
  align-self: flex-start;
  min-height: 44px;
}
.calendar-pill {
  padding: 8px 14px;
  border: none;
  border-radius: 999px;
  cursor: pointer;
  background: var(--surface-2);
  color: var(--fg-2);
  font-weight: 600;
  font-size: 13px;
  font-family: inherit;
  min-height: 44px;
}
.calendar-pill[data-on="true"] {
  background: var(--fg);
  color: var(--bg);
}
```

- [ ] **Step 5b.5: Confirm Mobile tests PASS**

Run: `cd frontend/invest && npx vitest run src/__tests__/MobileCalendarPage.test.tsx`
Expected: 9 tests passing.

- [ ] **Step 5b.6: Run full suite to confirm desktop tests still pass**

Run: `cd frontend/invest && npm test`
Expected: every previous test still green. The legacy mobile assertion `calendar-prev-week` / `calendar-next-week` no longer exists — verify no test references them. (Search: `grep -rn "calendar-prev-week\|calendar-next-week" frontend/invest/src/__tests__/` should return zero hits.)

- [ ] **Step 5b.7: Commit**

```bash
git add frontend/invest/src/components/calendar/CalendarMonthHeader.tsx \
        frontend/invest/src/pages/mobile/MobileCalendarPage.tsx \
        frontend/invest/src/styles/calendar.css \
        frontend/invest/src/__tests__/CalendarMonthHeader.test.tsx \
        frontend/invest/src/__tests__/MobileCalendarPage.test.tsx
git commit -m "feat(invest-calendar): ROB-166 mobile month header + fmtLocal + shared list"
```

---

## Task 6 — `DesktopCalendarPage` swap inline header for `CalendarMonthHeader`, wire loading/error, add 3 new assertions (TDD)

**Files:**
- Modify: `frontend/invest/src/pages/desktop/DesktopCalendarPage.tsx`
- Modify: `frontend/invest/src/__tests__/DesktopCalendarPage.test.tsx` (append 3 tests)

The desktop page already has prev/next buttons inline. Swap them out for the shared `CalendarMonthHeader` (preserves `calendar-prev-month` / `calendar-next-month` test ids), pass `loading` / `error` to `SelectedDateEvents`, and switch to `selectedDateLabelWithRelative` so today's label reads `오늘 · …`. The new tests cover loading, error, and the relative-prefix label.

- [ ] **Step 6.1: Append failing tests to `DesktopCalendarPage.test.tsx`**

Add to the bottom of `frontend/invest/src/__tests__/DesktopCalendarPage.test.tsx`:

```tsx
test("renders the calendar-loading skeleton while the first fetch is in flight", async () => {
  // Stall the fetch so the skeleton is visible.
  let resolve: (v: typeof calendarFixture) => void;
  vi.spyOn(calApi, "fetchCalendar").mockImplementationOnce(
    () => new Promise((r) => { resolve = r; }),
  );
  render(wrap(<DesktopCalendarPage />));
  expect(await screen.findByTestId("calendar-loading")).toBeInTheDocument();
  // Resolve and verify it goes away.
  resolve!(calendarFixture);
  await waitFor(() =>
    expect(screen.queryByTestId("calendar-loading")).not.toBeInTheDocument(),
  );
});

test("renders calendar-error banner when fetchCalendar rejects", async () => {
  vi.spyOn(calApi, "fetchCalendar").mockRejectedValueOnce(new Error("network blew up"));
  render(wrap(<DesktopCalendarPage />));
  const banner = await screen.findByTestId("calendar-error");
  expect(banner).toHaveTextContent("network blew up");
  // Empty state must not render — error wins.
  expect(screen.queryByTestId("calendar-empty")).not.toBeInTheDocument();
});

test("today's selected-date label includes the 오늘 prefix", async () => {
  render(wrap(<DesktopCalendarPage />));
  // selectedDate defaults to today (2026-05-11 — Monday).
  expect(await screen.findByText(/오늘 · 5월 11일 월요일 일정/)).toBeInTheDocument();
});
```

- [ ] **Step 6.2: Confirm 3 new tests FAIL**

Run: `cd frontend/invest && npx vitest run src/__tests__/DesktopCalendarPage.test.tsx`
Expected: 9 originals pass, 3 new ones fail (no `calendar-loading`, no `calendar-error`, label still bare).

- [ ] **Step 6.3: Edit `DesktopCalendarPage.tsx`**

Make exactly these edits to `frontend/invest/src/pages/desktop/DesktopCalendarPage.tsx`:

1. Add the import at the top of the imports block:

```tsx
import { CalendarMonthHeader } from "../../components/calendar/CalendarMonthHeader";
```

2. Add `selectedDateLabelWithRelative` to the existing `vm` import (alongside `selectedDateLabel` — keep the latter only if still referenced, otherwise drop it to avoid an unused import):

```tsx
import {
  addMonths,
  fmtLocal,
  gridEndFromMonth,
  gridStartFromMonth,
  monthLabel,
  monthTitleLabel,
  selectedDateLabelWithRelative,
  startOfMonth,
  toClusterVM,
  toEventVM,
  weekStartOf,
  type CalendarClusterVM,
  type CalendarEventVM,
  type DisplayEventType,
  type DisplayRegion,
} from "../../components/calendar/vm";
```

3. Add a `calendarLoading` state alongside `calendar`/`calendarErr`:

```tsx
const [calendar, setCalendar] = useState<CalendarResponse | undefined>();
const [calendarLoading, setCalendarLoading] = useState(true);
const [calendarErr, setCalendarErr] = useState<string | null>(null);
```

(Note: `calendarErr` changes from `string | undefined` to `string | null`. Update the catch handler to `setCalendarErr(String(e?.message ?? e))` and the success handler to `setCalendarErr(null)`.)

4. Wrap the fetch effect to update `calendarLoading`:

```tsx
useEffect(() => {
  let cancel = false;
  setCalendar(undefined);
  setCalendarLoading(true);
  setCalendarErr(null);
  fetchCalendar({ fromDate: fmtLocal(gridStart), toDate: fmtLocal(gridEnd), tab: "all" })
    .then((r) => {
      if (cancel) return;
      setCalendar(r);
      setCalendarLoading(false);
    })
    .catch((e) => {
      if (cancel) return;
      setCalendarErr(String(e?.message ?? e));
      setCalendarLoading(false);
    });
  return () => {
    cancel = true;
  };
}, [gridStart, gridEnd]);
```

5. Replace the inline header `<div style="display:flex;align-items:center;justify-content:space-between;...">…prev/next buttons…</div>` block (currently spanning roughly lines 159–190) with:

```tsx
<CalendarMonthHeader
  title={monthTitleLabel(monthFirstIso)}
  onPrev={goPrevMonth}
  onNext={goNextMonth}
/>
```

6. Replace the bare `{calendarErr && <div style={{ color: "var(--danger)" }}>오류: {calendarErr}</div>}` block with nothing — error rendering moves into `SelectedDateEvents`. (Or keep a `null` fallback so the prior block's surrounding layout doesn't shift.)

7. Update the `SelectedDateEvents` JSX to pass `loading`, `error`, and the relative label:

```tsx
<SelectedDateEvents
  dateLabel={selectedDateLabelWithRelative(selectedDate, today)}
  dateIso={selectedDate}
  events={selectedDay.events}
  clusters={selectedDay.clusters}
  emptyMessage="선택한 날짜에 일정이 없습니다."
  loading={calendarLoading}
  error={calendarErr}
/>
```

8. Delete the now-unused `navBtnStyle` constant (the buttons live inside `CalendarMonthHeader` and use `.calendar-nav-btn` instead).

- [ ] **Step 6.4: Confirm all 12 desktop tests PASS**

Run: `cd frontend/invest && npx vitest run src/__tests__/DesktopCalendarPage.test.tsx`
Expected: 12 tests passing (9 original + 3 new).

- [ ] **Step 6.5: Run full suite**

Run: `cd frontend/invest && npm test`
Expected: every test green.

- [ ] **Step 6.6: Commit**

```bash
git add frontend/invest/src/pages/desktop/DesktopCalendarPage.tsx \
        frontend/invest/src/__tests__/DesktopCalendarPage.test.tsx
git commit -m "feat(invest-calendar): ROB-166 desktop loading/error + shared month header"
```

---

## Task 7 — Quality gates, manual smoke, PR, deploy

- [ ] **Step 7.1: Run frontend type-check + production build**

Run:
```bash
cd /Users/mgh3326/worktrees/auto_trader/ROB-166-calendar-responsive/frontend/invest
npm run build
```
Expected: tsc emits no errors, Vite produces a build under `frontend/invest/dist/`.

- [ ] **Step 7.2: Run repo-wide lint (Python touched? — no, but lint is cheap and catches accidental edits)**

Run:
```bash
cd /Users/mgh3326/worktrees/auto_trader/ROB-166-calendar-responsive
make lint
```
Expected: green. If anything Python complains, you accidentally changed a backend file — revert it (this PR is frontend-only).

- [ ] **Step 7.3: Manual smoke in dev (recommended; skip with a note in the PR if no local backend)**

Run:
```bash
cd /Users/mgh3326/worktrees/auto_trader/ROB-166-calendar-responsive/frontend/invest
npm run dev
```
Open `/invest/calendar` in three viewport sizes (use DevTools device toolbar):
- **Desktop (≥1200px)**: 6×7 grid in the left aside; selected-date list in the main column with the 5-column EventRow grid. Prev/next month re-fetches; today shows the accent ring; selecting another day moves the selection. `오늘 · …` prefix appears when today is selected. Long titles ellipsis without overflowing. Filter pills wrap onto a single row.
- **Compact desktop (1024×768)**: grid cells get smaller (compact density via media query); EventRow numeric columns still fit; AI weekly card still visible.
- **Mobile (375×812 — iPhone SE)**: month header at top; week strip below it; AI button; filter pills horizontally scrollable; selected-date list below. `오늘 · …` prefix on today; long titles wrap to 2 lines; clicking a day in the strip updates the list. Tap the next-month button — the strip jumps to the first week of the next month and the list updates.

Confirm in DevTools console: zero React warnings, zero `validateDOMNesting` warnings (no nested interactive elements).

- [ ] **Step 7.4: Final review checklist**

Re-read these files and confirm:
- `MobileCalendarPage.tsx` no longer contains `d.toISOString()` anywhere.
- `DesktopCalendarPage.tsx` no longer inlines its prev/next buttons (uses `<CalendarMonthHeader>`).
- `vm.ts` only has additive changes — every pre-ROB-166 export still resolves.
- `calendar.css` is imported once (top of `styles.css`); no component re-imports it.
- All `calendar-*` test ids documented in the Design notes resolve in the rendered DOM.
- `grep -rn "data-testid=\"calendar-prev-week\"\|data-testid=\"calendar-next-week\"" frontend/invest/src/` returns **zero** matches (those ids no longer exist in any component or test).

- [ ] **Step 7.5: Push branch and open PR**

```bash
cd /Users/mgh3326/worktrees/auto_trader/ROB-166-calendar-responsive
git push -u origin kanban/ROB-166-calendar-responsive
gh pr create --base main --title "feat(invest): ROB-166 /invest/calendar responsive + mobile UX cleanup" --body "$(cat <<'EOF'
## Summary
- Mobile calendar: month header + week strip + shared SelectedDateEvents list. Fixes UTC drift bug from the old `d.toISOString().slice(0,10)` formatter (selected dates now use `fmtLocal`).
- Desktop & mobile share `CalendarMonthHeader` and `SelectedDateEvents` — consistent affordances, distinct loading / empty / error states (new `calendar-loading` and `calendar-error` test ids).
- Long titles in `EventRow` / `ClusterRow` clamp to 2 lines on mobile, ellipsis on desktop. Cluster `+N` chip is visible separately so dense days don't collide with the title.
- `MonthCalendarGrid` gains `density`, focus ring, `aria-label` / `aria-current="date"`, count clamp at `+999`.
- Today's selected-date label gets the `오늘 · …` / `내일 · …` prefix via `selectedDateLabelWithRelative`.
- New `calendar.css` houses every responsive rule (media queries, focus styles, mobile filter scroller). Component inline styles stay for atomic shape.
- Single canonical `/invest/calendar` route preserved — no `/invest/app` drift.

## Test plan
- [x] `cd frontend/invest && npm test`
- [x] `cd frontend/invest && npm run build`
- [x] `make lint` (no Python touched)
- [ ] Manual smoke at 1440px / 1024px / 375px viewports — confirm grid density, no overflow, KST labels, focus ring, touch targets
- [ ] After merge: `main → production` deploy, then read-only smoke (open `/invest/calendar` desktop and mobile-emulated, no broker side effects)

Linear: [ROB-166](https://linear.app/mgh3326/issue/ROB-166)
EOF
)"
```

- [ ] **Step 7.6: Integrator handles CI / merge / deploy / smoke**

After CI green and review approval, the integrator (different role) squash-merges to `main`, then merges `main → production` for one read-only deploy. Read-only smoke = open `/invest/calendar` on production with an authenticated session, plus a 360-wide responsive emulation, verify no console errors and that the grid + selected-date list render. **No broker / order / watch / paper / live side effects**.

---

## Acceptance checkpoint mapping (Linear → tasks)

| Linear acceptance criterion | Verified in |
|---|---|
| `/invest/calendar` no longer breaks on common desktop and mobile widths | Task 4 (compact density), Task 5 (mobile rewrite), Task 7 (manual smoke at 1440 / 1024 / 375) |
| Long event names do not overflow outside cards/grid cells | Task 2 (EventRow / ClusterRow class-based clamp), Task 4 (count clamp `+999`) |
| Event-heavy dates remain understandable with counts/clusters/top event preview | Task 2 (visible `+N` chip on cluster), Task 4 (cell count clamp) |
| Empty calendar states are explicit and not confused with loading or errors | Task 3 (distinct `calendar-loading` / `calendar-empty` / `calendar-error`), Task 5 + 6 (loading/error wired in pages) |
| Mobile and desktop share the canonical `/invest/calendar` route — no `/invest/app` legacy drift | Task 5 (mobile rewrite stays on `MobileCalendarPage` mounted by existing `CalendarRoute`); confirmed by `legacyAppRedirects.test.tsx` already in suite |
| Frontend typecheck/tests/build pass | Task 7 step 7.1 |
| Touch targets / keyboard / focus | Task 4 (`aria-current`, `aria-pressed`, focus ring on cells), Task 5 (44px nav + filter pills, `aria-pressed` on filters), `calendar.css` `.calendar-grid-cell:focus-visible` rule |
| Korean / KST display consistency | Task 1 (`formatKstTime`, `selectedDateLabelWithRelative`), Task 2 (EventRow KST fallback), Task 5/6 (pages use the relative label) |
| PR from worktree branch | Task 7 step 7.5 (push + gh pr create) |
| Production smoke after merge | Task 7 step 7.6 (integrator) |

## Risk notes

1. **Existing test fragility — `미국 실적 발표 327건` text node.** Task 2 adds a separate `+327` chip beside the cluster title. If any `getByText("미국 실적 발표 327건")` assertion in a test gets confused by partial-text matches, switch to `{ exact: true }` — but as of ROB-165 the cluster title is its own `<span>`, so the text-node match remains unique and tests should keep working without changes. Verify in Step 2.7.
2. **`calendarErr` type change.** Desktop currently uses `string | undefined`; ROB-166 unifies on `string | null` to match `SelectedDateEvents`. The catch handler now sets `null` on success. If TypeScript flags any consumer (`calendarErr && …`), update to `calendarErr != null && …`. The Step 6.3 instructions cover this for the desktop file; nothing else consumes the field.
3. **CSS load order.** Adding `@import url("./styles/calendar.css");` to the **top** of `styles.css` is mandatory — `@import` only works at the top of a stylesheet. If the import is misplaced, all `.calendar-*` rules silently no-op and the only visible failure is the visual smoke step. The unit tests rely on **class names being attached** (jsdom doesn't apply CSS), so they will still pass even if `@import` is wrong.
4. **`legacyAppRedirects.test.tsx` mocks `CalendarRoute`.** That test mocks the entire module (`vi.mock("../pages/desktop/DesktopCalendarPage", …)`), so changes inside `DesktopCalendarPage` cannot break it. Mobile rewrite likewise can't break it.
5. **MobileCalendarPage previously matched a 7-day window.** Now it requests a 42-day window. Backend response will be ~6× larger but `CLUSTER_THRESHOLD=10` already prevents giant days. If smoke shows perceptible delay on mobile data, fall back to fetching only `monthStart..monthEnd` and dim out-of-month strip days as `count = 0`. This is a one-line change in the fetch effect — note in PR but DO NOT pre-implement.
6. **`useViewport()` is read by `CalendarRoute` (the parent of both pages). At <900px width, `MobileCalendarPage` mounts; ≥900 mounts `DesktopCalendarPage`.** Both component test files import the page directly, so they bypass viewport detection entirely. Manual smoke is the only place where the switch is exercised.
7. **Fake timers + new tests.** `MobileCalendarPage.test.tsx` reuses ROB-165's `vi.useFakeTimers({ toFake: ["Date"] })` + `vi.setSystemTime("2026-05-11T12:00:00+09:00")` pattern. Confirms our `fmtLocal` reads local Date methods (not UTC). If the CI runner is in UTC, today's `getDate()` may differ by a day from KST — but the existing ROB-165 desktop tests already proved this pattern works on the same CI.
8. **`calendar.css` `@-webkit-` prefixes.** `-webkit-line-clamp` is needed for Safari/iOS. Modern Chromium supports it unprefixed too, but the prefixed form is required for our user base. No build-time autoprefixer in this Vite config — leave the prefix in source.

## Self-review checklist

- ✅ Spec coverage: every Linear acceptance criterion is mapped to a task above.
- ✅ No placeholders: every test file has full code; every component file is included verbatim; every command has expected output.
- ✅ Type consistency: `MonthGridDensity`, `MonthCalendarGridProps`, `SelectedDateEventsProps` (now with `loading?`/`error?`), `CalendarMonthHeaderProps` are all referenced consistently across tasks.
- ✅ TDD: every behavior change has a failing test step before the implementation step.
- ✅ Frequent commits: 7 commits across the implementation tasks (Task 1 + 2 + 3 + 4 + 5a/5b + 6 = 6 functional commits + Task 7 push). Each commit leaves the suite green.
- ✅ Worktree-only: every `cd` and `git` command targets `/Users/mgh3326/worktrees/auto_trader/ROB-166-calendar-responsive`. No edit lands in `/Users/mgh3326/work/auto_trader` or `/Users/mgh3326/services/auto_trader/current`.
- ✅ Safety boundaries: zero broker / order / watch / order-intent / paper / live / scheduler / DB-mutation / ingestion changes. Pure frontend (vm + components + CSS + page wires).
