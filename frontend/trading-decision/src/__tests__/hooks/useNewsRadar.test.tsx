// frontend/trading-decision/src/__tests__/hooks/useNewsRadar.test.tsx
import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { useNewsRadar } from "../../hooks/useNewsRadar";
import { makeNewsRadarResponse } from "../../test/fixtures/newsRadar";
import { mockFetch } from "../../test/server";

describe("useNewsRadar", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("loads the radar with default filters and exposes data", async () => {
    mockFetch({
      "/trading/api/news-radar?market=all&hours=24&include_excluded=true&limit=50":
        () => new Response(JSON.stringify(makeNewsRadarResponse())),
    });

    const { result } = renderHook(() => useNewsRadar());

    await waitFor(() => {
      expect(result.current.status).toBe("success");
    });
    expect(result.current.data?.summary.total_count).toBe(1);
  });

  it("refetches when filters change", async () => {
    const { calls } = mockFetch({
      "/trading/api/news-radar?market=all&hours=24&include_excluded=true&limit=50":
        () => new Response(JSON.stringify(makeNewsRadarResponse())),
      "/trading/api/news-radar?market=us&hours=6&include_excluded=true&limit=50":
        () =>
          new Response(JSON.stringify(makeNewsRadarResponse({ market: "us" }))),
    });

    const { result } = renderHook(() => useNewsRadar());

    await waitFor(() => {
      expect(result.current.status).toBe("success");
    });
    act(() => {
      result.current.setFilters((prev) => ({ ...prev, market: "us", hours: 6 }));
    });
    await waitFor(() => {
      expect(result.current.data?.market).toBe("us");
    });
    const requestedPaths = calls.map((c) => new URL(c.url, "https://x.test").search);
    expect(requestedPaths.some((s) => s.includes("market=us"))).toBe(true);
  });

  it("surfaces error state when fetch fails", async () => {
    mockFetch({
      "/trading/api/news-radar?market=all&hours=24&include_excluded=true&limit=50":
        () => new Response("boom", { status: 500 }),
    });

    const { result } = renderHook(() => useNewsRadar());
    await waitFor(() => {
      expect(result.current.status).toBe("error");
    });
    expect(result.current.error).toMatch(/500|boom/i);
  });
});
