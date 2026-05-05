import type {
  ResearchSessionCreateResponse,
  ResearchSessionFullResponse,
  ResearchSessionListItem,
  ResearchSummary,
  StageAnalysis,
  SymbolTimelineResponse,
} from "../../api/types";

export function makeSessionListItem(
  overrides: Partial<ResearchSessionListItem> = {},
): ResearchSessionListItem {
  return {
    id: 1,
    stock_info_id: 99,
    status: "finalized",
    created_at: "2026-05-05T00:00:00Z",
    decision: "buy",
    confidence: 75,
    ...overrides,
  };
}

export function makeStageAnalysis(
  overrides: Partial<StageAnalysis> = {},
): StageAnalysis {
  return {
    id: 10,
    stage_type: "market",
    verdict: "bull",
    confidence: 70,
    signals: { last_close: 100, change_pct: 1.5, trend: "uptrend" },
    raw_payload: null,
    source_freshness: null,
    executed_at: "2026-05-05T00:00:01Z",
    snapshot_at: null,
    ...overrides,
  };
}

export function makeResearchSummary(
  overrides: Partial<ResearchSummary> = {},
): ResearchSummary {
  return {
    id: 100,
    session_id: 1,
    decision: "buy",
    confidence: 80,
    bull_arguments: [
      {
        text: "RSI oversold",
        cited_stage_ids: [10],
        direction: "support",
        weight: 0.8,
      },
    ],
    bear_arguments: [],
    price_analysis: {
      appropriate_buy_min: 95,
      appropriate_buy_max: 100,
      appropriate_sell_min: null,
      appropriate_sell_max: null,
      buy_hope_min: null,
      buy_hope_max: null,
      sell_target_min: 110,
      sell_target_max: 120,
    },
    reasons: ["short-term momentum"],
    detailed_text: "buy signal confirmed",
    warnings: [],
    executed_at: "2026-05-05T00:00:02Z",
    summary_stage_links: [
      {
        stage_analysis_id: 10,
        stage_type: "market",
        direction: "support",
        weight: 0.8,
        rationale: "rsi 14 below 30",
      },
    ],
    ...overrides,
  };
}

export function makeSessionFull(
  overrides: Partial<ResearchSessionFullResponse> = {},
): ResearchSessionFullResponse {
  return {
    session: {
      id: 1,
      stock_info_id: 99,
      research_run_id: null,
      status: "finalized",
      started_at: "2026-05-05T00:00:00Z",
      finalized_at: "2026-05-05T00:00:03Z",
      created_at: "2026-05-05T00:00:00Z",
      updated_at: null,
      symbol: "KRW-BTC",
      instrument_type: "crypto",
    },
    stages: [
      makeStageAnalysis(),
      makeStageAnalysis({
        id: 11,
        stage_type: "news",
        signals: { headline_count: 5, sentiment_score: 0.4, top_themes: ["earnings"] },
      }),
      makeStageAnalysis({
        id: 12,
        stage_type: "fundamentals",
        signals: { per: 12, pbr: 1.5, market_cap: 1_000_000_000 },
      }),
      makeStageAnalysis({
        id: 13,
        stage_type: "social",
        verdict: "unavailable",
        confidence: 0,
        signals: { available: false, reason: "not_implemented", phase: "placeholder" },
      }),
    ],
    summary: makeResearchSummary(),
    ...overrides,
  };
}

export function makeCreateResponse(
  overrides: Partial<ResearchSessionCreateResponse> = {},
): ResearchSessionCreateResponse {
  return {
    session_id: 1,
    status: "running",
    started_at: "2026-05-05T00:00:00Z",
    ...overrides,
  };
}

export function makeSymbolTimeline(
  overrides: Partial<SymbolTimelineResponse> = {},
): SymbolTimelineResponse {
  return {
    symbol: "AAPL",
    days: 30,
    entries: [
      {
        session_id: 11,
        status: "finalized",
        started_at: "2026-05-05T00:00:00Z",
        finalized_at: "2026-05-05T00:00:03Z",
        decision: "buy",
        confidence: 70,
        stage_verdicts: {
          market: "bull",
          news: "neutral",
          fundamentals: "bull",
          social: "unavailable",
        },
      },
    ],
    ...overrides,
  };
}
