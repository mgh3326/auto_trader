import { act, render } from "@testing-library/react";
import { describe, expect, test } from "vitest";
import {
  useCalendarDayCache,
  type CalendarFetchFn,
  type UseCalendarDayCacheResult,
} from "../components/calendar/useCalendarDayCache";
import type { CalendarResponse } from "../types/calendar";
import { dayDisplayState } from "../components/calendar/dayCache";

interface CapturedCall {
  fromDate: string;
  toDate: string;
}

function makeFetch(): {
  fn: CalendarFetchFn;
  calls: CapturedCall[];
  pending: Array<{
    call: CapturedCall;
    resolve: (resp: CalendarResponse) => void;
    reject: (err: Error) => void;
  }>;
} {
  const calls: CapturedCall[] = [];
  const pending: Array<{
    call: CapturedCall;
    resolve: (resp: CalendarResponse) => void;
    reject: (err: Error) => void;
  }> = [];
  const fn: CalendarFetchFn = (params) => {
    const call = { fromDate: params.fromDate, toDate: params.toDate };
    calls.push(call);
    return new Promise<CalendarResponse>((resolve, reject) => {
      pending.push({ call, resolve, reject });
    });
  };
  return { fn, calls, pending };
}

function calendarResponse(
  fromDate: string,
  toDate: string,
  byDateCounts: Record<string, number> = {},
): CalendarResponse {
  const start = new Date(`${fromDate}T00:00:00`);
  const end = new Date(`${toDate}T00:00:00`);
  const days = [];
  const cur = new Date(start);
  while (cur.getTime() <= end.getTime()) {
    const y = cur.getFullYear();
    const m = String(cur.getMonth() + 1).padStart(2, "0");
    const d = String(cur.getDate()).padStart(2, "0");
    const iso = `${y}-${m}-${d}`;
    const count = byDateCounts[iso] ?? 0;
    const events = Array.from({ length: count }, (_, i) => ({
      eventId: `${iso}-e${i}`,
      title: `evt ${i}`,
      market: "us" as const,
      eventType: "earnings" as const,
      eventTimeLocal: null,
      source: "test",
      country: null,
      currency: null,
      importance: null,
      impactTags: [],
      actual: null,
      forecast: null,
      previous: null,
      relatedSymbols: [],
      relation: "none" as const,
      badges: [],
    }));
    days.push({
      date: iso,
      events,
      clusters: [],
      dataState: "loaded" as const,
      summary: null,
    });
    cur.setDate(cur.getDate() + 1);
  }
  return {
    tab: "all",
    fromDate,
    toDate,
    asOf: new Date().toISOString(),
    days,
    meta: { warnings: [], sourceFreshness: [], coverage: null },
  };
}

interface ProbeProps {
  monthCursor: Date;
  selectedDate: string;
  initialChunkRadius?: number;
  fetchFn: CalendarFetchFn;
}

function makeHarness() {
  const captured = {
    last: undefined as UseCalendarDayCacheResult | undefined,
  };
  function Probe(props: ProbeProps) {
    captured.last = useCalendarDayCache({
      monthCursor: props.monthCursor,
      selectedDate: props.selectedDate,
      initialChunkRadius: props.initialChunkRadius,
      fetchFn: props.fetchFn,
    });
    return null;
  }
  return { captured, Probe };
}

describe("useCalendarDayCache — ROB-272 Phase 2 step B", () => {
  test("on mount, fetches the ±radius window around selectedDate", async () => {
    const { fn, calls, pending } = makeFetch();
    const { Probe } = makeHarness();
    render(
      <Probe
        monthCursor={new Date(2026, 4, 1)}
        selectedDate="2026-05-19"
        initialChunkRadius={3}
        fetchFn={fn}
      />,
    );
    expect(calls).toHaveLength(1);
    expect(calls[0]).toEqual({ fromDate: "2026-05-16", toDate: "2026-05-22" });
    await act(async () => {
      pending[0]!.resolve(calendarResponse("2026-05-16", "2026-05-22"));
    });
  });

  test("ensureRange dedups against in-flight days (no duplicate fetch)", async () => {
    const { fn, calls, pending } = makeFetch();
    const { captured, Probe } = makeHarness();
    render(
      <Probe
        monthCursor={new Date(2026, 4, 1)}
        selectedDate="2026-05-19"
        initialChunkRadius={3}
        fetchFn={fn}
      />,
    );
    act(() => {
      captured.last!.ensureRange("2026-05-18", "2026-05-20");
    });
    expect(calls).toHaveLength(1);
    await act(async () => {
      pending[0]!.resolve(
        calendarResponse("2026-05-16", "2026-05-22", { "2026-05-19": 2 }),
      );
    });
    act(() => {
      captured.last!.ensureRange("2026-05-17", "2026-05-21");
    });
    expect(calls).toHaveLength(1);
  });

  test("ensureRange fetches days outside the initial window", async () => {
    const { fn, calls, pending } = makeFetch();
    const { captured, Probe } = makeHarness();
    render(
      <Probe
        monthCursor={new Date(2026, 4, 1)}
        selectedDate="2026-05-19"
        initialChunkRadius={3}
        fetchFn={fn}
      />,
    );
    await act(async () => {
      pending[0]!.resolve(calendarResponse("2026-05-16", "2026-05-22"));
    });
    act(() => {
      captured.last!.ensureRange("2026-05-25", "2026-05-27");
    });
    expect(calls).toHaveLength(2);
    expect(calls[1]).toEqual({ fromDate: "2026-05-25", toDate: "2026-05-27" });
    await act(async () => {
      pending[1]!.resolve(calendarResponse("2026-05-25", "2026-05-27"));
    });
  });

  test("monthCursor change bumps epoch — late response from prior month is dropped", async () => {
    const { fn, pending } = makeFetch();
    const { captured, Probe } = makeHarness();
    const { rerender } = render(
      <Probe
        monthCursor={new Date(2026, 4, 1)}
        selectedDate="2026-05-19"
        initialChunkRadius={3}
        fetchFn={fn}
      />,
    );
    // Switch month before the prior-month fetch resolves.
    rerender(
      <Probe
        monthCursor={new Date(2026, 5, 1)}
        selectedDate="2026-06-15"
        initialChunkRadius={3}
        fetchFn={fn}
      />,
    );
    // Resolve the stale call with a non-zero count — would be a regression if
    // it landed.
    await act(async () => {
      pending[0]!.resolve(
        calendarResponse("2026-05-16", "2026-05-22", { "2026-05-19": 99 }),
      );
    });
    expect(dayDisplayState(captured.last!.cache, "2026-05-19")).toBe(
      "unloaded",
    );
  });

  test("fetch failure marks days as error", async () => {
    const { fn, pending } = makeFetch();
    const { captured, Probe } = makeHarness();
    render(
      <Probe
        monthCursor={new Date(2026, 4, 1)}
        selectedDate="2026-05-19"
        initialChunkRadius={3}
        fetchFn={fn}
      />,
    );
    await act(async () => {
      pending[0]!.reject(new Error("boom"));
    });
    expect(dayDisplayState(captured.last!.cache, "2026-05-19")).toBe("error");
  });
});
