import type {
  NewsRadarItem,
  NewsRadarResponse,
} from "../../api/types";

export function makeNewsRadarItem(
  overrides: Partial<NewsRadarItem> = {},
): NewsRadarItem {
  return {
    id: "1",
    title: "UAE airstrike on tanker in Hormuz",
    source: "Reuters",
    feed_source: "rss_reuters",
    url: "https://example.test/uae",
    published_at: "2026-05-05T11:00:00Z",
    market: "us",
    risk_category: "geopolitical_oil",
    severity: "high",
    themes: ["oil", "defense"],
    symbols: ["XOM"],
    included_in_briefing: false,
    briefing_reason: "filtered_out_low_rank_or_not_selected",
    briefing_score: 12,
    snippet: "Tanker attack pushes Brent higher.",
    matched_terms: ["uae", "airstrike"],
    ...overrides,
  };
}

export function makeNewsRadarResponse(
  overrides: Partial<NewsRadarResponse> = {},
): NewsRadarResponse {
  const high = makeNewsRadarItem();
  return {
    market: "all",
    as_of: "2026-05-05T12:00:00Z",
    readiness: {
      status: "ready",
      latest_scraped_at: "2026-05-05T11:55:00Z",
      latest_published_at: "2026-05-05T11:50:00Z",
      recent_6h_count: 12,
      recent_24h_count: 80,
      source_count: 6,
      stale: false,
      max_age_minutes: 180,
      warnings: [],
    },
    summary: {
      high_risk_count: 1,
      total_count: 1,
      included_in_briefing_count: 0,
      excluded_but_collected_count: 1,
    },
    sections: [
      {
        section_id: "geopolitical_oil",
        title: "Geopolitical / Oil shock",
        severity: "high",
        items: [high],
      },
    ],
    items: [high],
    excluded_items: [high],
    source_coverage: [],
    ...overrides,
  };
}
