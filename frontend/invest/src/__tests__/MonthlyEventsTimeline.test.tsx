import { render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";
import { MonthlyEventsTimeline } from "../components/calendar/MonthlyEventsTimeline";
import type { CalendarClusterVM, CalendarEventVM } from "../components/calendar/vm";

// --- IntersectionObserver shim ------------------------------------------
//
// jsdom does not implement IntersectionObserver. Tests that need viewport
// callbacks install this shim, which records each observed element and lets
// the test fire synthetic entries via `triggerIntersection`. The shim is
// reset between tests so cross-test bleed is impossible.

interface FakeObserver {
  callback: IntersectionObserverCallback;
  observed: Set<Element>;
  options: IntersectionObserverInit | undefined;
}

const fakeObservers: FakeObserver[] = [];

function installIntersectionObserverShim() {
  class MockIntersectionObserver implements IntersectionObserver {
    readonly root: Element | Document | null = null;
    readonly rootMargin: string = "";
    readonly scrollMargin: string = "";
    readonly thresholds: ReadonlyArray<number> = [];
    private readonly _observer: FakeObserver;
    constructor(
      callback: IntersectionObserverCallback,
      options?: IntersectionObserverInit,
    ) {
      this._observer = { callback, observed: new Set(), options };
      fakeObservers.push(this._observer);
    }
    observe(target: Element): void {
      this._observer.observed.add(target);
    }
    unobserve(target: Element): void {
      this._observer.observed.delete(target);
    }
    disconnect(): void {
      this._observer.observed.clear();
    }
    takeRecords(): IntersectionObserverEntry[] {
      return [];
    }
  }
  (globalThis as unknown as { IntersectionObserver: typeof IntersectionObserver }).IntersectionObserver =
    MockIntersectionObserver as unknown as typeof IntersectionObserver;
}

function triggerIntersection(visibleIsos: string[]): void {
  for (const obs of fakeObservers) {
    const entries: IntersectionObserverEntry[] = [];
    for (const target of obs.observed) {
      const iso = (target as HTMLElement).getAttribute("data-day-anchor");
      const isIntersecting = iso != null && visibleIsos.includes(iso);
      entries.push({
        target,
        isIntersecting,
        intersectionRatio: isIntersecting ? 1 : 0,
        boundingClientRect: target.getBoundingClientRect(),
        intersectionRect:
          isIntersecting
            ? target.getBoundingClientRect()
            : ({} as DOMRectReadOnly),
        rootBounds: null,
        time: 0,
      });
    }
    obs.callback(entries, {} as IntersectionObserver);
  }
}

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
    const may11 = document.querySelector('[data-day-anchor="2026-05-11"]')!;
    expect(within(may11 as HTMLElement).getByText("AAPL")).toBeInTheDocument();
    // May 13 cluster present somewhere in the timeline. ROB-186: a cluster with no
    // topEvents renders as the ClusterOverflow row ("{base} · 그 외 {count}건"), not
    // the aggregate title verbatim.
    expect(screen.getByText("미국 실적 발표 · 그 외 4건")).toBeInTheDocument();
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
      // First-mount scroll already fired; reset before the user-driven navigation.
      spy.mockClear();
      rerender(<MonthlyEventsTimeline {...baseProps} selectedDate="2026-05-20" />);
      expect(spy).toHaveBeenCalledTimes(1);
      // User-initiated navigation should animate.
      expect(spy).toHaveBeenCalledWith(expect.objectContaining({ behavior: "smooth" }));
    } finally {
      Element.prototype.scrollIntoView = original;
    }
  });

  test("scrolls to selectedDate on first effective render (ROB-272)", () => {
    const spy = vi.fn();
    const original = Element.prototype.scrollIntoView;
    Element.prototype.scrollIntoView = spy;
    try {
      render(<MonthlyEventsTimeline {...baseProps} selectedDate="2026-05-19" />);
      expect(spy).toHaveBeenCalledTimes(1);
      // First-mount scroll should be instant so the page-load doesn't visibly drift.
      expect(spy).toHaveBeenCalledWith(expect.objectContaining({ behavior: "auto", block: "start" }));
    } finally {
      Element.prototype.scrollIntoView = original;
    }
  });

  test("does not scroll while loading; fires once when loading flips to false", () => {
    const spy = vi.fn();
    const original = Element.prototype.scrollIntoView;
    Element.prototype.scrollIntoView = spy;
    try {
      const { rerender } = render(<MonthlyEventsTimeline {...baseProps} loading />);
      expect(spy).not.toHaveBeenCalled();
      rerender(<MonthlyEventsTimeline {...baseProps} />);
      expect(spy).toHaveBeenCalledTimes(1);
      expect(spy).toHaveBeenCalledWith(expect.objectContaining({ behavior: "auto" }));
    } finally {
      Element.prototype.scrollIntoView = original;
    }
  });

  test("does not scroll when an error is shown", () => {
    const spy = vi.fn();
    const original = Element.prototype.scrollIntoView;
    Element.prototype.scrollIntoView = spy;
    try {
      render(<MonthlyEventsTimeline {...baseProps} error="boom" />);
      expect(spy).not.toHaveBeenCalled();
    } finally {
      Element.prototype.scrollIntoView = original;
    }
  });
});

// --- ROB-272 Phase 2 step D: viewport observer --------------------------

describe("MonthlyEventsTimeline viewport observer (ROB-272 Phase 2)", () => {
  let originalIO: typeof IntersectionObserver | undefined;
  beforeEach(() => {
    originalIO = (globalThis as unknown as { IntersectionObserver?: typeof IntersectionObserver })
      .IntersectionObserver;
    fakeObservers.length = 0;
    installIntersectionObserverShim();
  });
  afterEach(() => {
    fakeObservers.length = 0;
    if (originalIO) {
      (globalThis as unknown as { IntersectionObserver: typeof IntersectionObserver }).IntersectionObserver =
        originalIO;
    } else {
      delete (globalThis as unknown as { IntersectionObserver?: typeof IntersectionObserver })
        .IntersectionObserver;
    }
  });

  test("observes every day section when loading=false and error=null", () => {
    render(
      <MonthlyEventsTimeline
        {...baseProps}
        onVisibleDaysChange={() => {}}
      />,
    );
    // One observer, 31 May 2026 day sections observed.
    expect(fakeObservers).toHaveLength(1);
    expect(fakeObservers[0]!.observed.size).toBe(31);
  });

  test("does NOT observe while loading", () => {
    render(<MonthlyEventsTimeline {...baseProps} loading onVisibleDaysChange={() => {}} />);
    // Either no observer constructed at all, or no targets observed.
    expect(fakeObservers.every((o) => o.observed.size === 0)).toBe(true);
  });

  test("does NOT observe while error is shown", () => {
    render(
      <MonthlyEventsTimeline {...baseProps} error="boom" onVisibleDaysChange={() => {}} />,
    );
    expect(fakeObservers.every((o) => o.observed.size === 0)).toBe(true);
  });

  test("fires onVisibleDaysChange with the set of currently-visible day isos (sorted)", () => {
    const onVisible = vi.fn<(isos: string[]) => void>();
    render(<MonthlyEventsTimeline {...baseProps} onVisibleDaysChange={onVisible} />);
    triggerIntersection(["2026-05-12", "2026-05-10", "2026-05-11"]);
    expect(onVisible).toHaveBeenCalled();
    const last = onVisible.mock.calls.at(-1)![0];
    expect(last).toEqual(["2026-05-10", "2026-05-11", "2026-05-12"]);
  });

  test("does not call onVisibleDaysChange if no sections are visible", () => {
    const onVisible = vi.fn<(isos: string[]) => void>();
    render(<MonthlyEventsTimeline {...baseProps} onVisibleDaysChange={onVisible} />);
    triggerIntersection([]);
    // An "all-empty" notification is treated as a no-op: no point waking the
    // parent up to dispatch an empty ensureRange.
    expect(onVisible).not.toHaveBeenCalled();
  });
});
