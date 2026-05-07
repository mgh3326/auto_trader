import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";
import { fetchDiscoverCalendar } from "../api/marketEvents";

const fetchMock = vi.fn();

beforeEach(() => {
  fetchMock.mockReset();
  vi.stubGlobal("fetch", fetchMock);
});
afterEach(() => {
  vi.unstubAllGlobals();
});

describe("fetchDiscoverCalendar", () => {
  test("encodes from_date/to_date/today/tab", async () => {
    fetchMock.mockResolvedValueOnce({
      ok: true,
      json: async () => ({
        headline: null,
        week_label: "5월 1주차",
        from_date: "2026-05-04",
        to_date: "2026-05-10",
        today: "2026-05-07",
        tab: "all",
        days: [],
      }),
    });
    const data = await fetchDiscoverCalendar({
      fromDate: "2026-05-04",
      toDate: "2026-05-10",
      today: "2026-05-07",
      tab: "all",
    });
    expect(data.week_label).toBe("5월 1주차");
    const url = fetchMock.mock.calls[0][0] as string;
    expect(url).toContain("from_date=2026-05-04");
    expect(url).toContain("to_date=2026-05-10");
    expect(url).toContain("today=2026-05-07");
    expect(url).toContain("tab=all");
  });

  test("rejects on non-ok response", async () => {
    fetchMock.mockResolvedValueOnce({ ok: false, status: 500, json: async () => ({}) });
    await expect(
      fetchDiscoverCalendar({ fromDate: "2026-05-04", toDate: "2026-05-10" }),
    ).rejects.toThrow(/500/);
  });
});
