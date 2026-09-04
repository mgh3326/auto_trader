import { afterEach, expect, test, vi } from "vitest";

import {
  buildInvestRumPayload,
  InvestRumReporter,
  normalizeInvestRumPath,
  postInvestRum,
} from "../api/investRum";

afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  document.cookie = "csrftoken=; expires=Thu, 01 Jan 1970 00:00:00 GMT; path=/";
});

test("RUM templates dynamic report IDs and API symbols before tagging", () => {
  expect(normalizeInvestRumPath("/invest/reports/550e8400-e29b-41d4-a716-446655440000")).toBe(
    "/invest/reports/:id",
  );
  expect(normalizeInvestRumPath("/invest/api/symbols/005930/quote")).toBe(
    "/invest/api/symbols/:symbol/quote",
  );
});

test("RUM groups invest API resources by route entry and keeps the slowest path", () => {
  const payload = buildInvestRumPayload("/invest", 100, [
    { name: "http://localhost/invest/api/home", startTime: 120, responseEnd: 210 },
    { name: "http://localhost/invest/api/market", startTime: 150, responseEnd: 360 },
    { name: "http://localhost/static/app.js", startTime: 130, responseEnd: 400 },
  ]);

  expect(payload).toEqual({
    route: "/invest",
    n_requests: 2,
    wall_ms: 260,
    slowest: "/invest/api/market",
  });
});

test("RUM POST uses the existing session and CSRF mutation pattern", () => {
  document.cookie = "csrftoken=csrf-fixture; path=/";
  const fetchMock = vi.fn().mockResolvedValue({ ok: true });
  vi.stubGlobal("fetch", fetchMock);

  postInvestRum({
    route: "/invest",
    n_requests: 1,
    wall_ms: 24,
    slowest: "/invest/api/home",
  });

  expect(fetchMock).toHaveBeenCalledWith(
    "/invest/api/rum",
    expect.objectContaining({
      method: "POST",
      credentials: "include",
      headers: {
        "Content-Type": "application/json",
        "X-CSRFToken": "csrf-fixture",
      },
    }),
  );
  const request = fetchMock.mock.calls[0]![1] as RequestInit;
  expect(JSON.parse(String(request.body))).toMatchObject({ n_requests: 1 });
});

test("RUM waits for the final observed fan-out response before one flush", () => {
  vi.useFakeTimers();
  const fetchMock = vi.fn().mockResolvedValue({ ok: true });
  vi.stubGlobal("fetch", fetchMock);
  vi.spyOn(performance, "now").mockReturnValue(0);

  class FakePerformanceObserver {
    static current: FakePerformanceObserver | null = null;

    constructor(
      private readonly callback: (list: { getEntries(): PerformanceEntry[] }) => void,
    ) {
      FakePerformanceObserver.current = this;
    }

    observe(): void {}
    disconnect(): void {}

    emit(entries: PerformanceEntry[]): void {
      this.callback({ getEntries: () => entries });
    }
  }

  vi.stubGlobal("PerformanceObserver", FakePerformanceObserver);
  const reporter = new InvestRumReporter();
  reporter.begin("/invest/reports/550e8400-e29b-41d4-a716-446655440000");

  // A mutant that starts the idle timer in begin() posts an empty sample here.
  vi.advanceTimersByTime(750);
  expect(fetchMock).not.toHaveBeenCalled();

  FakePerformanceObserver.current!.emit([
    {
      name: "http://localhost/invest/api/home",
      startTime: 0,
      responseEnd: 750,
    } as unknown as PerformanceEntry,
  ]);
  vi.advanceTimersByTime(250);
  FakePerformanceObserver.current!.emit([
    {
      name: "http://localhost/invest/api/symbols/005930/quote",
      startTime: 0,
      responseEnd: 1000,
    } as unknown as PerformanceEntry,
  ]);

  vi.advanceTimersByTime(8999);
  expect(fetchMock).not.toHaveBeenCalled();
  vi.advanceTimersByTime(1);

  expect(fetchMock).toHaveBeenCalledOnce();
  const request = fetchMock.mock.calls[0]![1] as RequestInit;
  expect(JSON.parse(String(request.body))).toMatchObject({
    route: "/invest/reports/:id",
    n_requests: 2,
    wall_ms: 1000,
    slowest: "/invest/api/symbols/:symbol/quote",
  });
});
