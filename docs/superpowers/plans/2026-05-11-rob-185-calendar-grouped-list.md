# ROB-185 — /invest/calendar Grouped Monthly Event Timeline Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the single-selected-date event panel on `/invest/calendar` with a Toss-like, grouped, scrollable monthly event timeline. Clicking a day in the month grid (desktop) or week strip (mobile) highlights and scrolls to that day's section — it does NOT filter the list down to one day. Strip out the dashboard feel: drop the freshness banner from the default surface, drop `+N` count chips, drop em-dash placeholders, drop ISO-formatted meta text.

**Architecture:** Introduce a new `MonthlyEventsTimeline` component (renders one `DaySection` per in-month day for the current `monthCursor`) that replaces `SelectedDateEvents` as the center-pane content on both `DesktopCalendarPage` and `MobileCalendarPage`. Each `DaySection` has a sticky header, an empty-state row when no events match the active filters, and a stable anchor element so that the parent can scroll the section into view when `selectedDate` changes. The existing month-grid / week-strip continues to control `selectedDate`, but the parent pages no longer derive a `selectedDay` map — instead they pass the full filtered-day map plus `selectedDate` straight to the timeline. The current `CalendarFreshnessBanner` unconditional render is removed and replaced with a minimal trailing `CalendarSourceButton` that opens a quiet popover/sheet listing source states in plain Korean — this satisfies the ROB-182 §A1 acceptance ("single trailing affordance") and prevents the page from regressing on stale-source visibility while ROB-186/187 do the broader UX work.

**Tech Stack:** React 18 + TypeScript, existing `frontend/invest/src/components/calendar/` module, vitest + @testing-library/react, project's vanilla CSS module at `frontend/invest/src/styles/calendar.css`. No new dependencies. No backend changes. No DB writes. No broker / order / watch / paper / live trading side effects.

**Acceptance items from ROB-182 spec satisfied by this PR:**
- §A1 (freshness banner removed from default surface; replaced with quiet `데이터 출처` button)
- §A2 (internal state labels — `데이터 상태:`, `오래됨`, `수집 실패`, `미수집`, `일부 수집 중`, `Finnhub 실적`, `DART 공시`, `ForexFactory 경제지표` — removed from the default DOM; `freshnessBadgeLabel()` and the source-name map move inside the popover)
- §A5 (ISO date / `건` meta line replaced with `오늘 일정 N개` / `5월 12일 일정 N개`)
- §A6 (empty-day copy "이 날은 예정된 일정이 없어요"; empty-month copy "이번 달은 예정된 주요 일정이 없어요")
- §B1 (day-pick is scroll target, not filter — single-month flavour rather than 7-day rolling)
- §B4 (em-dash placeholders for null numeric columns removed)
- §C1 partial (raw `+N` cluster count chip removed; cluster title-style label `미국 실적 발표 327건` is retained — full category-pill expansion + Top-3/`더 보기` accordion remain ROB-187 scope)
- §C2 (month-grid cell `+999` overflow replaced with `많음`)
- §D2 (empty-week copy on the timeline)
- §D7 (stale-source signal moves from the centre banner to the source button; full unread dot + retry affordance is ROB-186 scope — this PR ships the button + popover only, with stale rows labelled `방금 업데이트되지 않았어요`)

**Acceptance items NOT in scope (deferred to ROB-186 / ROB-187):**
- §A3, §A4 (mobile time-above-title reorder, source name purge from per-event DOM)
- §B2 sticky header polish across very long scrolls (we apply `position: sticky` per section; cross-section overlap polish stays out)
- §B3 (mobile card-first stacking — numeric columns remain hidden < 900 px exactly as today)
- §B5 (mobile day-strip replacing the week strip)
- §B6 (scroll restoration on history.back)
- §C1 full (category-pill expansion accordion)
- §C3, §C4 (in-section pill expansion + Top-3 + `더 보기`)
- §D1, §D3, §D5 (skeleton day sections, retry card, partial-state per-day rendering — beyond loading/error pass-through we already do)
- §E5 (bundle delta tracking)
- §F (Playwright/visual smoke harness)

**Safety / workspace boundaries (re-stated):**
- All work happens in worktree `/Users/mgh3326/worktrees/auto_trader/rob-185-calendar-grouped-list`. Never edit `/Users/mgh3326/services/auto_trader/current` or shared `work/` checkouts.
- No production DB writes, no scheduler activation, no broker / order / watch / order-intent / paper / live trading mutations.
- Planner / reviewer preference: Claude Code Opus. Implementer preference: Claude Code Sonnet. If runtime cannot enforce, record the limitation in the PR description.

---

## File Map

**Modify:**
- `frontend/invest/src/components/calendar/vm.ts` — add `monthDaysIso`, `dayHeaderLabel`, `dayTotalLabel`, `monthEmptyLabel`, `dayEmptyLabel`, `sourceFriendlyLabel`, `sourceStaleStatusCopy`. Soften `clampCount` to return `많음` instead of `+999` for ≥ 1000 (and keep current numerals 1–999). Existing `dataStateLabel` / `freshnessBadgeLabel` stay in the file but are no longer called from default-surface components; their downstream callers move into the new source popover.
- `frontend/invest/src/components/calendar/MonthCalendarGrid.tsx` — swap `clampCount(n)` rule from `>= 1000 → "+999"` to `>= 1000 → "많음"` (consequence of §C2; one-line change).
- `frontend/invest/src/components/calendar/EventRow.tsx` — render empty string for null `actual` / `forecast` / `previous` (was `"—"`); keep CSS layout so empty cells preserve column alignment on desktop.
- `frontend/invest/src/components/calendar/ClusterRow.tsx` — remove the `+{cluster.count}` chip. The cluster title already carries `… N건`; the redundant chip is the dashboard-feel signal.
- `frontend/invest/src/pages/desktop/DesktopCalendarPage.tsx` — swap `SelectedDateEvents` for `MonthlyEventsTimeline`; remove the `CalendarFreshnessBanner` unconditional block at lines 249-253; mount a `CalendarSourceButton` in its place; remove the `selectedDay` shadow (the timeline gets the whole `filteredByDate` map).
- `frontend/invest/src/pages/mobile/MobileCalendarPage.tsx` — same swap; remove the banner block at lines 218-222; mount `CalendarSourceButton`; remove the single-day `filteredSelected` memo (timeline takes the full map).
- `frontend/invest/src/styles/calendar.css` — add `.calendar-timeline`, `.calendar-day-section`, `.calendar-day-section--selected`, `.calendar-day-section__header`, `.calendar-day-section__empty`, `.calendar-source-button`, `.calendar-source-popover`, `.calendar-source-row` rules. Reuse existing tokens (`--surface-2`, `--accent`, `--accent-soft`, `--fg-3`, etc.).
- `frontend/invest/src/__tests__/EventRow.test.tsx` — add an "empty cells, not em-dash" test.
- `frontend/invest/src/__tests__/ClusterRow.test.tsx` — flip the count-chip test from "expects +327" to "absent".
- `frontend/invest/src/__tests__/MonthCalendarGrid.test.tsx` — flip the `+999` test to `많음`.
- `frontend/invest/src/__tests__/DesktopCalendarPage.test.tsx` — update the click-to-filter assertions to scroll-target assertions; verify no banner; verify timeline-day-section anchors.
- `frontend/invest/src/__tests__/MobileCalendarPage.test.tsx` — same.
- `frontend/invest/src/__tests__/SelectedDateEvents.test.tsx` — keep file; the component is no longer mounted by the pages but stays exported so the test suite still passes (acts as a leaf-component contract test until ROB-186 removes the file). Add a top-of-file comment noting the deprecation. **No assertion changes** — this keeps the diff bounded.

**Create:**
- `frontend/invest/src/components/calendar/DaySection.tsx`
- `frontend/invest/src/components/calendar/MonthlyEventsTimeline.tsx`
- `frontend/invest/src/components/calendar/CalendarSourceButton.tsx`
- `frontend/invest/src/__tests__/DaySection.test.tsx`
- `frontend/invest/src/__tests__/MonthlyEventsTimeline.test.tsx`
- `frontend/invest/src/__tests__/CalendarSourceButton.test.tsx`
- `frontend/invest/src/__tests__/calendarTimelineVm.test.ts`

---

## Task 1: VM helpers for the grouped-monthly timeline

**Files:**
- Modify: `frontend/invest/src/components/calendar/vm.ts`
- Test: `frontend/invest/src/__tests__/calendarTimelineVm.test.ts` (new)

- [ ] **Step 1.1: Write the failing test file**

Create `frontend/invest/src/__tests__/calendarTimelineVm.test.ts`:

```ts
import { describe, expect, test } from "vitest";
import {
  dayHeaderLabel,
  dayTotalLabel,
  dayEmptyLabel,
  monthEmptyLabel,
  monthDaysIso,
  sourceFriendlyLabel,
  sourceStaleStatusCopy,
} from "../components/calendar/vm";
import type { CalendarSourceStatus } from "../types/calendar";

describe("ROB-185 timeline VM helpers", () => {
  test("monthDaysIso returns every in-month date in order (May 2026 -> 31 entries)", () => {
    const out = monthDaysIso(new Date(2026, 4, 1));
    expect(out).toHaveLength(31);
    expect(out[0]).toBe("2026-05-01");
    expect(out[30]).toBe("2026-05-31");
  });

  test("monthDaysIso handles 30-day months (June)", () => {
    const out = monthDaysIso(new Date(2026, 5, 15));
    expect(out).toHaveLength(30);
    expect(out[0]).toBe("2026-06-01");
    expect(out[29]).toBe("2026-06-30");
  });

  test("monthDaysIso handles leap-year February", () => {
    const out = monthDaysIso(new Date(2024, 1, 10));
    expect(out).toHaveLength(29);
    expect(out[28]).toBe("2024-02-29");
  });

  test("dayHeaderLabel prefixes 오늘 / 내일 within the current month", () => {
    expect(dayHeaderLabel("2026-05-11", "2026-05-11")).toBe("오늘 · 5월 11일 (월)");
    expect(dayHeaderLabel("2026-05-12", "2026-05-11")).toBe("내일 · 5월 12일 (화)");
    expect(dayHeaderLabel("2026-05-15", "2026-05-11")).toBe("5월 15일 (금)");
  });

  test("dayTotalLabel renders Korean noun phrasing", () => {
    expect(dayTotalLabel(3)).toBe("일정 3개");
    expect(dayTotalLabel(0)).toBe("");
  });

  test("dayEmptyLabel + monthEmptyLabel are fixed Korean copy", () => {
    expect(dayEmptyLabel()).toBe("이 날은 예정된 일정이 없어요");
    expect(monthEmptyLabel()).toBe("이번 달은 예정된 주요 일정이 없어요");
  });

  test("sourceFriendlyLabel maps internal source ids to plain Korean", () => {
    expect(sourceFriendlyLabel("finnhub")).toBe("미국 실적 일정");
    expect(sourceFriendlyLabel("dart")).toBe("한국 공시");
    expect(sourceFriendlyLabel("forexfactory")).toBe("경제 지표");
    // Unknown source falls back to a generic label, NOT the raw id.
    expect(sourceFriendlyLabel("wisefn")).toBe("기타 일정");
  });

  test("sourceStaleStatusCopy emits Toss-friendly copy for non-fresh states", () => {
    const stale: CalendarSourceStatus = {
      source: "finnhub", category: "earnings", market: "us", state: "stale",
      lastSuccessAt: null, lastFailureAt: null, lastError: null,
      succeededPartitions: 0, failedPartitions: 0, missingPartitions: 0, eventCount: 0,
    };
    expect(sourceStaleStatusCopy(stale)).toBe("방금 업데이트되지 않았어요");
    expect(sourceStaleStatusCopy({ ...stale, state: "failed" })).toBe("잠시 후 다시 확인할게요");
    expect(sourceStaleStatusCopy({ ...stale, state: "missing" })).toBe("잠시 후 다시 확인할게요");
    expect(sourceStaleStatusCopy({ ...stale, state: "fresh" })).toBeNull();
  });
});
```

- [ ] **Step 1.2: Run the new test file and verify every test fails**

Run: `cd frontend/invest && npx vitest run src/__tests__/calendarTimelineVm.test.ts`
Expected: 7 tests fail with "X is not a function" / "is not exported".

- [ ] **Step 1.3: Implement the helpers in vm.ts**

Append to `frontend/invest/src/components/calendar/vm.ts` (after the existing `clampSelectedDateToMonth` block, before the `--- ROB-167 freshness helpers ---` section):

```ts
// --- ROB-185 grouped-monthly timeline helpers ---

const KOREAN_DOW: readonly string[] = ["일", "월", "화", "수", "목", "금", "토"];

export function monthDaysIso(monthCursor: Date): string[] {
  const first = startOfMonth(monthCursor);
  const last = endOfMonth(monthCursor);
  const out: string[] = [];
  const cur = new Date(first);
  while (cur.getTime() <= last.getTime()) {
    out.push(fmtLocal(cur));
    cur.setDate(cur.getDate() + 1);
  }
  return out;
}

export function dayHeaderLabel(dateIso: string, todayIso: string): string {
  const [, mStr, dStr] = dateIso.split("-");
  const month = Number.parseInt(mStr ?? "0", 10);
  const day = Number.parseInt(dStr ?? "0", 10);
  const dow = KOREAN_DOW[new Date(`${dateIso}T00:00:00`).getDay()] ?? "";
  const base = `${month}월 ${day}일 (${dow})`;
  const prefix = relativeDayPrefix(dateIso, todayIso);
  return prefix == null ? base : `${prefix} · ${base}`;
}

export function dayTotalLabel(total: number): string {
  if (total <= 0) return "";
  return `일정 ${total}개`;
}

export function dayEmptyLabel(): string {
  return "이 날은 예정된 일정이 없어요";
}

export function monthEmptyLabel(): string {
  return "이번 달은 예정된 주요 일정이 없어요";
}

const SOURCE_FRIENDLY_MAP: Record<string, string> = {
  finnhub: "미국 실적 일정",
  dart: "한국 공시",
  forexfactory: "경제 지표",
};

export function sourceFriendlyLabel(source: string): string {
  return SOURCE_FRIENDLY_MAP[source] ?? "기타 일정";
}

export function sourceStaleStatusCopy(status: CalendarSourceStatus): string | null {
  switch (status.state) {
    case "fresh":
      return null;
    case "stale":
      return "방금 업데이트되지 않았어요";
    case "failed":
    case "missing":
      return "잠시 후 다시 확인할게요";
  }
}
```

- [ ] **Step 1.4: Run the test file and verify all pass**

Run: `cd frontend/invest && npx vitest run src/__tests__/calendarTimelineVm.test.ts`
Expected: PASS 7/7.

- [ ] **Step 1.5: Commit**

```bash
git add frontend/invest/src/components/calendar/vm.ts \
        frontend/invest/src/__tests__/calendarTimelineVm.test.ts
git commit -m "$(cat <<'EOF'
feat(ROB-185): timeline VM helpers — month days, day header, source copy

Add `monthDaysIso`, `dayHeaderLabel`, `dayTotalLabel`, `dayEmptyLabel`,
`monthEmptyLabel`, `sourceFriendlyLabel`, `sourceStaleStatusCopy` so the
grouped-monthly timeline + source popover can render Toss-friendly Korean
copy without leaking ingestion vocabulary. No callers yet.

Co-Authored-By: Paperclip <noreply@paperclip.ing>
EOF
)"
```

---

## Task 2: `DaySection` component

**Files:**
- Create: `frontend/invest/src/components/calendar/DaySection.tsx`
- Test: `frontend/invest/src/__tests__/DaySection.test.tsx` (new)

- [ ] **Step 2.1: Write the failing test file**

Create `frontend/invest/src/__tests__/DaySection.test.tsx`:

```tsx
import { render, screen, within } from "@testing-library/react";
import { describe, expect, test } from "vitest";
import { DaySection } from "../components/calendar/DaySection";
import type { CalendarClusterVM, CalendarEventVM } from "../components/calendar/vm";

function evt(id: string, title: string): CalendarEventVM {
  return {
    id, date: "2026-05-11", dayOfMonth: 11, monthDay: "5/11",
    type: "earnings", region: "us", title,
    time: null, released: false, actual: null, forecast: null, previous: null,
    own: null, badges: [],
  };
}

function cluster(id: string, count: number): CalendarClusterVM {
  return {
    id, date: "2026-05-11", dayOfMonth: 11, monthDay: "5/11",
    type: "earnings", region: "us", title: `미국 실적 발표 ${count}건`,
    count, topEvents: [],
  };
}

describe("DaySection (ROB-185)", () => {
  test("renders header with day label and total count when non-empty", () => {
    render(
      <DaySection
        dateIso="2026-05-11"
        todayIso="2026-05-11"
        events={[evt("e1", "AAPL")]}
        clusters={[cluster("c1", 5)]}
        selected={false}
      />,
    );
    const sec = screen.getByTestId("calendar-day-section");
    expect(sec).toHaveAttribute("data-day-anchor", "2026-05-11");
    expect(sec).toHaveTextContent("오늘 · 5월 11일 (월)");
    expect(sec).toHaveTextContent("일정 6개");
  });

  test("renders empty placeholder copy when no events/clusters and hides total", () => {
    render(
      <DaySection
        dateIso="2026-05-12"
        todayIso="2026-05-11"
        events={[]}
        clusters={[]}
        selected={false}
      />,
    );
    const sec = screen.getByTestId("calendar-day-section");
    expect(sec).toHaveTextContent("내일 · 5월 12일 (화)");
    expect(sec).toHaveTextContent("이 날은 예정된 일정이 없어요");
    expect(sec).not.toHaveTextContent("일정 0개");
  });

  test("renders events and clusters (clusters first, events after)", () => {
    render(
      <DaySection
        dateIso="2026-05-13"
        todayIso="2026-05-11"
        events={[evt("e1", "Event A"), evt("e2", "Event B")]}
        clusters={[cluster("c1", 12)]}
        selected={false}
      />,
    );
    expect(within(screen.getByTestId("calendar-day-section")).getAllByTestId("calendar-event")).toHaveLength(2);
    expect(screen.getByTestId("calendar-cluster")).toBeInTheDocument();
  });

  test("applies data-selected='true' when selected, default 'false' otherwise", () => {
    const { rerender } = render(
      <DaySection
        dateIso="2026-05-15"
        todayIso="2026-05-11"
        events={[]}
        clusters={[]}
        selected
      />,
    );
    expect(screen.getByTestId("calendar-day-section")).toHaveAttribute("data-selected", "true");

    rerender(
      <DaySection
        dateIso="2026-05-15"
        todayIso="2026-05-11"
        events={[]}
        clusters={[]}
        selected={false}
      />,
    );
    expect(screen.getByTestId("calendar-day-section")).toHaveAttribute("data-selected", "false");
  });

  test("header uses sticky CSS class so longer scrolls keep the day label visible", () => {
    render(
      <DaySection
        dateIso="2026-05-11"
        todayIso="2026-05-11"
        events={[evt("e1", "x")]}
        clusters={[]}
        selected={false}
      />,
    );
    const header = screen.getByTestId("calendar-day-section-header");
    expect(header).toHaveClass("calendar-day-section__header");
  });

  test("renders as a semantic <section> with an accessible label", () => {
    render(
      <DaySection
        dateIso="2026-05-11"
        todayIso="2026-05-11"
        events={[evt("e1", "x")]}
        clusters={[]}
        selected={false}
      />,
    );
    const sec = screen.getByTestId("calendar-day-section");
    expect(sec.tagName).toBe("SECTION");
    expect(sec.getAttribute("aria-label")).toContain("5월 11일");
  });
});
```

- [ ] **Step 2.2: Run the test file and verify it fails (component missing)**

Run: `cd frontend/invest && npx vitest run src/__tests__/DaySection.test.tsx`
Expected: FAIL — "Cannot find module './DaySection'".

- [ ] **Step 2.3: Implement `DaySection.tsx`**

Create `frontend/invest/src/components/calendar/DaySection.tsx`:

```tsx
import { forwardRef } from "react";
import { ClusterRow } from "./ClusterRow";
import { EventRow } from "./EventRow";
import type { CalendarClusterVM, CalendarEventVM } from "./vm";
import { dayEmptyLabel, dayHeaderLabel, dayTotalLabel } from "./vm";

export interface DaySectionProps {
  dateIso: string;
  todayIso: string;
  events: CalendarEventVM[];
  clusters: CalendarClusterVM[];
  selected: boolean;
}

export const DaySection = forwardRef<HTMLElement, DaySectionProps>(function DaySection(
  { dateIso, todayIso, events, clusters, selected },
  ref,
) {
  const total = events.length + clusters.reduce((s, c) => s + c.count, 0);
  const isEmpty = events.length === 0 && clusters.length === 0;
  const headerLabel = dayHeaderLabel(dateIso, todayIso);

  return (
    <section
      ref={ref}
      data-testid="calendar-day-section"
      data-day-anchor={dateIso}
      data-selected={selected ? "true" : "false"}
      aria-label={headerLabel}
      className={
        selected
          ? "calendar-day-section calendar-day-section--selected"
          : "calendar-day-section"
      }
    >
      <header
        data-testid="calendar-day-section-header"
        className="calendar-day-section__header"
      >
        <span className="calendar-day-section__label">{headerLabel}</span>
        {total > 0 && (
          <span className="calendar-day-section__total">{dayTotalLabel(total)}</span>
        )}
      </header>
      {isEmpty ? (
        <div className="calendar-day-section__empty">{dayEmptyLabel()}</div>
      ) : (
        <div className="calendar-day-section__body">
          {clusters.map((c) => (
            <ClusterRow key={c.id} cluster={c} />
          ))}
          {events.map((ev) => (
            <EventRow key={ev.id} ev={ev} />
          ))}
        </div>
      )}
    </section>
  );
});
```

- [ ] **Step 2.4: Run the test and verify it passes**

Run: `cd frontend/invest && npx vitest run src/__tests__/DaySection.test.tsx`
Expected: PASS 6/6.

- [ ] **Step 2.5: Add `DaySection` CSS**

Append to `frontend/invest/src/styles/calendar.css`:

```css
/* ---------- ROB-185 grouped-monthly timeline ---------- */
.calendar-timeline {
  display: flex;
  flex-direction: column;
  gap: 12px;
  min-width: 0;
}
.calendar-timeline__empty {
  padding: 48px 16px;
  text-align: center;
  color: var(--fg-3);
  font-size: 13px;
}
.calendar-day-section {
  display: flex;
  flex-direction: column;
  gap: 4px;
  scroll-margin-top: 76px; /* leaves room for sticky filters on desktop */
}
.calendar-day-section--selected {
  background: var(--accent-soft);
  border-radius: 12px;
  padding: 4px 4px 8px;
}
.calendar-day-section__header {
  position: sticky;
  top: 0;
  z-index: 1;
  background: var(--surface);
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 8px;
  padding: 8px 8px 6px;
  font-size: 13px;
  font-weight: 800;
  letter-spacing: -0.01em;
  color: var(--fg);
}
.calendar-day-section--selected .calendar-day-section__header {
  background: var(--accent-soft);
}
.calendar-day-section__total {
  font-size: 11px;
  font-weight: 600;
  color: var(--fg-3);
  font-feature-settings: "tnum";
}
.calendar-day-section__empty {
  padding: 12px 14px;
  color: var(--fg-3);
  font-size: 12px;
}
.calendar-day-section__body {
  display: flex;
  flex-direction: column;
  gap: 2px;
  min-width: 0;
}
```

- [ ] **Step 2.6: Re-run all calendar tests to make sure nothing else regressed**

Run: `cd frontend/invest && npx vitest run src/__tests__/DaySection.test.tsx src/__tests__/ClusterRow.test.tsx src/__tests__/EventRow.test.tsx`
Expected: PASS for all three files.

- [ ] **Step 2.7: Commit**

```bash
git add frontend/invest/src/components/calendar/DaySection.tsx \
        frontend/invest/src/__tests__/DaySection.test.tsx \
        frontend/invest/src/styles/calendar.css
git commit -m "$(cat <<'EOF'
feat(ROB-185): DaySection — sticky-header day group for monthly timeline

New leaf component rendering one day's clusters/events with a sticky day
label and a Toss-friendly empty placeholder. Not yet wired in pages.

Co-Authored-By: Paperclip <noreply@paperclip.ing>
EOF
)"
```

---

## Task 3: `MonthlyEventsTimeline` component

**Files:**
- Create: `frontend/invest/src/components/calendar/MonthlyEventsTimeline.tsx`
- Test: `frontend/invest/src/__tests__/MonthlyEventsTimeline.test.tsx` (new)

- [ ] **Step 3.1: Write the failing test file**

Create `frontend/invest/src/__tests__/MonthlyEventsTimeline.test.tsx`:

```tsx
import { render, screen, within } from "@testing-library/react";
import { describe, expect, test, vi } from "vitest";
import { MonthlyEventsTimeline } from "../components/calendar/MonthlyEventsTimeline";
import type { CalendarClusterVM, CalendarEventVM } from "../components/calendar/vm";

function evt(id: string, dateIso: string, title: string): CalendarEventVM {
  const day = Number.parseInt(dateIso.slice(8, 10), 10);
  const month = Number.parseInt(dateIso.slice(5, 7), 10);
  return {
    id, date: dateIso, dayOfMonth: day, monthDay: `${month}/${day}`,
    type: "earnings", region: "us", title,
    time: null, released: false, actual: null, forecast: null, previous: null,
    own: null, badges: [],
  };
}

function cluster(id: string, dateIso: string, count: number): CalendarClusterVM {
  const day = Number.parseInt(dateIso.slice(8, 10), 10);
  const month = Number.parseInt(dateIso.slice(5, 7), 10);
  return {
    id, date: dateIso, dayOfMonth: day, monthDay: `${month}/${day}`,
    type: "earnings", region: "us", title: `미국 실적 발표 ${count}건`,
    count, topEvents: [],
  };
}

const baseProps = {
  monthCursor: new Date(2026, 4, 1), // May 2026
  selectedDate: "2026-05-11",
  todayIso: "2026-05-11",
  filteredByDate: new Map<string, { events: CalendarEventVM[]; clusters: CalendarClusterVM[]; total: number }>(),
};

describe("MonthlyEventsTimeline", () => {
  test("renders one section per in-month day (31 for May 2026)", () => {
    render(<MonthlyEventsTimeline {...baseProps} />);
    const sections = screen.getAllByTestId("calendar-day-section");
    expect(sections).toHaveLength(31);
    expect(sections[0]).toHaveAttribute("data-day-anchor", "2026-05-01");
    expect(sections[30]).toHaveAttribute("data-day-anchor", "2026-05-31");
  });

  test("loading prop renders a single skeleton block, not 31 sections", () => {
    render(<MonthlyEventsTimeline {...baseProps} loading />);
    expect(screen.getByTestId("calendar-loading")).toBeInTheDocument();
    expect(screen.queryAllByTestId("calendar-day-section")).toHaveLength(0);
  });

  test("error prop renders the error banner, not sections", () => {
    render(<MonthlyEventsTimeline {...baseProps} error="boom" />);
    expect(screen.getByTestId("calendar-error")).toHaveTextContent("boom");
    expect(screen.queryAllByTestId("calendar-day-section")).toHaveLength(0);
  });

  test("empty filteredByDate renders all 31 day sections, each with the empty placeholder", () => {
    render(<MonthlyEventsTimeline {...baseProps} />);
    const sections = screen.getAllByTestId("calendar-day-section");
    expect(sections).toHaveLength(31);
    expect(sections[0]).toHaveTextContent("이 날은 예정된 일정이 없어요");
    // The month-level "empty" copy is rendered above the sections when total == 0.
    expect(screen.getByTestId("calendar-timeline-empty")).toHaveTextContent(
      "이번 달은 예정된 주요 일정이 없어요",
    );
  });

  test("non-empty filteredByDate hydrates the matching section's events/clusters", () => {
    const filtered = new Map<string, { events: CalendarEventVM[]; clusters: CalendarClusterVM[]; total: number }>([
      ["2026-05-11", { events: [evt("e1", "2026-05-11", "AAPL")], clusters: [], total: 1 }],
      ["2026-05-13", { events: [], clusters: [cluster("c1", "2026-05-13", 4)], total: 4 }],
    ]);
    render(<MonthlyEventsTimeline {...baseProps} filteredByDate={filtered} />);
    expect(screen.queryByTestId("calendar-timeline-empty")).not.toBeInTheDocument();
    const may11 = screen.getByTestId("calendar-day-section");
    expect(within(may11).getByText("AAPL")).toBeInTheDocument();
    // May 13 cluster present somewhere in the timeline.
    expect(screen.getByText("미국 실적 발표 4건")).toBeInTheDocument();
  });

  test("selectedDate flags only the matching section", () => {
    render(<MonthlyEventsTimeline {...baseProps} selectedDate="2026-05-15" />);
    const may15 = document.querySelector('[data-day-anchor="2026-05-15"]');
    const may11 = document.querySelector('[data-day-anchor="2026-05-11"]');
    expect(may15).toHaveAttribute("data-selected", "true");
    expect(may11).toHaveAttribute("data-selected", "false");
  });

  test("changing selectedDate calls scrollIntoView on the matching section", () => {
    const spy = vi.fn();
    // Patch Element.prototype so any scrollIntoView call is captured.
    const original = Element.prototype.scrollIntoView;
    Element.prototype.scrollIntoView = spy;
    try {
      const { rerender } = render(<MonthlyEventsTimeline {...baseProps} selectedDate="2026-05-11" />);
      spy.mockClear();
      rerender(<MonthlyEventsTimeline {...baseProps} selectedDate="2026-05-20" />);
      expect(spy).toHaveBeenCalledTimes(1);
    } finally {
      Element.prototype.scrollIntoView = original;
    }
  });

  test("does not scroll on first mount (initial render is not a navigation)", () => {
    const spy = vi.fn();
    const original = Element.prototype.scrollIntoView;
    Element.prototype.scrollIntoView = spy;
    try {
      render(<MonthlyEventsTimeline {...baseProps} selectedDate="2026-05-15" />);
      expect(spy).not.toHaveBeenCalled();
    } finally {
      Element.prototype.scrollIntoView = original;
    }
  });
});
```

- [ ] **Step 3.2: Run the test file and verify it fails (component missing)**

Run: `cd frontend/invest && npx vitest run src/__tests__/MonthlyEventsTimeline.test.tsx`
Expected: FAIL — "Cannot find module './MonthlyEventsTimeline'".

- [ ] **Step 3.3: Implement `MonthlyEventsTimeline.tsx`**

Create `frontend/invest/src/components/calendar/MonthlyEventsTimeline.tsx`:

```tsx
import { useEffect, useMemo, useRef } from "react";
import { DaySection } from "./DaySection";
import type { CalendarClusterVM, CalendarEventVM } from "./vm";
import { monthDaysIso, monthEmptyLabel } from "./vm";

export interface MonthlyDay {
  events: CalendarEventVM[];
  clusters: CalendarClusterVM[];
  total: number;
}

export interface MonthlyEventsTimelineProps {
  monthCursor: Date;
  selectedDate: string;
  todayIso: string;
  filteredByDate: Map<string, MonthlyDay>;
  loading?: boolean;
  error?: string | null;
}

export function MonthlyEventsTimeline({
  monthCursor,
  selectedDate,
  todayIso,
  filteredByDate,
  loading = false,
  error = null,
}: MonthlyEventsTimelineProps) {
  const days = useMemo(() => monthDaysIso(monthCursor), [monthCursor]);
  const refs = useRef<Map<string, HTMLElement | null>>(new Map());

  // Track whether this is the first render — first mount must not steal the scroll.
  const isFirstRender = useRef(true);

  useEffect(() => {
    if (isFirstRender.current) {
      isFirstRender.current = false;
      return;
    }
    const node = refs.current.get(selectedDate);
    if (!node) return;
    // Some test environments (older jsdom) leave scrollIntoView undefined;
    // guard so the page never crashes if the platform lacks it.
    if (typeof node.scrollIntoView !== "function") return;
    const reduceMotion =
      typeof window !== "undefined" &&
      typeof window.matchMedia === "function" &&
      window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    node.scrollIntoView({
      behavior: reduceMotion ? "auto" : "smooth",
      block: "start",
    });
  }, [selectedDate]);

  if (loading) {
    return (
      <div data-testid="calendar-loading" className="calendar-loading">
        {Array.from({ length: 3 }, (_, i) => (
          <div key={i} className="calendar-loading__row" aria-hidden="true" />
        ))}
        <span className="calendar-loading__sr">일정을 불러오는 중입니다…</span>
      </div>
    );
  }

  if (error) {
    return (
      <div data-testid="calendar-error" role="alert" className="calendar-error">
        <strong className="calendar-error__title">일정을 불러올 수 없습니다</strong>
        <span className="calendar-error__detail">{error}</span>
      </div>
    );
  }

  const monthHasAny = days.some((iso) => (filteredByDate.get(iso)?.total ?? 0) > 0);

  return (
    <div
      data-testid="calendar-timeline"
      className="calendar-timeline"
      role="region"
      aria-label="이번 달 일정"
    >
      {!monthHasAny && (
        <div data-testid="calendar-timeline-empty" className="calendar-timeline__empty">
          {monthEmptyLabel()}
        </div>
      )}
      {days.map((iso) => {
        const day = filteredByDate.get(iso) ?? { events: [], clusters: [], total: 0 };
        return (
          <DaySection
            key={iso}
            ref={(node) => {
              if (node) refs.current.set(iso, node);
              else refs.current.delete(iso);
            }}
            dateIso={iso}
            todayIso={todayIso}
            events={day.events}
            clusters={day.clusters}
            selected={iso === selectedDate}
          />
        );
      })}
    </div>
  );
}
```

- [ ] **Step 3.4: Run the test and verify it passes**

Run: `cd frontend/invest && npx vitest run src/__tests__/MonthlyEventsTimeline.test.tsx`
Expected: PASS 8/8.

- [ ] **Step 3.5: Commit**

```bash
git add frontend/invest/src/components/calendar/MonthlyEventsTimeline.tsx \
        frontend/invest/src/__tests__/MonthlyEventsTimeline.test.tsx
git commit -m "$(cat <<'EOF'
feat(ROB-185): MonthlyEventsTimeline — month-grouped event feed

New component renders one DaySection per in-month day. selectedDate is a
scroll target (not a filter): when it changes, the matching section is
scrolled into view, respecting prefers-reduced-motion. First mount is a
no-op so users land at the top of the month.

Co-Authored-By: Paperclip <noreply@paperclip.ing>
EOF
)"
```

---

## Task 4: Strip dashboard chrome from `EventRow`, `ClusterRow`, and `MonthCalendarGrid`

**Files:**
- Modify: `frontend/invest/src/components/calendar/EventRow.tsx`
- Modify: `frontend/invest/src/components/calendar/ClusterRow.tsx`
- Modify: `frontend/invest/src/components/calendar/MonthCalendarGrid.tsx`
- Modify tests: `frontend/invest/src/__tests__/EventRow.test.tsx`, `ClusterRow.test.tsx`, `MonthCalendarGrid.test.tsx`

- [ ] **Step 4.1: Flip the three existing tests to the new expectations (red)**

Edit `frontend/invest/src/__tests__/EventRow.test.tsx`. Add this test at the bottom of the `describe` block:

```tsx
  test("renders empty cells (not em-dash) when actual/forecast/previous are null", () => {
    render(<EventRow ev={ev({ actual: null, forecast: null, previous: null })} />);
    const row = screen.getByTestId("calendar-event");
    expect(row.textContent ?? "").not.toContain("—");
    expect(row.querySelector(".calendar-event-row__num--actual")?.textContent ?? "").toBe("");
    expect(row.querySelector(".calendar-event-row__num--forecast")?.textContent ?? "").toBe("");
    expect(row.querySelector(".calendar-event-row__num--previous")?.textContent ?? "").toBe("");
  });
```

Edit `frontend/invest/src/__tests__/ClusterRow.test.tsx`. Replace the existing block:

```tsx
  test("count chip is visible separately from the title for narrow screens", () => {
    render(<ClusterRow cluster={cluster({ count: 327 })} />);
    expect(screen.getByTestId("calendar-cluster-count")).toHaveTextContent("+327");
  });
```

with:

```tsx
  test("count chip is removed — cluster title already carries the count", () => {
    render(<ClusterRow cluster={cluster({ count: 327 })} />);
    expect(screen.queryByTestId("calendar-cluster-count")).not.toBeInTheDocument();
    // The cluster title still surfaces the number to the user.
    expect(screen.getByText("미국 실적 발표 327건")).toBeInTheDocument();
    // And no leftover raw +N anywhere.
    expect(screen.getByTestId("calendar-cluster").textContent ?? "").not.toMatch(/\+\d/);
  });
```

Edit `frontend/invest/src/__tests__/MonthCalendarGrid.test.tsx`. Replace:

```tsx
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
```

with:

```tsx
  test("count badge renders 많음 for any count >= 1000 (no +999 raw label)", () => {
    render(
      <MonthCalendarGrid
        {...baseProps}
        countByDate={new Map([["2026-05-13", 1234]])}
        onSelect={() => {}}
      />,
    );
    const cell = screen.getByTestId("month-grid-cell-2026-05-13");
    expect(cell).toHaveTextContent("많음");
    expect(cell.textContent ?? "").not.toContain("+999");
  });
```

- [ ] **Step 4.2: Run the three tests to verify they fail**

Run: `cd frontend/invest && npx vitest run src/__tests__/EventRow.test.tsx src/__tests__/ClusterRow.test.tsx src/__tests__/MonthCalendarGrid.test.tsx`
Expected: FAIL on the three new/changed cases.

- [ ] **Step 4.3: Update `EventRow.tsx` — drop em-dashes**

Edit `frontend/invest/src/components/calendar/EventRow.tsx`. Replace the three numeric blocks:

```tsx
      <div className="calendar-event-row__num calendar-event-row__num--actual" data-released={ev.released ? "true" : "false"}>
        {ev.actual ?? "—"}
      </div>
      <div className="calendar-event-row__num calendar-event-row__num--forecast">
        {ev.forecast ?? "—"}
      </div>
      <div className="calendar-event-row__num calendar-event-row__num--previous">
        {ev.previous ?? "—"}
      </div>
```

with:

```tsx
      <div className="calendar-event-row__num calendar-event-row__num--actual" data-released={ev.released ? "true" : "false"}>
        {ev.actual ?? ""}
      </div>
      <div className="calendar-event-row__num calendar-event-row__num--forecast">
        {ev.forecast ?? ""}
      </div>
      <div className="calendar-event-row__num calendar-event-row__num--previous">
        {ev.previous ?? ""}
      </div>
```

- [ ] **Step 4.4: Update `ClusterRow.tsx` — drop the `+N` chip**

Edit `frontend/invest/src/components/calendar/ClusterRow.tsx`. Replace the entire title-line block:

```tsx
        <div className="calendar-cluster-row__title-line">
          <RegionBadge region={cluster.region} />
          <span className="calendar-cluster-row__title" title={cluster.title}>
            {cluster.title}
          </span>
          <span data-testid="calendar-cluster-count" className="calendar-cluster-row__count">
            +{cluster.count}
          </span>
        </div>
```

with:

```tsx
        <div className="calendar-cluster-row__title-line">
          <RegionBadge region={cluster.region} />
          <span className="calendar-cluster-row__title" title={cluster.title}>
            {cluster.title}
          </span>
        </div>
```

The unused `.calendar-cluster-row__count` CSS rule may remain in `calendar.css` — leaving stale CSS class definitions is cheap; removing them would expand the diff with no behavioural payoff.

- [ ] **Step 4.5: Update `MonthCalendarGrid.tsx` — `clampCount` returns `많음`**

Edit `frontend/invest/src/components/calendar/MonthCalendarGrid.tsx`. Replace:

```tsx
function clampCount(n: number): string {
  if (n >= 1000) return "+999";
  return String(n);
}
```

with:

```tsx
function clampCount(n: number): string {
  if (n >= 1000) return "많음";
  return String(n);
}
```

- [ ] **Step 4.6: Re-run the three test files and verify they pass**

Run: `cd frontend/invest && npx vitest run src/__tests__/EventRow.test.tsx src/__tests__/ClusterRow.test.tsx src/__tests__/MonthCalendarGrid.test.tsx`
Expected: PASS all.

- [ ] **Step 4.7: Commit**

```bash
git add frontend/invest/src/components/calendar/EventRow.tsx \
        frontend/invest/src/components/calendar/ClusterRow.tsx \
        frontend/invest/src/components/calendar/MonthCalendarGrid.tsx \
        frontend/invest/src/__tests__/EventRow.test.tsx \
        frontend/invest/src/__tests__/ClusterRow.test.tsx \
        frontend/invest/src/__tests__/MonthCalendarGrid.test.tsx
git commit -m "$(cat <<'EOF'
refactor(ROB-185): strip dashboard chrome — em-dash, +N chip, +999

EventRow renders empty cells for null actual/forecast/previous instead of
the em-dash placeholder. ClusterRow drops the redundant +N count chip —
the cluster title already says "미국 실적 발표 327건". MonthCalendarGrid
overflow label becomes "많음" instead of "+999". Tests follow the new
copy contracts.

Co-Authored-By: Paperclip <noreply@paperclip.ing>
EOF
)"
```

---

## Task 5: `CalendarSourceButton` — quiet replacement for the freshness banner

**Files:**
- Create: `frontend/invest/src/components/calendar/CalendarSourceButton.tsx`
- Test: `frontend/invest/src/__tests__/CalendarSourceButton.test.tsx` (new)

- [ ] **Step 5.1: Write the failing test file**

Create `frontend/invest/src/__tests__/CalendarSourceButton.test.tsx`:

```tsx
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, test } from "vitest";
import { CalendarSourceButton } from "../components/calendar/CalendarSourceButton";
import type { CalendarSourceStatus } from "../types/calendar";

function src(overrides: Partial<CalendarSourceStatus>): CalendarSourceStatus {
  return {
    source: "finnhub", category: "earnings", market: "us", state: "fresh",
    lastSuccessAt: null, lastFailureAt: null, lastError: null,
    succeededPartitions: 0, failedPartitions: 0, missingPartitions: 0, eventCount: 0,
    ...overrides,
  };
}

describe("CalendarSourceButton (ROB-185)", () => {
  test("renders quiet button labelled '데이터 출처'", () => {
    render(<CalendarSourceButton sources={[src({ state: "fresh" })]} />);
    const btn = screen.getByTestId("calendar-source-button");
    expect(btn).toHaveTextContent("데이터 출처");
    // No banner element should ever appear regardless of source states.
    expect(screen.queryByTestId("calendar-freshness-banner")).not.toBeInTheDocument();
  });

  test("popover is hidden by default", () => {
    render(<CalendarSourceButton sources={[src({ state: "fresh" })]} />);
    expect(screen.queryByTestId("calendar-source-popover")).not.toBeInTheDocument();
  });

  test("clicking the button opens the popover with one row per source", async () => {
    const user = userEvent.setup();
    render(
      <CalendarSourceButton
        sources={[
          src({ source: "finnhub", state: "fresh" }),
          src({ source: "dart", state: "stale" }),
        ]}
      />,
    );
    await user.click(screen.getByTestId("calendar-source-button"));
    const pop = screen.getByTestId("calendar-source-popover");
    // Two rows.
    expect(pop.querySelectorAll('[data-testid="calendar-source-row"]')).toHaveLength(2);
    // Friendly Korean labels, not source ids.
    expect(pop).toHaveTextContent("미국 실적 일정");
    expect(pop).toHaveTextContent("한국 공시");
    expect(pop).not.toHaveTextContent("finnhub");
    expect(pop).not.toHaveTextContent("dart");
    expect(pop).not.toHaveTextContent("ForexFactory");
  });

  test("stale source row shows the friendly stale copy, fresh row does not", async () => {
    const user = userEvent.setup();
    render(
      <CalendarSourceButton
        sources={[
          src({ source: "finnhub", state: "fresh" }),
          src({ source: "dart", state: "stale" }),
        ]}
      />,
    );
    await user.click(screen.getByTestId("calendar-source-button"));
    const rows = screen.getAllByTestId("calendar-source-row");
    const dartRow = rows.find((r) => r.textContent?.includes("한국 공시"))!;
    expect(dartRow).toHaveTextContent("방금 업데이트되지 않았어요");
    const finnRow = rows.find((r) => r.textContent?.includes("미국 실적 일정"))!;
    expect(finnRow).not.toHaveTextContent("방금");
  });

  test("button has aria-expanded reflecting popover state", async () => {
    const user = userEvent.setup();
    render(<CalendarSourceButton sources={[src({ state: "fresh" })]} />);
    const btn = screen.getByTestId("calendar-source-button");
    expect(btn).toHaveAttribute("aria-expanded", "false");
    await user.click(btn);
    expect(btn).toHaveAttribute("aria-expanded", "true");
  });

  test("empty sources list still renders the button (silent passthrough)", () => {
    render(<CalendarSourceButton sources={[]} />);
    expect(screen.getByTestId("calendar-source-button")).toBeInTheDocument();
  });

  test("the default DOM contains no banished operational strings", () => {
    render(
      <CalendarSourceButton
        sources={[src({ source: "finnhub", state: "stale" })]}
      />,
    );
    const body = document.body.textContent ?? "";
    for (const bad of [
      "데이터 상태:",
      "오래됨",
      "수집 실패",
      "미수집",
      "Finnhub",
      "DART",
      "ForexFactory",
    ]) {
      expect(body).not.toContain(bad);
    }
  });
});
```

- [ ] **Step 5.2: Run the test file and verify it fails**

Run: `cd frontend/invest && npx vitest run src/__tests__/CalendarSourceButton.test.tsx`
Expected: FAIL — module not found.

- [ ] **Step 5.3: Implement `CalendarSourceButton.tsx`**

Create `frontend/invest/src/components/calendar/CalendarSourceButton.tsx`:

```tsx
import { useId, useState } from "react";
import type { CalendarSourceStatus } from "../../types/calendar";
import { sourceFriendlyLabel, sourceStaleStatusCopy } from "./vm";

export function CalendarSourceButton({ sources }: { sources: CalendarSourceStatus[] }) {
  const [open, setOpen] = useState(false);
  const popoverId = useId();

  return (
    <div className="calendar-source-button-wrap">
      <button
        type="button"
        data-testid="calendar-source-button"
        className="calendar-source-button"
        aria-expanded={open ? "true" : "false"}
        aria-controls={popoverId}
        onClick={() => setOpen((v) => !v)}
      >
        데이터 출처
      </button>
      {open && (
        <div
          id={popoverId}
          data-testid="calendar-source-popover"
          role="dialog"
          aria-label="데이터 출처"
          className="calendar-source-popover"
        >
          {sources.map((s) => {
            const stale = sourceStaleStatusCopy(s);
            return (
              <div
                key={`${s.source}-${s.category}-${s.market}`}
                data-testid="calendar-source-row"
                data-source={s.source}
                data-state={s.state}
                className="calendar-source-row"
              >
                <span className="calendar-source-row__label">{sourceFriendlyLabel(s.source)}</span>
                {stale != null && (
                  <span className="calendar-source-row__status">{stale}</span>
                )}
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}
```

- [ ] **Step 5.4: Run the test and verify it passes**

Run: `cd frontend/invest && npx vitest run src/__tests__/CalendarSourceButton.test.tsx`
Expected: PASS 7/7.

- [ ] **Step 5.5: Add CSS for the button + popover**

Append to `frontend/invest/src/styles/calendar.css`:

```css
/* ---------- ROB-185 source button + popover ---------- */
.calendar-source-button-wrap {
  position: relative;
  display: inline-flex;
}
.calendar-source-button {
  border: none;
  background: transparent;
  color: var(--fg-3);
  font-family: inherit;
  font-size: 12px;
  font-weight: 600;
  cursor: pointer;
  padding: 8px 10px;
  border-radius: 8px;
  min-height: 44px; /* touch target */
}
.calendar-source-button:hover { background: var(--surface-2); color: var(--fg-2); }
.calendar-source-button:focus-visible {
  outline: none;
  box-shadow: var(--shadow-focus);
}
.calendar-source-popover {
  position: absolute;
  top: calc(100% + 6px);
  right: 0;
  z-index: 10;
  min-width: 240px;
  padding: 10px;
  background: var(--surface);
  border: 1px solid var(--divider);
  border-radius: 12px;
  box-shadow: var(--shadow-2, 0 8px 24px rgba(0,0,0,0.08));
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.calendar-source-row {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 12px;
  font-size: 13px;
  color: var(--fg-1);
}
.calendar-source-row__status {
  font-size: 11px;
  color: var(--fg-3);
}
```

- [ ] **Step 5.6: Commit**

```bash
git add frontend/invest/src/components/calendar/CalendarSourceButton.tsx \
        frontend/invest/src/__tests__/CalendarSourceButton.test.tsx \
        frontend/invest/src/styles/calendar.css
git commit -m "$(cat <<'EOF'
feat(ROB-185): CalendarSourceButton — quiet 데이터 출처 affordance

Minimal popover-style source-status surface that replaces the
CalendarFreshnessBanner. Source names render as plain Korean
(미국 실적 일정 / 한국 공시 / 경제 지표), and stale rows show
"방금 업데이트되지 않았어요" instead of internal state vocabulary.
Not yet mounted in pages.

Co-Authored-By: Paperclip <noreply@paperclip.ing>
EOF
)"
```

---

## Task 6: Wire the timeline into `DesktopCalendarPage`

**Files:**
- Modify: `frontend/invest/src/pages/desktop/DesktopCalendarPage.tsx`
- Modify: `frontend/invest/src/__tests__/DesktopCalendarPage.test.tsx`

- [ ] **Step 6.1: Update tests — they currently encode the "click = filter" rule we're killing**

Edit `frontend/invest/src/__tests__/DesktopCalendarPage.test.tsx`. Replace each of these existing tests:

```tsx
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
```

with:

```tsx
test("renders the monthly timeline with one section per in-month day", async () => {
  render(wrap(<DesktopCalendarPage />));
  await screen.findByTestId("calendar-timeline");
  // May 2026 has 31 days.
  expect(screen.getAllByTestId("calendar-day-section")).toHaveLength(31);
  // Today's section reflects the AAPL event.
  const today = screen.getByText(/오늘 · 5월 11일 \(월\)/).closest('[data-testid="calendar-day-section"]')!;
  expect(within(today).getByText("AAPL earnings direct")).toBeInTheDocument();
});

test("clicking a grid cell sets selectedDate as scroll target (does not filter the feed away)", async () => {
  const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
  // Patch scrollIntoView so we can detect the scroll without jsdom failing.
  const scrollSpy = vi.fn();
  const originalScroll = Element.prototype.scrollIntoView;
  Element.prototype.scrollIntoView = scrollSpy;
  try {
    render(wrap(<DesktopCalendarPage />));
    await screen.findByTestId("calendar-timeline");

    await user.click(screen.getByTestId("month-grid-cell-2026-05-13"));

    // All sections still in the DOM (no filter-to-one-day collapse).
    expect(screen.getAllByTestId("calendar-day-section")).toHaveLength(31);
    // The May 13 section is now data-selected="true"; others are "false".
    const may13 = document.querySelector('[data-day-anchor="2026-05-13"]')!;
    const may11 = document.querySelector('[data-day-anchor="2026-05-11"]')!;
    expect(may13).toHaveAttribute("data-selected", "true");
    expect(may11).toHaveAttribute("data-selected", "false");
    // And we scrolled.
    expect(scrollSpy).toHaveBeenCalled();
    // May 13's cluster is still visible in its section.
    expect(within(may13 as HTMLElement).getByText("미국 실적 발표 327건")).toBeInTheDocument();
  } finally {
    Element.prototype.scrollIntoView = originalScroll;
  }
});

test("days with no matching events render the Toss-friendly empty placeholder, not the freshness banner", async () => {
  render(wrap(<DesktopCalendarPage />));
  await screen.findByTestId("calendar-timeline");
  const may12 = document.querySelector('[data-day-anchor="2026-05-12"]')!;
  expect(within(may12 as HTMLElement).getByText("이 날은 예정된 일정이 없어요")).toBeInTheDocument();
  expect(screen.queryByTestId("calendar-freshness-banner")).not.toBeInTheDocument();
});
```

Replace this existing test:

```tsx
test("renders calendar-error banner when fetchCalendar rejects", async () => {
  vi.spyOn(calApi, "fetchCalendar").mockRejectedValueOnce(new Error("network blew up"));
  render(wrap(<DesktopCalendarPage />));
  const banner = await screen.findByTestId("calendar-error");
  expect(banner).toHaveTextContent("network blew up");
  // Empty state must not render — error wins.
  expect(screen.queryByTestId("calendar-empty")).not.toBeInTheDocument();
});
```

with (only the timeline test-id changes; semantics identical):

```tsx
test("renders calendar-error banner when fetchCalendar rejects", async () => {
  vi.spyOn(calApi, "fetchCalendar").mockRejectedValueOnce(new Error("network blew up"));
  render(wrap(<DesktopCalendarPage />));
  const banner = await screen.findByTestId("calendar-error");
  expect(banner).toHaveTextContent("network blew up");
  // No timeline sections render when the request fails.
  expect(screen.queryByTestId("calendar-day-section")).not.toBeInTheDocument();
});
```

Replace this test block:

```tsx
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

with:

```tsx
test("type and region filters hide non-matching items from each day section, grid count stays accurate", async () => {
  const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
  render(wrap(<DesktopCalendarPage />));
  await screen.findByTestId("calendar-timeline");

  // Baseline: May 13 cluster present somewhere.
  expect(screen.getByText("미국 실적 발표 327건")).toBeInTheDocument();

  // Filter to 경제지표 — earnings cluster gone, day section still rendered with the empty placeholder.
  await user.click(screen.getByRole("button", { name: "경제지표" }));
  expect(screen.queryByText("미국 실적 발표 327건")).not.toBeInTheDocument();
  const may13 = document.querySelector('[data-day-anchor="2026-05-13"]')!;
  expect(within(may13 as HTMLElement).getByText("이 날은 예정된 일정이 없어요")).toBeInTheDocument();
  // Grid count badge for May 13 is gone.
  const cell = screen.getByTestId("month-grid-cell-2026-05-13");
  expect(within(cell).queryByText("327")).not.toBeInTheDocument();

  // Switch to 실적 — cluster reappears.
  await user.click(screen.getByRole("button", { name: "실적" }));
  expect(screen.getByText("미국 실적 발표 327건")).toBeInTheDocument();

  // 국내 region filter — empty (cluster is US).
  await user.click(screen.getByRole("button", { name: "국내" }));
  expect(screen.queryByText("미국 실적 발표 327건")).not.toBeInTheDocument();
  expect(within(may13 as HTMLElement).getByText("이 날은 예정된 일정이 없어요")).toBeInTheDocument();
});
```

Remove this test (selected-date label string is no longer rendered):

```tsx
test("today's selected-date label includes the 오늘 prefix", async () => {
  render(wrap(<DesktopCalendarPage />));
  // selectedDate defaults to today (2026-05-11 — Monday).
  expect(await screen.findByText(/오늘 · 5월 11일 월요일 일정/)).toBeInTheDocument();
});
```

Replace it with:

```tsx
test("today's day section is labelled with 오늘 prefix", async () => {
  render(wrap(<DesktopCalendarPage />));
  await screen.findByTestId("calendar-timeline");
  expect(screen.getByText(/오늘 · 5월 11일 \(월\)/)).toBeInTheDocument();
});

test("default surface renders the source button and never the legacy freshness banner", async () => {
  render(wrap(<DesktopCalendarPage />));
  await screen.findByTestId("calendar-timeline");
  expect(screen.getByTestId("calendar-source-button")).toBeInTheDocument();
  expect(screen.queryByTestId("calendar-freshness-banner")).not.toBeInTheDocument();
});
```

Update the loading test to assert against the timeline's loading slot rather than `selected-date-events`. Replace:

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
```

with (only the trailing assertion changes — the new component renders a `calendar-loading` testid as well):

```tsx
test("renders the calendar-loading skeleton while the first fetch is in flight, replaces with the timeline", async () => {
  let resolve: (v: typeof calendarFixture) => void;
  vi.spyOn(calApi, "fetchCalendar").mockImplementationOnce(
    () => new Promise((r) => { resolve = r; }),
  );
  render(wrap(<DesktopCalendarPage />));
  expect(await screen.findByTestId("calendar-loading")).toBeInTheDocument();
  resolve!(calendarFixture);
  await waitFor(() =>
    expect(screen.queryByTestId("calendar-loading")).not.toBeInTheDocument(),
  );
  expect(screen.getByTestId("calendar-timeline")).toBeInTheDocument();
});
```

- [ ] **Step 6.2: Run the desktop test file and verify the new/updated tests fail**

Run: `cd frontend/invest && npx vitest run src/__tests__/DesktopCalendarPage.test.tsx`
Expected: FAIL on the new "monthly timeline" / "scroll target" / "source button" tests (component not wired yet).

- [ ] **Step 6.3: Wire `DesktopCalendarPage.tsx`**

Edit `frontend/invest/src/pages/desktop/DesktopCalendarPage.tsx`:

1. Update the imports — drop `SelectedDateEvents` and `CalendarFreshnessBanner`, add `MonthlyEventsTimeline` and `CalendarSourceButton`, drop `selectedDateLabelWithRelative` and `monthLabel`:

```tsx
import { AIWeeklyCard } from "../../components/calendar/AIWeeklyCard";
import { CalendarMonthHeader } from "../../components/calendar/CalendarMonthHeader";
import { CalendarSourceButton } from "../../components/calendar/CalendarSourceButton";
import { EventDetailModal } from "../../components/calendar/EventDetailModal";
import { MonthCalendarGrid } from "../../components/calendar/MonthCalendarGrid";
import { MonthlyEventsTimeline } from "../../components/calendar/MonthlyEventsTimeline";
import {
  addMonths,
  fmtLocal,
  gridEndFromMonth,
  gridStartFromMonth,
  monthLabel,
  monthTitleLabel,
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

2. Delete the `const selectedDay: FilteredDay = filteredByDate.get(selectedDate) ?? …` block (no longer used).

3. Replace the existing `<Card style={{ padding: "16px 6px" }}>` block in the `center` slot:

```tsx
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
              {calendar?.meta?.sourceFreshness && (
                <div style={{ padding: "8px 8px 0" }}>
                  <CalendarFreshnessBanner sources={calendar.meta.sourceFreshness} />
                </div>
              )}
              <div style={{ padding: "12px 8px 4px" }}>
                <SelectedDateEvents
                  dateLabel={selectedDateLabelWithRelative(selectedDate, today)}
                  dateIso={selectedDate}
                  events={selectedDay.events}
                  clusters={selectedDay.clusters}
                  emptyMessage="선택한 날짜에 일정이 없습니다."
                  loading={calendarLoading}
                  error={calendarErr}
                />
              </div>
            </Card>
```

with:

```tsx
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
                <CalendarSourceButton sources={calendar?.meta?.sourceFreshness ?? []} />
              </div>
              <div style={{ padding: "12px 8px 4px" }}>
                <MonthlyEventsTimeline
                  monthCursor={monthCursor}
                  selectedDate={selectedDate}
                  todayIso={today}
                  filteredByDate={filteredByDate}
                  loading={calendarLoading}
                  error={calendarErr}
                />
              </div>
            </Card>
```

- [ ] **Step 6.4: Run the desktop test file and verify all pass**

Run: `cd frontend/invest && npx vitest run src/__tests__/DesktopCalendarPage.test.tsx`
Expected: PASS on every test.

- [ ] **Step 6.5: Commit**

```bash
git add frontend/invest/src/pages/desktop/DesktopCalendarPage.tsx \
        frontend/invest/src/__tests__/DesktopCalendarPage.test.tsx
git commit -m "$(cat <<'EOF'
feat(ROB-185): desktop calendar — grouped monthly timeline + source button

DesktopCalendarPage now renders MonthlyEventsTimeline as the centre-pane
feed. Month-grid click sets selectedDate as a scroll target, not a filter
— every in-month day stays visible. CalendarFreshnessBanner removed from
the default surface; CalendarSourceButton sits trailing the month label.

Co-Authored-By: Paperclip <noreply@paperclip.ing>
EOF
)"
```

---

## Task 7: Wire the timeline into `MobileCalendarPage`

**Files:**
- Modify: `frontend/invest/src/pages/mobile/MobileCalendarPage.tsx`
- Modify: `frontend/invest/src/__tests__/MobileCalendarPage.test.tsx`

- [ ] **Step 7.1: Update mobile tests**

Edit `frontend/invest/src/__tests__/MobileCalendarPage.test.tsx`. Replace this test:

```tsx
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
```

with:

```tsx
test("renders CalendarMonthHeader with the current month title and the monthly timeline", async () => {
  render(wrap(<MobileCalendarPage />));
  expect(await screen.findByText("2026년 5월")).toBeInTheDocument();
  expect(screen.getByTestId("calendar-prev-month")).toBeInTheDocument();
  expect(screen.getByTestId("calendar-next-month")).toBeInTheDocument();
  await screen.findByTestId("calendar-timeline");
  // 31 day sections for May 2026.
  expect(screen.getAllByTestId("calendar-day-section")).toHaveLength(31);
});
```

Replace this test:

```tsx
test("does NOT use UTC fmt — selected date uses fmtLocal even in non-UTC timezone", async () => {
  // 2026-05-11 in KST equals 2026-05-10 in UTC; fmtLocal must give 2026-05-11.
  render(wrap(<MobileCalendarPage />));
  const list = await screen.findByTestId("selected-date-events");
  expect(list).toHaveAttribute("data-selected-date", "2026-05-11");
});
```

with:

```tsx
test("does NOT use UTC fmt — today's section uses fmtLocal even in non-UTC timezone", async () => {
  render(wrap(<MobileCalendarPage />));
  await screen.findByTestId("calendar-timeline");
  // The 2026-05-11 section is the data-selected one, not 2026-05-10.
  const may11 = document.querySelector('[data-day-anchor="2026-05-11"]');
  expect(may11).not.toBeNull();
  expect(may11).toHaveAttribute("data-selected", "true");
});
```

Replace this test:

```tsx
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
```

with:

```tsx
test("touches an in-month date in the strip and scrolls/highlights that day section (does not filter)", async () => {
  const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
  const scrollSpy = vi.fn();
  const originalScroll = Element.prototype.scrollIntoView;
  Element.prototype.scrollIntoView = scrollSpy;
  try {
    render(wrap(<MobileCalendarPage />));
    await screen.findByTestId("calendar-timeline");

    await user.click(screen.getByTestId("day-2026-05-13"));

    // All 31 day sections still in the DOM (no filter-collapse).
    expect(screen.getAllByTestId("calendar-day-section")).toHaveLength(31);
    const may13 = document.querySelector('[data-day-anchor="2026-05-13"]')!;
    expect(may13).toHaveAttribute("data-selected", "true");
    expect(scrollSpy).toHaveBeenCalled();
    // The cluster still shows in the May 13 section.
    expect(within(may13 as HTMLElement).getByText("미국 실적 발표 327건")).toBeInTheDocument();
  } finally {
    Element.prototype.scrollIntoView = originalScroll;
  }
});
```

Replace this test:

```tsx
test("includes 오늘 prefix on the selected-date label when today is selected", async () => {
  render(wrap(<MobileCalendarPage />));
  expect(await screen.findByText(/오늘 · 5월 11일 월요일 일정/)).toBeInTheDocument();
});
```

with:

```tsx
test("today's day section is labelled with the 오늘 prefix", async () => {
  render(wrap(<MobileCalendarPage />));
  await screen.findByTestId("calendar-timeline");
  expect(screen.getByText(/오늘 · 5월 11일 \(월\)/)).toBeInTheDocument();
});

test("default surface renders the source button and never the legacy freshness banner", async () => {
  render(wrap(<MobileCalendarPage />));
  await screen.findByTestId("calendar-timeline");
  expect(screen.getByTestId("calendar-source-button")).toBeInTheDocument();
  expect(screen.queryByTestId("calendar-freshness-banner")).not.toBeInTheDocument();
});
```

Replace this test:

```tsx
test("error response surfaces the calendar-error banner, not the empty state", async () => {
  vi.spyOn(calApi, "fetchCalendar").mockRejectedValueOnce(new Error("boom"));
  render(wrap(<MobileCalendarPage />));
  expect(await screen.findByTestId("calendar-error")).toHaveTextContent("boom");
  expect(screen.queryByTestId("calendar-empty")).not.toBeInTheDocument();
});
```

with:

```tsx
test("error response surfaces the calendar-error banner, not day sections", async () => {
  vi.spyOn(calApi, "fetchCalendar").mockRejectedValueOnce(new Error("boom"));
  render(wrap(<MobileCalendarPage />));
  expect(await screen.findByTestId("calendar-error")).toHaveTextContent("boom");
  expect(screen.queryByTestId("calendar-day-section")).not.toBeInTheDocument();
});
```

- [ ] **Step 7.2: Run mobile test file and verify failures**

Run: `cd frontend/invest && npx vitest run src/__tests__/MobileCalendarPage.test.tsx`
Expected: FAIL on the new timeline/scroll/source tests.

- [ ] **Step 7.3: Wire `MobileCalendarPage.tsx`**

Edit `frontend/invest/src/pages/mobile/MobileCalendarPage.tsx`:

1. Replace the imports — drop `SelectedDateEvents`, `CalendarFreshnessBanner`, `selectedDateLabelWithRelative`; add `MonthlyEventsTimeline`, `CalendarSourceButton`:

```tsx
import { CalendarMonthHeader } from "../../components/calendar/CalendarMonthHeader";
import { CalendarSourceButton } from "../../components/calendar/CalendarSourceButton";
import { EventDetailModal } from "../../components/calendar/EventDetailModal";
import { MonthlyEventsTimeline } from "../../components/calendar/MonthlyEventsTimeline";
import { SparkleIcon } from "../../components/calendar/SparkleIcon";
import { WeekDateStrip } from "../../components/calendar/WeekDateStrip";
import {
  addMonths,
  clampSelectedDateToMonth,
  fmtLocal,
  gridEndFromMonth,
  gridStartFromMonth,
  monthTitleLabel,
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

2. Delete the `filteredSelected` memo (lines 139-149) and add a `filteredByDate` memo modeled on `DesktopCalendarPage`'s:

```tsx
interface FilteredDay {
  events: CalendarEventVM[];
  clusters: CalendarClusterVM[];
  total: number;
}

  // After the existing weekDays memo:
  const filteredByDate = useMemo<Map<string, FilteredDay>>(() => {
    const map = new Map<string, FilteredDay>();
    for (const d of calendar?.days ?? []) {
      const events = d.events
        .map((event) => toEventVM(event, d.date))
        .filter((event) => matches(event, typeFilter, regionFilter));
      const clusters = d.clusters
        .map((cluster) => toClusterVM(cluster, d.date))
        .filter((cluster) => matches(cluster, typeFilter, regionFilter));
      const total = events.length + clusters.reduce((s, c) => s + c.count, 0);
      if (total === 0) continue;
      map.set(d.date, { events, clusters, total });
    }
    return map;
  }, [calendar?.days, typeFilter, regionFilter]);
```

3. Replace the freshness banner + `SelectedDateEvents` block:

```tsx
          {calendar?.meta?.sourceFreshness && (
            <div style={{ padding: "0 12px 8px" }}>
              <CalendarFreshnessBanner sources={calendar.meta.sourceFreshness} />
            </div>
          )}
          <SelectedDateEvents
            dateLabel={selectedDateLabelWithRelative(selectedDate, today)}
            dateIso={selectedDate}
            events={filteredSelected.events}
            clusters={filteredSelected.clusters}
            emptyMessage="해당 날짜에는 일정이 없습니다."
            loading={calendarLoading}
            error={calendarErr}
          />
```

with:

```tsx
          <div style={{ display: "flex", justifyContent: "flex-end", padding: "0 8px" }}>
            <CalendarSourceButton sources={calendar?.meta?.sourceFreshness ?? []} />
          </div>
          <MonthlyEventsTimeline
            monthCursor={monthCursor}
            selectedDate={selectedDate}
            todayIso={today}
            filteredByDate={filteredByDate}
            loading={calendarLoading}
            error={calendarErr}
          />
```

- [ ] **Step 7.4: Run the mobile test file and verify all pass**

Run: `cd frontend/invest && npx vitest run src/__tests__/MobileCalendarPage.test.tsx`
Expected: PASS on every test.

- [ ] **Step 7.5: Run the full calendar test suite + lint + typecheck**

Run:

```bash
cd frontend/invest
npx vitest run src/__tests__/calendarTimelineVm.test.ts \
               src/__tests__/DaySection.test.tsx \
               src/__tests__/MonthlyEventsTimeline.test.tsx \
               src/__tests__/CalendarSourceButton.test.tsx \
               src/__tests__/EventRow.test.tsx \
               src/__tests__/ClusterRow.test.tsx \
               src/__tests__/MonthCalendarGrid.test.tsx \
               src/__tests__/DesktopCalendarPage.test.tsx \
               src/__tests__/MobileCalendarPage.test.tsx \
               src/__tests__/SelectedDateEvents.test.tsx \
               src/__tests__/calendarFreshnessVm.test.ts \
               src/__tests__/calendarMonthVm.test.ts \
               src/__tests__/calendarKstAndDateLabel.test.ts
npx tsc --noEmit
npx eslint src/components/calendar src/pages src/__tests__ --max-warnings 0
```

Expected: all vitest files PASS, `tsc` returns 0, `eslint` returns 0.

- [ ] **Step 7.6: Commit**

```bash
git add frontend/invest/src/pages/mobile/MobileCalendarPage.tsx \
        frontend/invest/src/__tests__/MobileCalendarPage.test.tsx
git commit -m "$(cat <<'EOF'
feat(ROB-185): mobile calendar — grouped monthly timeline + source button

MobileCalendarPage swaps SelectedDateEvents for MonthlyEventsTimeline. The
week strip click sets selectedDate as a scroll target rather than a
filter, matching desktop. CalendarFreshnessBanner removed; source button
sits above the timeline on the right edge.

Co-Authored-By: Paperclip <noreply@paperclip.ing>
EOF
)"
```

---

## Task 8: PR readiness — full smoke + checklist

**Files:** none — verification only.

- [ ] **Step 8.1: Run the entire frontend invest test suite**

Run: `cd frontend/invest && npx vitest run`
Expected: PASS on every test file. Investigate any regression — do not skip.

- [ ] **Step 8.2: Run the dev server and manually validate**

Run: `cd frontend/invest && npm run dev`

In a browser at `http://localhost:5173/invest/calendar`:

1. Default render: timeline shows every day of the current month. Today's section has the `오늘 · M월 D일 (요일)` header and is `data-selected="true"`.
2. Click a future day in the month grid (desktop) — page scrolls (smoothly) to that day's section, header gets the relative prefix if appropriate, all other days remain in the DOM.
3. Toggle the `경제지표` filter — earnings clusters disappear from each day; days with no remaining items still render with `이 날은 예정된 일정이 없어요`.
4. Click the `데이터 출처` button — quiet popover opens with one row per source, written as 미국 실적 일정 / 한국 공시 / 경제 지표; no `Finnhub` / `DART` / `ForexFactory` strings appear.
5. Resize the browser to 390×844 (or open in Mobile emulation) — confirm the timeline is still readable, the numeric columns are hidden (existing media query), the source button is reachable.
6. Disable JavaScript and reload — the page should still render the shell and degrade gracefully (existing behaviour).

Record any defect found here as a follow-up item — do not patch in this PR unless a test was missing for it.

- [ ] **Step 8.3: Confirm the ROB-182 §A2 string audit holds**

Run:

```bash
cd frontend/invest
npx vitest run --reporter=verbose src/__tests__/CalendarSourceButton.test.tsx
```

The 'banished operational strings' test in that file is the §A2 gate. If it fails, audit the rendered DOM for new leaks; this PR must keep the default surface free of `데이터 상태:`, `오래됨`, `수집 실패`, `미수집`, `Finnhub`, `DART`, `ForexFactory`.

- [ ] **Step 8.4: Open the PR**

Title: `feat(ROB-185): Toss-like grouped monthly event timeline for /invest/calendar`

Body (use the HEREDOC pattern in CLAUDE.md):

```
## Summary
- Replace single-selected-date panel with a month-grouped event timeline.
  Day-pick now scrolls/highlights, doesn't filter.
- Strip dashboard chrome: no em-dash placeholders, no `+N` cluster chip,
  no `+999` month-grid label, no `데이터 상태:` banner on the default
  surface. Source attribution moves into a quiet `데이터 출처` popover
  written in plain Korean.

## Test plan
- [x] `npx vitest run` green across the calendar test suite
- [x] `npx tsc --noEmit` clean
- [x] `npx eslint --max-warnings 0` clean on touched paths
- [x] Manual smoke at 1440×900 and 390×844: scroll-to-day, filters,
      source popover, today/tomorrow prefixes

## ROB-182 acceptance items satisfied
A1 (partial — popover only, full sheet/dot in ROB-186), A2, A5, A6, B1
(scroll target), B4 (no em-dash), C1 (partial — no `+N` chip), C2
(`많음`), D2 (timeline empty copy), D7 (partial).

## Deferred (will be picked up in follow-up tickets)
A3, A4, B2 polish, B3, B5, B6, C1 full, C3, C4, D1/D3/D5, E5, F.0–F.4.

🤖 Generated with Claude Code (Sonnet implementer per ROB-182).
```

---

## Self-review checklist (run mentally before declaring this plan ready)

**Spec coverage:** Walked through ROB-182 §A–§F:
- §A: A1 (yes — banner removed + quiet button), A2 (yes — popover test asserts banished strings), A3 (deferred — explicit), A4 (deferred — explicit), A5 (yes — `dayTotalLabel`), A6 (yes — empty copy).
- §B: B1 scroll-target (yes — Task 6/7 tests), B2 sticky-header (partial — CSS in Task 2 applies `position: sticky`; cross-section polish deferred), B3 (deferred — explicit), B4 (yes — em-dash dropped in Task 4), B5/B6 (deferred — explicit).
- §C: C1 (partial — `+N` chip removed, but full category-pill expansion deferred — explicit), C2 (yes — `많음` in Task 4), C3/C4 (deferred — explicit).
- §D: D2 (yes — `monthEmptyLabel`), D7 (partial — popover only, no unread dot yet — explicit).
- §E: not in scope — explicit.
- §F: not in scope — explicit. Tests stick with vitest until ROB-187 adds Playwright.

**Placeholder scan:** Every step contains the exact code, exact command, and exact expected outcome. No "TBD", no "implement similar", no naked "add tests".

**Type consistency:**
- `monthDaysIso(monthCursor: Date)` — same signature used in Task 1 helper + Task 3 component.
- `dayHeaderLabel(dateIso: string, todayIso: string)` — same in helper test + DaySection + MonthlyEventsTimeline (via DaySection).
- `MonthlyDay` shape — `{ events, clusters, total }` matches the existing `FilteredDay` interface in `DesktopCalendarPage.tsx`. Mobile gets the same shape added.
- `CalendarSourceButton({ sources })` — same prop shape on desktop wiring and mobile wiring.
- `sourceFriendlyLabel(source: string)` / `sourceStaleStatusCopy(status: CalendarSourceStatus)` — return-type/null contract matches every call site.

**Behaviour preserved where intentional:** `SelectedDateEvents.tsx` remains in the tree as an unmounted leaf component so its existing test file keeps passing without modification. ROB-186 will remove the file once its callers are gone.

**One thing the implementer should sanity-check on review:** the `scroll-margin-top: 76px` on `.calendar-day-section` is an educated guess for desktop sticky-filter overlap. After Step 8.2 manual smoke, adjust the value if the section header lands under the filter bar.
