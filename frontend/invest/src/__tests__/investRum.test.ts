import { afterEach, expect, test, vi } from "vitest";

import { buildInvestRumPayload, postInvestRum } from "../api/investRum";

afterEach(() => {
  vi.unstubAllGlobals();
  document.cookie = "csrftoken=; expires=Thu, 01 Jan 1970 00:00:00 GMT; path=/";
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
