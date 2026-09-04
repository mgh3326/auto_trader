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

test("RUM flushes 250ms after the final completed fan-out response", async () => {
  vi.useFakeTimers();
  let resolveFirst: ((response: Response) => void) | undefined;
  let resolveSecond: ((response: Response) => void) | undefined;
  const first = new Promise<Response>((resolve) => { resolveFirst = resolve; });
  const second = new Promise<Response>((resolve) => { resolveSecond = resolve; });
  const fetchMock = vi.fn()
    .mockReturnValueOnce(first)
    .mockReturnValueOnce(second)
    .mockResolvedValue({ ok: true });
  vi.stubGlobal("fetch", fetchMock);
  vi.spyOn(performance, "now").mockReturnValue(0);
  vi.stubGlobal("PerformanceObserver", FakePerformanceObserver);
  const reporter = new InvestRumReporter();
  reporter.begin("/invest/reports/550e8400-e29b-41d4-a716-446655440000");
  void fetch("/invest/api/home");
  void fetch("/invest/api/symbols/005930/quote");

  // A mutant that starts the idle timer in begin() posts an empty sample here.
  vi.advanceTimersByTime(750);
  expect(fetchMock).toHaveBeenCalledTimes(2);

  resolveFirst!({ ok: true } as Response);
  await Promise.resolve();
  await Promise.resolve();
  FakePerformanceObserver.current!.emit([
    {
      name: "http://localhost/invest/api/home",
      startTime: 0,
      responseEnd: 750,
    } as unknown as PerformanceEntry,
  ]);
  vi.advanceTimersByTime(250);
  resolveSecond!({ ok: true } as Response);
  await Promise.resolve();
  await Promise.resolve();
  FakePerformanceObserver.current!.emit([
    {
      name: "http://localhost/invest/api/symbols/005930/quote",
      startTime: 0,
      responseEnd: 1000,
    } as unknown as PerformanceEntry,
  ]);

  vi.advanceTimersByTime(249);
  expect(fetchMock).toHaveBeenCalledTimes(2);
  vi.advanceTimersByTime(1);

  expect(fetchMock).toHaveBeenCalledTimes(3);
  const request = fetchMock.mock.calls[2]![1] as RequestInit;
  expect(JSON.parse(String(request.body))).toMatchObject({
    route: "/invest/reports/:id",
    n_requests: 2,
    wall_ms: 1000,
    slowest: "/invest/api/symbols/:symbol/quote",
  });
});

test("RUM marks a 10-second cap sample as truncated when a fetch is pending", async () => {
  vi.useFakeTimers();
  let resolveFast: ((response: Response) => void) | undefined;
  const fast = new Promise<Response>((resolve) => { resolveFast = resolve; });
  const slow = new Promise<Response>(() => undefined);
  const fetchMock = vi.fn()
    .mockReturnValueOnce(fast)
    .mockReturnValueOnce(slow)
    .mockResolvedValue({ ok: true });
  vi.stubGlobal("fetch", fetchMock);
  vi.spyOn(performance, "now").mockReturnValue(0);
  vi.stubGlobal("PerformanceObserver", FakePerformanceObserver);
  const reporter = new InvestRumReporter();
  reporter.begin("/invest");

  void fetch("/invest/api/home");
  void fetch("/invest/api/symbols/005930/quote");
  resolveFast!({ ok: true } as Response);
  await Promise.resolve();
  await Promise.resolve();
  FakePerformanceObserver.current!.emit([
    {
      name: "http://localhost/invest/api/home",
      startTime: 0,
      responseEnd: 1000,
    } as unknown as PerformanceEntry,
  ]);

  vi.advanceTimersByTime(10_000);

  expect(fetchMock).toHaveBeenCalledTimes(3);
  const request = fetchMock.mock.calls[2]![1] as RequestInit;
  expect(JSON.parse(String(request.body))).toMatchObject({
    n_requests: 1,
    pending_requests: 1,
    truncated: true,
  });
});

test("RUM sends one truncated beacon on pagehide and never retries it", async () => {
  vi.useFakeTimers();
  document.cookie = "csrftoken=csrf-fixture; path=/";
  const sendBeacon = vi.fn().mockReturnValue(true);
  Object.defineProperty(navigator, "sendBeacon", { configurable: true, value: sendBeacon });
  vi.stubGlobal("PerformanceObserver", FakePerformanceObserver);
  vi.spyOn(performance, "now").mockReturnValue(0);
  const reporter = new InvestRumReporter();
  reporter.begin("/invest");

  FakePerformanceObserver.current!.emit([
    {
      name: "http://localhost/invest/api/home",
      startTime: 0,
      responseEnd: 1000,
    } as unknown as PerformanceEntry,
  ]);
  window.dispatchEvent(new Event("pagehide"));

  expect(sendBeacon).toHaveBeenCalledOnce();
  const blob = sendBeacon.mock.calls[0]![1] as Blob;
  expect(sendBeacon.mock.calls[0]![0]).toBe("/invest/api/rum");
  expect(blob.type).toBe("application/json");
  expect(JSON.parse(await blob.text())).toMatchObject({
    truncated: true,
    pending_requests: 0,
    csrf_token: "csrf-fixture",
  });
  vi.advanceTimersByTime(10_000);
  expect(sendBeacon).toHaveBeenCalledOnce();
});
