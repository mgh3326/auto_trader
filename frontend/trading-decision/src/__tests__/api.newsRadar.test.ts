// frontend/trading-decision/src/__tests__/api.newsRadar.test.ts
import { afterEach, describe, expect, it, vi } from "vitest";
import { getNewsRadar } from "../api/newsRadar";
import { makeNewsRadarResponse } from "../test/fixtures/newsRadar";
import { mockFetch } from "../test/server";

describe("news radar API client", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("builds the default GET path with all default filters", async () => {
    const { calls } = mockFetch({
      "/trading/api/news-radar?market=all&hours=24&include_excluded=true&limit=50":
        () => new Response(JSON.stringify(makeNewsRadarResponse())),
    });

    await getNewsRadar({
      market: "all",
      hours: 24,
      q: "",
      riskCategory: "",
      includeExcluded: true,
      limit: 50,
    });

    expect(calls[0]?.url).toContain("/trading/api/news-radar");
    expect(calls[0]?.method).toBe("GET");
  });

  it("encodes q and risk_category when provided", async () => {
    const { calls } = mockFetch({
      "/trading/api/news-radar?market=us&hours=6&q=Iran&risk_category=geopolitical_oil&include_excluded=false&limit=20":
        () => new Response(JSON.stringify(makeNewsRadarResponse({ market: "us" }))),
    });

    await getNewsRadar({
      market: "us",
      hours: 6,
      q: "Iran",
      riskCategory: "geopolitical_oil",
      includeExcluded: false,
      limit: 20,
    });

    expect(calls[0]?.url).toMatch(/q=Iran/);
    expect(calls[0]?.url).toMatch(/risk_category=geopolitical_oil/);
    expect(calls[0]?.url).toMatch(/include_excluded=false/);
  });

  it("returns the parsed response", async () => {
    mockFetch({
      "/trading/api/news-radar?market=all&hours=24&include_excluded=true&limit=50":
        () => new Response(JSON.stringify(makeNewsRadarResponse())),
    });

    const data = await getNewsRadar({
      market: "all",
      hours: 24,
      q: "",
      riskCategory: "",
      includeExcluded: true,
      limit: 50,
    });

    expect(data.summary.high_risk_count).toBe(1);
    expect(data.sections[0]?.section_id).toBe("geopolitical_oil");
  });
});
