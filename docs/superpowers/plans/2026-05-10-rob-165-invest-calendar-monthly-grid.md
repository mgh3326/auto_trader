# ROB-165 — `/invest/calendar` Toss-style Monthly Grid Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Linear:** [ROB-165](https://linear.app/mgh3326/issue/ROB-165) — `auto_trader: /invest/calendar Toss식 월간 캘린더 grid 전환`
**Worktree:** `/Users/mgh3326/worktrees/auto_trader/ROB-165-calendar-grid`
**Branch:** `kanban/ROB-165-calendar-grid` (already created — do NOT push to `main`/`production`)
**Base PR target:** `main`
**Predecessor:** ROB-158 (weekly cluster fix, merged in #739)
**Successor / blocked by us:** ROB-166 (mobile/responsive polish — out of scope here)

**Goal:** Replace the week-centered desktop view at `/invest/calendar` with a Toss-style **monthly grid** (6 rows × 7 cols), fetching a full month range, and showing the selected date's events/clusters in a side list. Mobile route is unchanged (ROB-166 owns mobile UX).

**Architecture:** Frontend-only change. No backend/router/DB changes. The existing `/invest/api/calendar?from_date=&to_date=&tab=` already accepts arbitrary ranges — request the full month grid (6 weeks aligned to Sunday) and reuse the existing `CalendarResponse`. Add a presentational `MonthCalendarGrid` plus a `SelectedDateEvents` list. Keep `WeekDateStrip` and `computeWeekLabel` untouched so `MobileCalendarPage` keeps working.

**Tech stack:** React 18 + TypeScript + Vite + Vitest + React Testing Library; existing Card/Icon design-system primitives in `frontend/invest/src/ds`.

---

## Scope & Non-goals

In scope:
- Month-aligned state (`monthCursor`), 6×7 grid, selected-date highlight, today highlight.
- Per-cell event count + dim out-of-month cells.
- Selected-date event/cluster list reusing `EventRow` / `ClusterRow`.
- Filter pills (전체/경제지표/실적, 전체/국내/해외) keep working — they affect both the grid count and the selected-date list.
- AI weekly card pinned to the week containing the selected date (so the existing `/invest/api/calendar/weekly-summary?week_start=` contract is reused without backend changes).
- New labels: `2026년 5월`, `5월 금융 캘린더`, `5월 13일 수요일 일정`.
- Frontend tests (vitest) + typecheck + build.

Out of scope (do NOT touch):
- `MobileCalendarPage.tsx` — ROB-166 covers mobile UX. The current `WeekDateStrip` and `computeWeekLabel` MUST remain importable and unchanged.
- Backend service/router/schema changes. Add at most an *additive* test that exercises a 31-day range; do not modify `app/services/invest_view_model/calendar_service.py` or `app/routers/invest_api.py`.
- Broker/order/watch/order-intent/scheduler/DB-mutation work (Linear safety boundary).

---

## Files

**Create**
- `frontend/invest/src/components/calendar/MonthCalendarGrid.tsx` — purely presentational 6×7 month grid.
- `frontend/invest/src/components/calendar/SelectedDateEvents.tsx` — selected-date list (events + clusters) wrapper around `EventRow`/`ClusterRow`.
- `frontend/invest/src/__tests__/calendarMonthVm.test.ts` — unit tests for new vm helpers.
- `frontend/invest/src/__tests__/MonthCalendarGrid.test.tsx` — component tests for the grid.

**Modify**
- `frontend/invest/src/components/calendar/vm.ts` — add month/grid helpers and labels (additive — keep `computeWeekLabel`, `dayOfWeekLabel`, `shortDateLabel` intact for mobile).
- `frontend/invest/src/pages/desktop/DesktopCalendarPage.tsx` — replace week state/UI with month state + `MonthCalendarGrid` + `SelectedDateEvents`.
- `frontend/invest/src/__tests__/DesktopCalendarPage.test.tsx` — rewrite assertions for month-range fetch, grid render, selected-date list, filter behavior on selected day, AI weekly summary still wired to the week of the selected date.

**Optional (Task 7)**
- `tests/test_invest_calendar_router.py` — add `test_get_calendar_month_range` exercising a 31-day span (locks in compatibility).

**Untouched but read for context**
- `frontend/invest/src/types/calendar.ts` — `CalendarDay`/`CalendarEvent`/`CalendarCluster` already cover what we need.
- `frontend/invest/src/api/calendar.ts` — `fetchCalendar({fromDate,toDate,tab})` is reused unchanged.
- `frontend/invest/src/components/calendar/{EventRow,ClusterRow,EmptyEventState,EventDetailModal,AIWeeklyCard,RegionBadge,OwnershipTag}.tsx` — reused unchanged.
- `frontend/invest/src/pages/mobile/MobileCalendarPage.tsx` — DO NOT MODIFY.

---

## Design notes (lock these in)

1. **Week start = Sunday** for the grid (matches Toss's KR mainstream and the existing `dayOfWeekLabel` index where 0=일).
2. **Always render a 6×7 grid (42 cells)**, padded with leading days from the previous month and trailing days from the next month — this keeps layout height stable across months.
3. **Fetch range = grid range** (`gridStart` … `gridEnd`, 42 days). This way leading/trailing cells show real event counts; if we fetched only `monthStart..monthEnd` those cells would always look empty.
4. **`selectedDate` default** = today if today is within the visible grid, otherwise `monthStart` (first of the visible month). When the user navigates months, `selectedDate` should jump to today (if in range) else first-of-month.
5. **Filters affect both the grid badge and the selected-date list.** Compute a single `filteredByDate: Map<string, { events: CalendarEventVM[]; clusters: CalendarClusterVM[]; total: number }>` in `DesktopCalendarPage` and pass it to both children.
6. **AI weekly card** stays pinned to the week containing `selectedDate` (Mon-aligned, matching `/invest/api/calendar/weekly-summary` `week_start` param semantics).
7. **Local YYYY-MM-DD formatting** using `fmt(d)` (the existing helper), NEVER `toISOString().slice(0,10)` (UTC drift).
8. **Test ids**: keep stable, prefix-match patterns existing tests already query:
   - `month-grid` (root), `month-grid-cell-${date}`, `month-grid-cell-out-of-month-${date}` (variant attr `data-out-of-month="true"`).
   - `calendar-prev-month`, `calendar-next-month` (replaces `calendar-prev-week` / `calendar-next-week`).
   - `selected-date-events` (root) — keep `data-testid="day-events"` ALSO on the same node so cross-cutting tests that only look at "day-events" don't regress.
   - `calendar-event` and `calendar-cluster` test ids on rows are already set by `EventRow`/`ClusterRow`; do not change.

---

## Task 1 — vm.ts month/grid helpers (TDD)

**Files:**
- Modify: `frontend/invest/src/components/calendar/vm.ts` (append-only — keep existing exports)
- Create: `frontend/invest/src/__tests__/calendarMonthVm.test.ts`

**Helpers to add to `vm.ts`:**

```ts
// --- ROB-165 month/grid helpers (Sunday-first 6x7 grid) ---

export function fmtLocal(d: Date): string {
  const y = d.getFullYear();
  const m = String(d.getMonth() + 1).padStart(2, "0");
  const day = String(d.getDate()).padStart(2, "0");
  return `${y}-${m}-${day}`;
}

export function startOfMonth(d: Date): Date {
  const out = new Date(d);
  out.setDate(1);
  out.setHours(0, 0, 0, 0);
  return out;
}

export function endOfMonth(d: Date): Date {
  const out = new Date(d);
  out.setMonth(out.getMonth() + 1);
  out.setDate(0); // last day of previous (i.e., target) month
  out.setHours(0, 0, 0, 0);
  return out;
}

export function addMonths(d: Date, n: number): Date {
  const out = new Date(d);
  out.setDate(1); // avoid month-end overflow (e.g. Jan 31 + 1m -> Mar 3)
  out.setMonth(out.getMonth() + n);
  return out;
}

// Sunday-aligned start of the 6-week grid containing `monthFirst`.
export function gridStartFromMonth(monthFirst: Date): Date {
  const start = startOfMonth(monthFirst);
  const dow = start.getDay(); // 0=Sun
  start.setDate(start.getDate() - dow);
  start.setHours(0, 0, 0, 0);
  return start;
}

// Always 41 days after gridStart (6 weeks - 1).
export function gridEndFromMonth(monthFirst: Date): Date {
  const start = gridStartFromMonth(monthFirst);
  const end = new Date(start);
  end.setDate(end.getDate() + 41);
  return end;
}

// Mon-aligned start of the week containing `date` (matches backend weekly-summary semantics).
export function weekStartOf(dateIso: string): string {
  const d = new Date(`${dateIso}T00:00:00`);
  const offset = (d.getDay() + 6) % 7; // Mon=0
  d.setDate(d.getDate() - offset);
  return fmtLocal(d);
}

export function monthLabel(monthFirstIso: string): string {
  const [, m] = monthFirstIso.split("-");
  return `${Number.parseInt(m ?? "0", 10)}월 금융 캘린더`;
}

export function monthTitleLabel(monthFirstIso: string): string {
  const [y, m] = monthFirstIso.split("-");
  return `${y}년 ${Number.parseInt(m ?? "0", 10)}월`;
}

export function selectedDateLabel(dateIso: string): string {
  const [, m, d] = dateIso.split("-");
  const dow = dayOfWeekLabel(dateIso);
  return `${Number.parseInt(m ?? "0", 10)}월 ${Number.parseInt(d ?? "0", 10)}일 ${dow}요일 일정`;
}
```

- [ ] **Step 1.1: Write the failing tests**

Create `frontend/invest/src/__tests__/calendarMonthVm.test.ts`:

```ts
import { describe, expect, test } from "vitest";
import {
  addMonths,
  endOfMonth,
  fmtLocal,
  gridEndFromMonth,
  gridStartFromMonth,
  monthLabel,
  monthTitleLabel,
  selectedDateLabel,
  startOfMonth,
  weekStartOf,
} from "../components/calendar/vm";

describe("ROB-165 month/grid helpers", () => {
  test("startOfMonth returns 1st of month at local midnight", () => {
    expect(fmtLocal(startOfMonth(new Date(2026, 4, 13, 11)))).toBe("2026-05-01");
    expect(fmtLocal(startOfMonth(new Date(2026, 1, 28)))).toBe("2026-02-01");
  });

  test("endOfMonth handles 28/30/31-day months and leap year", () => {
    expect(fmtLocal(endOfMonth(new Date(2026, 4, 13)))).toBe("2026-05-31");
    expect(fmtLocal(endOfMonth(new Date(2026, 3, 1)))).toBe("2026-04-30");
    expect(fmtLocal(endOfMonth(new Date(2026, 1, 1)))).toBe("2026-02-28");
    expect(fmtLocal(endOfMonth(new Date(2024, 1, 1)))).toBe("2024-02-29");
  });

  test("addMonths avoids end-of-month overflow", () => {
    // Jan 31 + 1m must give Feb 1, not Mar 3.
    expect(fmtLocal(addMonths(new Date(2026, 0, 31), 1))).toBe("2026-02-01");
    expect(fmtLocal(addMonths(new Date(2026, 4, 15), -1))).toBe("2026-04-01");
    expect(fmtLocal(addMonths(new Date(2026, 4, 15), 12))).toBe("2027-05-01");
  });

  test("gridStartFromMonth aligns to the Sunday on/before the 1st", () => {
    // 2026-05-01 is Friday -> grid starts Sun 2026-04-26.
    expect(fmtLocal(gridStartFromMonth(new Date(2026, 4, 1)))).toBe("2026-04-26");
    // 2026-03-01 is Sunday -> grid starts that day.
    expect(fmtLocal(gridStartFromMonth(new Date(2026, 2, 1)))).toBe("2026-03-01");
  });

  test("gridEndFromMonth is gridStart + 41 days (6 weeks)", () => {
    expect(fmtLocal(gridEndFromMonth(new Date(2026, 4, 1)))).toBe("2026-06-06");
    expect(fmtLocal(gridEndFromMonth(new Date(2026, 2, 1)))).toBe("2026-04-11");
  });

  test("weekStartOf returns Monday-aligned date string", () => {
    expect(weekStartOf("2026-05-13")).toBe("2026-05-11"); // Wed -> Mon
    expect(weekStartOf("2026-05-11")).toBe("2026-05-11"); // Mon stays
    expect(weekStartOf("2026-05-10")).toBe("2026-05-04"); // Sun -> previous Mon
  });

  test("monthTitleLabel and monthLabel produce Korean labels", () => {
    expect(monthTitleLabel("2026-05-01")).toBe("2026년 5월");
    expect(monthLabel("2026-05-01")).toBe("5월 금융 캘린더");
  });

  test("selectedDateLabel produces Korean weekday label", () => {
    // 2026-05-13 is Wednesday.
    expect(selectedDateLabel("2026-05-13")).toBe("5월 13일 수요일 일정");
  });
});
```

- [ ] **Step 1.2: Run the tests, confirm all FAIL**

Run: `cd frontend/invest && npx vitest run src/__tests__/calendarMonthVm.test.ts`
Expected: each test fails with "is not a function" (helpers not exported yet).

- [ ] **Step 1.3: Add the helpers in `vm.ts`**

Append the entire helper block from the "Helpers to add" section above to `frontend/invest/src/components/calendar/vm.ts`. **Do NOT modify or remove** existing exports (`computeWeekLabel`, `dayOfWeekLabel`, `shortDateLabel`, `toEventVM`, `toClusterVM`, `mapEventType`, `mapMarketToRegion`, `mapOwnership`, `calendarDayEventCount`, `formatClusterTitle`, type aliases).

- [ ] **Step 1.4: Run the tests again, confirm all PASS**

Run: `cd frontend/invest && npx vitest run src/__tests__/calendarMonthVm.test.ts`
Expected: 7 tests passing.

- [ ] **Step 1.5: Commit**

```bash
git add frontend/invest/src/components/calendar/vm.ts \
        frontend/invest/src/__tests__/calendarMonthVm.test.ts
git commit -m "feat(invest-calendar): add ROB-165 month/grid vm helpers"
```

---

## Task 2 — `MonthCalendarGrid` component (TDD)

**Files:**
- Create: `frontend/invest/src/components/calendar/MonthCalendarGrid.tsx`
- Create: `frontend/invest/src/__tests__/MonthCalendarGrid.test.tsx`

**Component contract:**

```ts
export interface MonthCalendarGridProps {
  monthCursor: Date;             // any Date inside the month being shown
  selectedDate: string;          // YYYY-MM-DD
  today: string;                 // YYYY-MM-DD
  // Pre-computed map of dateIso -> total filtered count (events + summed cluster eventCount).
  // Out-of-month dates may also appear in the map; the component does not filter them.
  countByDate: Map<string, number>;
  onSelect: (date: string) => void;
}
```

The grid renders a 7-column header row of `일 월 화 수 목 금 토` then six rows of seven cells (42 total). For each cell we compute its `dateIso` from `gridStartFromMonth(monthCursor)` + offset. A cell renders:
- the day number (`dateIso.slice(8,10)` parsed),
- a small count badge if `countByDate.get(dateIso) > 0`,
- visual states: `data-today`, `data-selected`, `data-out-of-month` boolean attributes.

- [ ] **Step 2.1: Write the failing test**

Create `frontend/invest/src/__tests__/MonthCalendarGrid.test.tsx`:

```tsx
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, test, vi } from "vitest";
import { MonthCalendarGrid } from "../components/calendar/MonthCalendarGrid";

const baseProps = {
  monthCursor: new Date(2026, 4, 1), // May 2026
  selectedDate: "2026-05-13",
  today: "2026-05-11",
  countByDate: new Map<string, number>([
    ["2026-05-11", 3],
    ["2026-05-13", 327],
  ]),
};

describe("MonthCalendarGrid", () => {
  test("renders 42 day cells aligned Sunday-first starting 2026-04-26", () => {
    render(<MonthCalendarGrid {...baseProps} onSelect={() => {}} />);
    const cells = screen.getAllByTestId(/^month-grid-cell-/);
    expect(cells).toHaveLength(42);
    expect(cells[0]).toHaveAttribute("data-date", "2026-04-26");
    expect(cells[41]).toHaveAttribute("data-date", "2026-06-06");
  });

  test("flags out-of-month, today, and selected cells", () => {
    render(<MonthCalendarGrid {...baseProps} onSelect={() => {}} />);
    expect(screen.getByTestId("month-grid-cell-2026-04-26")).toHaveAttribute("data-out-of-month", "true");
    expect(screen.getByTestId("month-grid-cell-2026-05-01")).toHaveAttribute("data-out-of-month", "false");
    expect(screen.getByTestId("month-grid-cell-2026-05-11")).toHaveAttribute("data-today", "true");
    expect(screen.getByTestId("month-grid-cell-2026-05-13")).toHaveAttribute("data-selected", "true");
  });

  test("renders count badge from countByDate", () => {
    render(<MonthCalendarGrid {...baseProps} onSelect={() => {}} />);
    const cell = screen.getByTestId("month-grid-cell-2026-05-13");
    expect(cell).toHaveTextContent("13");
    expect(cell).toHaveTextContent("327");
  });

  test("clicking a cell calls onSelect with that ISO date", async () => {
    const user = userEvent.setup();
    const onSelect = vi.fn();
    render(<MonthCalendarGrid {...baseProps} onSelect={onSelect} />);
    await user.click(screen.getByTestId("month-grid-cell-2026-05-20"));
    expect(onSelect).toHaveBeenCalledWith("2026-05-20");
  });

  test("renders Korean weekday header row Sun-first", () => {
    render(<MonthCalendarGrid {...baseProps} onSelect={() => {}} />);
    const header = screen.getByTestId("month-grid-weekday-header");
    expect(header.textContent).toBe("일월화수목금토");
  });
});
```

- [ ] **Step 2.2: Run, confirm test fails**

Run: `cd frontend/invest && npx vitest run src/__tests__/MonthCalendarGrid.test.tsx`
Expected: failure — module not found.

- [ ] **Step 2.3: Implement `MonthCalendarGrid.tsx`**

Create `frontend/invest/src/components/calendar/MonthCalendarGrid.tsx`:

```tsx
import { fmtLocal, gridStartFromMonth, startOfMonth } from "./vm";

const WEEKDAY_LABELS = ["일", "월", "화", "수", "목", "금", "토"] as const;

export interface MonthCalendarGridProps {
  monthCursor: Date;
  selectedDate: string;
  today: string;
  countByDate: Map<string, number>;
  onSelect: (date: string) => void;
}

export function MonthCalendarGrid({
  monthCursor,
  selectedDate,
  today,
  countByDate,
  onSelect,
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
    <div data-testid="month-grid" style={{ display: "flex", flexDirection: "column", gap: 4 }}>
      <div
        data-testid="month-grid-weekday-header"
        style={{
          display: "grid",
          gridTemplateColumns: "repeat(7, 1fr)",
          fontSize: 11,
          fontWeight: 600,
          color: "var(--fg-3)",
          textAlign: "center",
          padding: "0 2px",
        }}
      >
        {WEEKDAY_LABELS.map((w) => (
          <span key={w}>{w}</span>
        ))}
      </div>
      <div
        style={{
          display: "grid",
          gridTemplateColumns: "repeat(7, 1fr)",
          gap: 4,
        }}
      >
        {cells.map((c) => {
          const isToday = c.iso === today;
          const isSelected = c.iso === selectedDate;
          const count = countByDate.get(c.iso) ?? 0;
          return (
            <button
              key={c.iso}
              type="button"
              data-testid={`month-grid-cell-${c.iso}`}
              data-date={c.iso}
              data-today={isToday ? "true" : "false"}
              data-selected={isSelected ? "true" : "false"}
              data-out-of-month={c.outOfMonth ? "true" : "false"}
              onClick={() => onSelect(c.iso)}
              style={{
                aspectRatio: "1 / 1",
                minHeight: 56,
                display: "flex",
                flexDirection: "column",
                alignItems: "center",
                justifyContent: "flex-start",
                gap: 2,
                padding: "8px 4px",
                border: "none",
                borderRadius: 10,
                cursor: "pointer",
                fontFamily: "inherit",
                background: isSelected ? "var(--surface-2)" : "transparent",
                opacity: c.outOfMonth ? 0.35 : 1,
              }}
            >
              <span
                style={{
                  width: 26,
                  height: 26,
                  borderRadius: 999,
                  display: "grid",
                  placeItems: "center",
                  background: isSelected ? "var(--accent)" : "transparent",
                  color: isSelected ? "var(--fg-on-accent)" : isToday ? "var(--accent)" : "var(--fg-1)",
                  fontWeight: isSelected || isToday ? 700 : 500,
                  fontSize: 13,
                  fontFeatureSettings: '"tnum"',
                }}
              >
                {c.day}
              </span>
              {count > 0 && (
                <span style={{ fontSize: 10, fontWeight: 600, color: "var(--fg-3)" }}>{count}</span>
              )}
            </button>
          );
        })}
      </div>
    </div>
  );
}
```

- [ ] **Step 2.4: Run tests, confirm all PASS**

Run: `cd frontend/invest && npx vitest run src/__tests__/MonthCalendarGrid.test.tsx`
Expected: 5 tests passing.

- [ ] **Step 2.5: Commit**

```bash
git add frontend/invest/src/components/calendar/MonthCalendarGrid.tsx \
        frontend/invest/src/__tests__/MonthCalendarGrid.test.tsx
git commit -m "feat(invest-calendar): add MonthCalendarGrid 6x7 grid component"
```

---

## Task 3 — `SelectedDateEvents` component

**Files:**
- Create: `frontend/invest/src/components/calendar/SelectedDateEvents.tsx`

This is a thin wrapper around existing `EventRow` and `ClusterRow` so the desktop page stays small. Tests for this component are exercised indirectly via the `DesktopCalendarPage` tests in Task 5 (no separate test file — the surface area is too small to justify duplicate fixtures).

- [ ] **Step 3.1: Implement the component**

Create `frontend/invest/src/components/calendar/SelectedDateEvents.tsx`:

```tsx
import { ClusterRow } from "./ClusterRow";
import { EventRow } from "./EventRow";
import { EmptyEventState } from "./EmptyEventState";
import type { CalendarClusterVM, CalendarEventVM } from "./vm";

export interface SelectedDateEventsProps {
  dateLabel: string;        // e.g. "5월 13일 수요일 일정"
  dateIso: string;          // e.g. "2026-05-13"
  events: CalendarEventVM[];
  clusters: CalendarClusterVM[];
  emptyMessage: string;     // e.g. "선택한 날짜에 일정이 없습니다."
}

export function SelectedDateEvents({
  dateLabel,
  dateIso,
  events,
  clusters,
  emptyMessage,
}: SelectedDateEventsProps) {
  const total = events.length + clusters.reduce((s, c) => s + c.count, 0);
  return (
    <div
      data-testid="selected-date-events"
      data-selected-date={dateIso}
    >
      <div
        style={{
          display: "flex",
          alignItems: "baseline",
          gap: 8,
          padding: "0 6px 8px",
        }}
      >
        <h2 style={{ margin: 0, fontSize: 15, fontWeight: 800, color: "var(--fg)" }}>
          {dateLabel}
        </h2>
        <span style={{ fontSize: 12, color: "var(--fg-3)", fontFeatureSettings: '"tnum"' }}>
          {dateIso} · {total}건
        </span>
      </div>
      {/* Keep `day-events` test id for cross-cutting tests that rely on it. */}
      <div data-testid="day-events" style={{ display: "flex", flexDirection: "column", gap: 4 }}>
        {events.length === 0 && clusters.length === 0 ? (
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
```

- [ ] **Step 3.2: Quick sanity check — run the existing test suite to confirm nothing imports from the new file yet (it shouldn't error)**

Run: `cd frontend/invest && npx vitest run`
Expected: pre-existing tests still pass; no import errors. (DesktopCalendarPage hasn't been wired yet — that happens in Task 4.)

- [ ] **Step 3.3: Commit**

```bash
git add frontend/invest/src/components/calendar/SelectedDateEvents.tsx
git commit -m "feat(invest-calendar): add SelectedDateEvents list component"
```

---

## Task 4 — Wire `DesktopCalendarPage` to month state and new components (TDD)

**Files:**
- Modify: `frontend/invest/src/pages/desktop/DesktopCalendarPage.tsx` (full rewrite)
- Modify: `frontend/invest/src/__tests__/DesktopCalendarPage.test.tsx`

The new page replaces the week strip + day-grouped section list with the month grid + selected-date list. Filters and the AI weekly card stay, with the AI card pinned to the week containing the selected date.

- [ ] **Step 4.1: Write the new failing tests (replacing the file's content)**

Replace `frontend/invest/src/__tests__/DesktopCalendarPage.test.tsx` with:

```tsx
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { vi, beforeEach, afterEach, test, expect } from "vitest";
import { MemoryRouter } from "react-router-dom";
import { DesktopCalendarPage } from "../pages/desktop/DesktopCalendarPage";
import { AccountPanelProvider } from "../desktop/AccountPanelProvider";
import * as calApi from "../api/calendar";
import * as panelApi from "../api/accountPanel";
import * as signalsApi from "../api/signals";
import type { CalendarEvent, CalendarResponse } from "../types/calendar";

function wrap(ui: React.ReactElement) {
  return (
    <AccountPanelProvider>
      <MemoryRouter basename="/invest" initialEntries={["/invest/calendar"]}>
        {ui}
      </MemoryRouter>
    </AccountPanelProvider>
  );
}

function event(
  overrides: Partial<CalendarEvent> & Pick<CalendarEvent, "eventId" | "title" | "market" | "eventType">,
): CalendarEvent {
  return {
    source: "fixture",
    relatedSymbols: [],
    relation: "none",
    badges: [],
    ...overrides,
  };
}

// Returned for any month-range fetch in this test file. We reuse the same days fixture to keep tests focused.
const calendarFixture: CalendarResponse = {
  tab: "all",
  fromDate: "2026-04-26",
  toDate: "2026-06-06",
  asOf: "2026-05-11T03:00:00.000Z",
  days: [
    {
      date: "2026-05-11",
      events: [
        event({
          eventId: "evt-aapl-direct",
          title: "AAPL earnings direct",
          market: "us",
          eventType: "earnings",
          eventTimeLocal: "오후 9시 발표 예정",
        }),
      ],
      clusters: [],
    },
    {
      date: "2026-05-13",
      events: [],
      clusters: [
        {
          clusterId: "cluster-us-earnings-2026-05-13",
          label: "US earnings",
          eventType: "earnings",
          market: "us",
          eventCount: 327,
          topEvents: [
            event({ eventId: "evt-aapl-top", title: "AAPL earnings", market: "us", eventType: "earnings" }),
            event({ eventId: "evt-msft-top", title: "MSFT earnings", market: "us", eventType: "earnings" }),
          ],
        },
      ],
    },
    {
      date: "2026-05-15",
      events: [],
      clusters: [
        {
          clusterId: "cluster-global-macro-2026-05-15",
          label: "Global macro",
          eventType: "economic",
          market: "global",
          eventCount: 4,
          topEvents: [event({ eventId: "evt-cpi", title: "US CPI", market: "us", eventType: "economic" })],
        },
      ],
    },
  ],
  meta: { warnings: [] },
};

beforeEach(() => {
  vi.useFakeTimers({ toFake: ["Date"] });
  vi.setSystemTime(new Date("2026-05-11T12:00:00+09:00"));
  vi.spyOn(panelApi, "fetchAccountPanel").mockResolvedValue({
    homeSummary: { includedSources: [], excludedSources: [], totalValueKrw: 0 },
    accounts: [],
    groupedHoldings: [],
    watchSymbols: [],
    sourceVisuals: [],
    meta: { warnings: [], watchlistAvailable: true },
  });
  vi.spyOn(signalsApi, "fetchSignals").mockResolvedValue({
    tab: "kr",
    asOf: new Date().toISOString(),
    items: [],
    meta: { warnings: [] },
  });
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

test("fetches the full month grid range (Sun-aligned 6 weeks) on mount", async () => {
  render(wrap(<DesktopCalendarPage />));
  await waitFor(() => {
    expect(calApi.fetchCalendar).toHaveBeenCalledWith({
      fromDate: "2026-04-26",
      toDate: "2026-06-06",
      tab: "all",
    });
  });
  expect(screen.getAllByTestId(/^month-grid-cell-/)).toHaveLength(42);
});

test("today highlight and default selected date is today (in-range)", async () => {
  render(wrap(<DesktopCalendarPage />));
  const today = await screen.findByTestId("month-grid-cell-2026-05-11");
  expect(today).toHaveAttribute("data-today", "true");
  expect(today).toHaveAttribute("data-selected", "true");
});

test("month grid shows count derived from clusters and events", async () => {
  render(wrap(<DesktopCalendarPage />));
  const clusterCell = await screen.findByTestId("month-grid-cell-2026-05-13");
  expect(within(clusterCell).getByText("327")).toBeInTheDocument();
});

test("clicking a date updates the selected-date list", async () => {
  const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
  render(wrap(<DesktopCalendarPage />));
  await screen.findByTestId("selected-date-events");

  await user.click(screen.getByTestId("month-grid-cell-2026-05-13"));

  await waitFor(() =>
    expect(screen.getByTestId("selected-date-events")).toHaveAttribute("data-selected-date", "2026-05-13"),
  );
  expect(screen.getByText("미국 실적 발표 327건")).toBeInTheDocument();
  expect(screen.getByText(/5월 13일 수요일 일정/)).toBeInTheDocument();
});

test("empty selected date renders graceful empty state", async () => {
  const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
  render(wrap(<DesktopCalendarPage />));
  await screen.findByTestId("selected-date-events");

  await user.click(screen.getByTestId("month-grid-cell-2026-05-12"));

  expect(await screen.findByTestId("calendar-empty")).toHaveTextContent(
    "선택한 날짜에 일정이 없습니다.",
  );
});

test("prev/next month navigation refetches with the new month range", async () => {
  const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
  render(wrap(<DesktopCalendarPage />));
  await waitFor(() => expect(calApi.fetchCalendar).toHaveBeenCalledTimes(1));

  await user.click(screen.getByTestId("calendar-next-month"));
  await waitFor(() =>
    expect(calApi.fetchCalendar).toHaveBeenLastCalledWith({
      // June 2026 grid: starts Sun 2026-05-31, ends Sat 2026-07-11
      fromDate: "2026-05-31",
      toDate: "2026-07-11",
      tab: "all",
    }),
  );

  await user.click(screen.getByTestId("calendar-prev-month"));
  await user.click(screen.getByTestId("calendar-prev-month"));
  await waitFor(() =>
    expect(calApi.fetchCalendar).toHaveBeenLastCalledWith({
      // April 2026 grid: starts Sun 2026-03-29, ends Sat 2026-05-09
      fromDate: "2026-03-29",
      toDate: "2026-05-09",
      tab: "all",
    }),
  );
});

test("month title label shows '2026년 5월' and section header '5월 금융 캘린더'", async () => {
  render(wrap(<DesktopCalendarPage />));
  expect(await screen.findByText("2026년 5월")).toBeInTheDocument();
  expect(screen.getByText("5월 금융 캘린더")).toBeInTheDocument();
});

test("AI weekly card refetches when selecting a date in a different week", async () => {
  const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
  render(wrap(<DesktopCalendarPage />));
  // Initial selected date = today 2026-05-11 (Mon) -> week_start = 2026-05-11
  await user.click(screen.getByTestId("open-weekly-summary"));
  await waitFor(() =>
    expect(calApi.fetchWeeklySummary).toHaveBeenLastCalledWith("2026-05-11"),
  );

  // Select 2026-05-20 (Wed of next week) -> week_start should be 2026-05-18
  await user.click(screen.getByTestId("month-grid-cell-2026-05-20"));
  await waitFor(() =>
    expect(calApi.fetchWeeklySummary).toHaveBeenLastCalledWith("2026-05-18"),
  );
});

test("type and region filters apply to the selected-date list and grid count", async () => {
  const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
  render(wrap(<DesktopCalendarPage />));
  await screen.findByTestId("selected-date-events");

  // Select May 13 — has 327 US-earnings cluster. Filter to 경제지표 — list/cluster disappears.
  await user.click(screen.getByTestId("month-grid-cell-2026-05-13"));
  expect(screen.getByText("미국 실적 발표 327건")).toBeInTheDocument();

  await user.click(screen.getByRole("button", { name: "경제지표" }));
  expect(screen.queryByText("미국 실적 발표 327건")).not.toBeInTheDocument();
  expect(screen.getByTestId("calendar-empty")).toBeInTheDocument();
  // Grid count badge for May 13 should be gone now (no macro events that day).
  const may13 = screen.getByTestId("month-grid-cell-2026-05-13");
  expect(within(may13).queryByText("327")).not.toBeInTheDocument();

  // Switch to 실적 — cluster reappears for May 13.
  await user.click(screen.getByRole("button", { name: "실적" }));
  expect(screen.getByText("미국 실적 발표 327건")).toBeInTheDocument();

  // 국내 region filter — empty (cluster is US).
  await user.click(screen.getByRole("button", { name: "국내" }));
  expect(screen.queryByText("미국 실적 발표 327건")).not.toBeInTheDocument();
  expect(screen.getByTestId("calendar-empty")).toBeInTheDocument();
});
```

- [ ] **Step 4.2: Run the new tests, confirm they fail**

Run: `cd frontend/invest && npx vitest run src/__tests__/DesktopCalendarPage.test.tsx`
Expected: most/all tests fail because `DesktopCalendarPage` still renders the weekly UI.

- [ ] **Step 4.3: Rewrite `DesktopCalendarPage.tsx`**

Overwrite `frontend/invest/src/pages/desktop/DesktopCalendarPage.tsx` with the implementation below. Keep the existing `CalendarRoute` viewport switch so mobile is unaffected.

```tsx
import { useEffect, useMemo, useState } from "react";
import { DesktopShell } from "../../desktop/DesktopShell";
import { RightRemotePanel } from "../../desktop/RightRemotePanel";
import { useViewport } from "../../hooks/useViewport";
import { fetchCalendar, fetchWeeklySummary } from "../../api/calendar";
import type { CalendarResponse, WeeklySummaryResponse } from "../../types/calendar";
import { Card, Icon } from "../../ds";
import { AIWeeklyCard } from "../../components/calendar/AIWeeklyCard";
import { EventDetailModal } from "../../components/calendar/EventDetailModal";
import { MonthCalendarGrid } from "../../components/calendar/MonthCalendarGrid";
import { SelectedDateEvents } from "../../components/calendar/SelectedDateEvents";
import {
  addMonths,
  fmtLocal,
  gridEndFromMonth,
  gridStartFromMonth,
  monthLabel,
  monthTitleLabel,
  selectedDateLabel,
  startOfMonth,
  toClusterVM,
  toEventVM,
  weekStartOf,
  type CalendarClusterVM,
  type CalendarEventVM,
  type DisplayEventType,
  type DisplayRegion,
} from "../../components/calendar/vm";
import { MobileCalendarPage } from "../mobile/MobileCalendarPage";

type TypeFilter = "all" | DisplayEventType;
type RegionFilter = "all" | DisplayRegion;

interface FilteredDay {
  events: CalendarEventVM[];
  clusters: CalendarClusterVM[];
  total: number;
}

export function CalendarRoute() {
  return useViewport() === "mobile" ? <MobileCalendarPage /> : <DesktopCalendarPage />;
}

export function DesktopCalendarPage() {
  const [monthCursor, setMonthCursor] = useState<Date>(() => startOfMonth(new Date()));
  const gridStart = useMemo(() => gridStartFromMonth(monthCursor), [monthCursor]);
  const gridEnd = useMemo(() => gridEndFromMonth(monthCursor), [monthCursor]);

  const today = fmtLocal(new Date());
  const monthFirstIso = fmtLocal(monthCursor);

  const [selectedDate, setSelectedDate] = useState<string>(() => {
    // Default: today if it's inside the visible month, else first of month.
    const cursorMonth = monthCursor.getMonth();
    const now = new Date();
    if (now.getFullYear() === monthCursor.getFullYear() && now.getMonth() === cursorMonth) {
      return fmtLocal(now);
    }
    return monthFirstIso;
  });

  const [calendar, setCalendar] = useState<CalendarResponse | undefined>();
  const [calendarErr, setCalendarErr] = useState<string | undefined>();
  const [summary, setSummary] = useState<WeeklySummaryResponse | undefined>();
  const [summaryErr, setSummaryErr] = useState<string | undefined>();
  const [summaryLoading, setSummaryLoading] = useState(false);
  const [showSummary, setShowSummary] = useState(false);
  const [typeFilter, setTypeFilter] = useState<TypeFilter>("all");
  const [regionFilter, setRegionFilter] = useState<RegionFilter>("all");

  // Fetch full grid range whenever month changes.
  useEffect(() => {
    let cancel = false;
    setCalendar(undefined);
    setCalendarErr(undefined);
    fetchCalendar({ fromDate: fmtLocal(gridStart), toDate: fmtLocal(gridEnd), tab: "all" })
      .then((r) => !cancel && setCalendar(r))
      .catch((e) => !cancel && setCalendarErr(String(e?.message ?? e)));
    return () => {
      cancel = true;
    };
  }, [gridStart, gridEnd]);

  // AI summary is keyed by the Mon-aligned week of the selected date.
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

  // Build filtered-by-date map once per filter/data change.
  const filteredByDate = useMemo<Map<string, FilteredDay>>(() => {
    const map = new Map<string, FilteredDay>();
    for (const d of calendar?.days ?? []) {
      const events = d.events
        .map((event) => toEventVM(event, d.date))
        .filter((event) => matchesFilters(event, typeFilter, regionFilter));
      const clusters = d.clusters
        .map((cluster) => toClusterVM(cluster, d.date))
        .filter((cluster) => matchesFilters(cluster, typeFilter, regionFilter));
      const total = events.length + clusters.reduce((sum, c) => sum + c.count, 0);
      if (total === 0) continue;
      map.set(d.date, { events, clusters, total });
    }
    return map;
  }, [calendar?.days, typeFilter, regionFilter]);

  const countByDate = useMemo<Map<string, number>>(() => {
    const m = new Map<string, number>();
    for (const [iso, day] of filteredByDate) m.set(iso, day.total);
    return m;
  }, [filteredByDate]);

  const selectedDay: FilteredDay = filteredByDate.get(selectedDate) ?? {
    events: [],
    clusters: [],
    total: 0,
  };

  const goPrevMonth = () => {
    setMonthCursor((m) => {
      const next = addMonths(m, -1);
      setSelectedDate(defaultSelectedDateForMonth(next, today));
      return next;
    });
  };
  const goNextMonth = () => {
    setMonthCursor((m) => {
      const next = addMonths(m, 1);
      setSelectedDate(defaultSelectedDateForMonth(next, today));
      return next;
    });
  };

  return (
    <>
      <DesktopShell
        leftColumnWidth={300}
        left={
          <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
            <Card style={{ padding: 16 }}>
              <div
                style={{
                  display: "flex",
                  alignItems: "center",
                  justifyContent: "space-between",
                  marginBottom: 10,
                }}
              >
                <div style={{ fontSize: 14, fontWeight: 700, letterSpacing: "-0.01em" }}>
                  {monthTitleLabel(monthFirstIso)}
                </div>
                <div style={{ display: "flex", gap: 4 }}>
                  <button
                    type="button"
                    aria-label="이전 달"
                    data-testid="calendar-prev-month"
                    onClick={goPrevMonth}
                    style={navBtnStyle}
                  >
                    <Icon name="chev" size={14} />
                  </button>
                  <button
                    type="button"
                    aria-label="다음 달"
                    data-testid="calendar-next-month"
                    onClick={goNextMonth}
                    style={{ ...navBtnStyle, transform: "scaleX(-1)" }}
                  >
                    <Icon name="chev" size={14} />
                  </button>
                </div>
              </div>
              <MonthCalendarGrid
                monthCursor={monthCursor}
                selectedDate={selectedDate}
                today={today}
                countByDate={countByDate}
                onSelect={setSelectedDate}
              />
            </Card>

            <AIWeeklyCard
              summary={summary}
              loading={summaryLoading}
              onOpen={() => setShowSummary(true)}
              compact
            />
          </div>
        }
        center={
          <>
            <header>
              <h1 style={{ margin: 0, fontSize: 22, fontWeight: 800, letterSpacing: "-0.02em" }}>
                캘린더
              </h1>
              <p style={{ margin: "4px 0 0", fontSize: 13, color: "var(--fg-3)" }}>
                이번 달 실적·경제지표·주요 이벤트를 한눈에 확인하세요.
              </p>
            </header>

            <div style={{ display: "flex", gap: 10, alignItems: "center", flexWrap: "wrap" }}>
              <FilterGroup>
                {(
                  [
                    ["all", "전체"],
                    ["macro", "경제지표"],
                    ["earnings", "실적"],
                  ] as const
                ).map(([k, l]) => (
                  <SegPill key={k} on={typeFilter === k} onClick={() => setTypeFilter(k)}>
                    {l}
                  </SegPill>
                ))}
              </FilterGroup>
              <FilterGroup>
                {(
                  [
                    ["all", "전체"],
                    ["kr", "국내"],
                    ["us", "해외"],
                  ] as const
                ).map(([k, l]) => (
                  <SegPill key={k} on={regionFilter === k} onClick={() => setRegionFilter(k)}>
                    {l}
                  </SegPill>
                ))}
              </FilterGroup>
            </div>

            {calendarErr && <div style={{ color: "var(--danger)" }}>오류: {calendarErr}</div>}

            <Card style={{ padding: "16px 6px" }}>
              <div
                style={{
                  display: "flex",
                  alignItems: "baseline",
                  justifyContent: "space-between",
                  padding: "0 14px 10px",
                  borderBottom: "1px solid var(--divider)",
                }}
              >
                <div style={{ fontSize: 14, fontWeight: 800, color: "var(--fg)", letterSpacing: "-0.01em" }}>
                  {monthLabel(monthFirstIso)}
                </div>
              </div>
              <div style={{ padding: "12px 8px 4px" }}>
                <SelectedDateEvents
                  dateLabel={selectedDateLabel(selectedDate)}
                  dateIso={selectedDate}
                  events={selectedDay.events}
                  clusters={selectedDay.clusters}
                  emptyMessage="선택한 날짜에 일정이 없습니다."
                />
              </div>
            </Card>
          </>
        }
        right={<RightRemotePanel />}
      />
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

function matchesFilters(
  item: { type: DisplayEventType; region: DisplayRegion },
  typeFilter: TypeFilter,
  regionFilter: RegionFilter,
): boolean {
  if (typeFilter !== "all" && item.type !== typeFilter) return false;
  if (regionFilter !== "all" && item.region !== regionFilter) return false;
  return true;
}

function defaultSelectedDateForMonth(monthCursor: Date, todayIso: string): string {
  const monthFirst = startOfMonth(monthCursor);
  const todayDate = new Date(`${todayIso}T00:00:00`);
  if (
    todayDate.getFullYear() === monthFirst.getFullYear() &&
    todayDate.getMonth() === monthFirst.getMonth()
  ) {
    return todayIso;
  }
  return fmtLocal(monthFirst);
}

const navBtnStyle: React.CSSProperties = {
  width: 24,
  height: 24,
  border: "none",
  background: "transparent",
  borderRadius: 6,
  cursor: "pointer",
  color: "var(--fg-2)",
  display: "grid",
  placeItems: "center",
};

function FilterGroup({ children }: { children: React.ReactNode }) {
  return (
    <div style={{ display: "inline-flex", padding: 3, background: "var(--surface-2)", borderRadius: 999 }}>
      {children}
    </div>
  );
}

function SegPill({ on, children, onClick }: { on: boolean; children: React.ReactNode; onClick: () => void }) {
  return (
    <button
      type="button"
      onClick={onClick}
      style={{
        padding: "6px 14px",
        border: "none",
        borderRadius: 999,
        cursor: "pointer",
        background: on ? "var(--fg)" : "transparent",
        color: on ? "var(--bg)" : "var(--fg-2)",
        fontWeight: 600,
        fontSize: 13,
        fontFamily: "inherit",
        whiteSpace: "nowrap",
        flexShrink: 0,
      }}
    >
      {children}
    </button>
  );
}
```

- [ ] **Step 4.4: Run the rewritten test file**

Run: `cd frontend/invest && npx vitest run src/__tests__/DesktopCalendarPage.test.tsx`
Expected: 9 tests passing.

- [ ] **Step 4.5: Run the entire frontend test suite to verify no regressions**

Run: `cd frontend/invest && npm test`
Expected: every previously-passing test still passes (mobile calendar, discover calendar card, and all unrelated tests). If any test references `calendar-prev-week`, `calendar-next-week`, or `WeekDateStrip` test ids on the desktop page, audit it — those ids only exist on `MobileCalendarPage` now, which is fine.

- [ ] **Step 4.6: Commit**

```bash
git add frontend/invest/src/pages/desktop/DesktopCalendarPage.tsx \
        frontend/invest/src/__tests__/DesktopCalendarPage.test.tsx
git commit -m "feat(invest-calendar): switch desktop /invest/calendar to monthly grid (ROB-165)"
```

---

## Task 5 — Frontend type-check & build

Verifies the new components/types don't break the production build.

- [ ] **Step 5.1: Run type-check + production build**

Run: `cd frontend/invest && npm run build`
Expected: tsc emits no errors and Vite produces a build under `frontend/invest/dist/`.

- [ ] **Step 5.2: Manual smoke in dev (recommended)**

Run: `cd frontend/invest && npm run dev` (then open `/invest/calendar` against a local backend if available). Confirm:
- The grid renders 6 rows × 7 cols, current month highlighted.
- Today cell shows accent ring; clicking another date moves the selection circle.
- Per-cell event count appears for days with data.
- Prev/next month buttons jump months and refetch.
- Filters change both grid counts and the selected-date list.

If a backend is not available, this step is optional — note "manual smoke skipped — no local backend" in the PR.

- [ ] **Step 5.3: Commit (only if you change anything during smoke; otherwise skip)**

---

## Task 6 — Backend test that locks in 31-day range compatibility (optional but recommended)

The backend already supports any range; we add one assertion-light test so a future refactor of `build_calendar` doesn't accidentally break the monthly UI.

**Files:**
- Modify: `tests/test_invest_calendar_router.py` (append a single new test) — or create a new file `tests/test_invest_calendar_month_range.py` if the existing file structure makes appending awkward.

- [ ] **Step 6.1: Inspect existing fixtures used in `tests/test_invest_calendar_router.py`**

Run: `cd /Users/mgh3326/worktrees/auto_trader/ROB-165-calendar-grid && rg -n "def test_" tests/test_invest_calendar_router.py | head -10`

Reuse whatever client/db fixture pattern the existing tests use (e.g., `async def test_get_calendar(...)`). The new test does NOT need to seed events — it just verifies the response shape is right when the range spans 31+ days.

- [ ] **Step 6.2: Add the test (template — adapt fixture names to match what the file already uses)**

```python
# tests/test_invest_calendar_router.py — APPEND
import pytest
from datetime import date

@pytest.mark.asyncio
async def test_get_calendar_returns_one_day_per_date_for_month_range(
    authed_client,  # adapt to whatever existing fixture name is in use
):
    """ROB-165: month-range request returns N days, one CalendarDay per date."""
    res = await authed_client.get(
        "/invest/api/calendar",
        params={"from_date": "2026-05-01", "to_date": "2026-05-31", "tab": "all"},
    )
    assert res.status_code == 200
    body = res.json()
    assert body["fromDate"] == "2026-05-01"
    assert body["toDate"] == "2026-05-31"
    assert len(body["days"]) == 31
    # Days are date-ascending and unique.
    dates = [d["date"] for d in body["days"]]
    assert dates == sorted(dates)
    assert len(set(dates)) == 31
```

- [ ] **Step 6.3: Run the new test**

Run: `cd /Users/mgh3326/worktrees/auto_trader/ROB-165-calendar-grid && uv run pytest tests/test_invest_calendar_router.py -k month_range -v`
Expected: PASS.

If the fixture name is wrong, fix it (look at the very top of `tests/test_invest_calendar_router.py` for the right name) and re-run. Do **not** modify `app/services/invest_view_model/calendar_service.py` — backend behavior is in scope only as test coverage.

- [ ] **Step 6.4: Commit**

```bash
git add tests/test_invest_calendar_router.py
git commit -m "test(invest-calendar): assert month-range response shape (ROB-165)"
```

---

## Task 7 — Quality gates, PR creation, smoke

- [ ] **Step 7.1: Run repo-wide quality gates (frontend + backend)**

Run (each in turn, fix any issue before moving on):

```bash
# Frontend
cd /Users/mgh3326/worktrees/auto_trader/ROB-165-calendar-grid/frontend/invest
npm test
npm run build

# Backend lint/format/test (only if Task 6 added a backend test, otherwise still run unit tests touching invest_view_model)
cd /Users/mgh3326/worktrees/auto_trader/ROB-165-calendar-grid
make lint
uv run pytest tests/test_invest_calendar_router.py -v
```

Expected: green across the board.

- [ ] **Step 7.2: Final review — re-read `DesktopCalendarPage.tsx`**

Confirm:
- `WeekDateStrip` is no longer imported on the desktop page.
- `MobileCalendarPage.tsx` is unchanged.
- No new exports were removed from `vm.ts`.
- All test ids documented in the "Design notes" section are present.

- [ ] **Step 7.3: Push branch + open PR**

```bash
cd /Users/mgh3326/worktrees/auto_trader/ROB-165-calendar-grid
git push -u origin kanban/ROB-165-calendar-grid
gh pr create --base main --title "feat(invest): ROB-165 monthly calendar grid for /invest/calendar" --body "$(cat <<'EOF'
## Summary
- Convert desktop `/invest/calendar` from week strip to a Toss-style 6×7 monthly grid.
- Fetch `/invest/api/calendar` for the full Sun-aligned 6-week grid range; backend contract unchanged.
- Selected-date event/cluster list reuses existing `EventRow`/`ClusterRow`.
- AI weekly summary is now pinned to the week containing the selected date.
- Mobile route untouched (ROB-166 owns mobile UX).

## Test plan
- [ ] `cd frontend/invest && npm test` (vitest)
- [ ] `cd frontend/invest && npm run build` (typecheck + Vite build)
- [ ] `uv run pytest tests/test_invest_calendar_router.py -v`
- [ ] Manual: open `/invest/calendar` on desktop, verify month grid, prev/next month, filters, AI weekly card on different weeks.
- [ ] Mobile route still works (no regression).

Linear: [ROB-165](https://linear.app/mgh3326/issue/ROB-165)
EOF
)"
```

- [ ] **Step 7.4: Track PR through CI; integrator role merges and deploys**

After CI green and review approval, the integrator squash-merges to `main`, then merges `main` → `production` for one read-only deploy. Read-only smoke = open `/invest/calendar` on production with an authenticated session, verify the month grid renders without console errors. No broker/order/watch side effects involved.

---

## Acceptance checkpoint mapping (Linear → tasks)

| Linear acceptance criterion | Verified in |
|---|---|
| Month grid on desktop | Task 4 (test "fetches the full month grid range") + Task 5 (build) |
| Prev/next changes month | Task 4 (test "prev/next month navigation refetches with the new month range") |
| Frontend requests month range | Task 4 (test "fetches the full month grid range") |
| Click date updates list | Task 4 (test "clicking a date updates the selected-date list") |
| Empty / heavy dates render gracefully | Task 4 (tests "empty selected date" and "month grid shows count derived from clusters and events") |
| KST/date grouping deliberate | Task 1 (`fmtLocal` + `weekStartOf` use local Date methods, not UTC); covered by helper tests |
| Filters preserved | Task 4 (test "type and region filters apply…") |
| Backend compat | Task 6 (test "month-range response shape") |
| Frontend typecheck/tests/build pass | Task 5, Task 7 |
| PR from worktree branch | Task 7 |

## Risk notes

1. **Mobile regression risk**: `MobileCalendarPage.tsx` uses `WeekDateStrip` and `computeWeekLabel`. **Do not delete or rename** either. Verify by running the full vitest suite at the end of Task 4 (Step 4.5) and Task 7.
2. **Local-time pitfall**: `MobileCalendarPage.tsx` uses `d.toISOString().slice(0,10)`, which can drift by a day for non-UTC timezones. Pre-existing latent bug owned by ROB-166 — do not fix here, but do not copy that pattern in new code (use `fmtLocal`).
3. **Grid range = 42 days** could increase backend response payload ~6×. The existing `CLUSTER_THRESHOLD=10` already prevents oversized days; payload growth for a typical month should still be small. If response time degrades noticeably in smoke, consider falling back to fetching only `monthStart..monthEnd` and dimming all out-of-month cells with `count = 0` (one-line change in the fetch effect — note this in the PR but don't pre-implement).
4. **AI weekly summary semantics**: The summary is keyed by Monday-week-start. With the monthly grid, users may select a Sunday and see a summary that crosses month boundaries — this is intentional; the existing endpoint only knows weekly. No backend change.
5. **Test fixture date**: tests pin system time to `2026-05-11T12:00:00+09:00`. If the system fakes Date but does NOT fake timezone, the local timezone of the CI runner matters for `getDay()`. The existing test file already uses this pattern and passes today, so we adopt the same convention.

## Self-review checklist

- ✅ Spec coverage: every Linear acceptance criterion is mapped to a task above.
- ✅ No placeholders: every test has full code; every implementation file is included verbatim; every command has expected output.
- ✅ Type consistency: `MonthCalendarGridProps`, `SelectedDateEventsProps`, `FilteredDay`, helper signatures are referenced consistently across tasks.
- ✅ TDD: every behavior change has a failing test step before the implementation step.
- ✅ Frequent commits: 6 commits across the implementation tasks (1 per task, plus optional backend test commit).
- ✅ Worktree-only: no instruction touches `/Users/mgh3326/work/auto_trader` directly; all `cd` paths point to the worktree.
