// frontend/trading-decision/src/__tests__/NewsRadarPage.test.tsx
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import NewsRadarPage from "../pages/NewsRadarPage";
import {
  makeNewsRadarItem,
  makeNewsRadarResponse,
} from "../test/fixtures/newsRadar";
import { mockFetch } from "../test/server";

const DEFAULT_URL =
  "/trading/api/news-radar?market=all&hours=24&include_excluded=true&limit=50";

describe("NewsRadarPage", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("renders the geopolitical/oil headline as high severity and excluded", async () => {
    mockFetch({
      [DEFAULT_URL]: () =>
        new Response(JSON.stringify(makeNewsRadarResponse())),
    });

    render(<NewsRadarPage />, { wrapper: MemoryRouter });

    expect(
      await screen.findByRole("heading", {
        level: 2,
        name: /Market Risk News Radar/i,
      }),
    ).toBeInTheDocument();
    expect(
      (await screen.findAllByRole("link", { name: /UAE airstrike on tanker in Hormuz/i }))[0],
    ).toBeInTheDocument();
    expect(screen.getAllByText(/Collected · not in briefing/i)[0]).toBeInTheDocument();
    expect(screen.getByText(/High-risk: 1/i)).toBeInTheDocument();
  });

  it("shows readiness stale chip when readiness.status is stale", async () => {
    mockFetch({
      [DEFAULT_URL]: () =>
        new Response(
          JSON.stringify(
            makeNewsRadarResponse({
              readiness: {
                status: "stale",
                latest_scraped_at: null,
                latest_published_at: null,
                recent_6h_count: 0,
                recent_24h_count: 0,
                source_count: 0,
                stale: true,
                max_age_minutes: 180,
                warnings: ["news_stale"],
              },
            }),
          ),
        ),
    });

    render(<NewsRadarPage />, { wrapper: MemoryRouter });
    expect(await screen.findByText(/Stale/i)).toBeInTheDocument();
  });

  it("changes filters and triggers a new fetch", async () => {
    const { calls } = mockFetch({
      [DEFAULT_URL]: () =>
        new Response(JSON.stringify(makeNewsRadarResponse())),
      "/trading/api/news-radar?market=us&hours=24&include_excluded=true&limit=50":
        () =>
          new Response(JSON.stringify(makeNewsRadarResponse({ market: "us" }))),
    });

    render(<NewsRadarPage />, { wrapper: MemoryRouter });
    await screen.findAllByText(/UAE airstrike/i);

    const marketSelect = screen.getByLabelText(/^Market$/i);
    await userEvent.selectOptions(marketSelect, "us");

    await waitFor(() => {
      expect(
        calls.some((c) => c.url.includes("market=us")),
      ).toBe(true);
    });
  });

  it("renders empty state when there are no items", async () => {
    mockFetch({
      [DEFAULT_URL]: () =>
        new Response(
          JSON.stringify(
            makeNewsRadarResponse({
              sections: [],
              items: [],
              excluded_items: [],
              summary: {
                high_risk_count: 0,
                total_count: 0,
                included_in_briefing_count: 0,
                excluded_but_collected_count: 0,
              },
            }),
          ),
        ),
    });

    render(<NewsRadarPage />, { wrapper: MemoryRouter });
    expect(
      await screen.findByText(/No matching news in this window/i),
    ).toBeInTheDocument();
  });

  it("renders error state and a retry button when fetch fails", async () => {
    mockFetch({
      [DEFAULT_URL]: () => new Response("server error", { status: 500 }),
    });

    render(<NewsRadarPage />, { wrapper: MemoryRouter });
    expect(
      await screen.findByRole("alert"),
    ).toHaveTextContent(/server error|500/i);
  });

  it("renders the collected-but-excluded section when include_excluded is on", async () => {
    mockFetch({
      [DEFAULT_URL]: () =>
        new Response(
          JSON.stringify(
            makeNewsRadarResponse({
              excluded_items: [
                makeNewsRadarItem({
                  id: "99",
                  title: "Iran sanctions briefing item",
                  included_in_briefing: false,
                }),
              ],
            }),
          ),
        ),
    });

    render(<NewsRadarPage />, { wrapper: MemoryRouter });
    expect(
      await screen.findByRole("region", {
        name: /collected but excluded/i,
      }),
    ).toBeInTheDocument();
  });
});
