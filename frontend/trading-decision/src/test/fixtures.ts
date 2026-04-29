import type {
  ActionDetail,
  CounterfactualDetail,
  OutcomeDetail,
  ProposalDetail,
  SessionAnalyticsCell,
  SessionAnalyticsResponse,
  SessionDetail,
  SessionListResponse,
  SessionSummary,
  StrategyEventDetail,
  StrategyEventListResponse,
} from "../api/types";
import type {
  CandidateKind,
  NxtClassification,
  ReconciliationPayload,
  ReconciliationStatus,
} from "../api/reconciliation";

const now = "2026-04-28T06:00:00Z";

export function makeAction(
  overrides: Partial<ActionDetail> = {},
): ActionDetail {
  return {
    id: 1,
    action_kind: "live_order",
    external_order_id: "KIS-123",
    external_paper_id: null,
    external_watch_id: null,
    external_source: "paper",
    payload_snapshot: { status: "submitted" },
    recorded_at: now,
    created_at: now,
    ...overrides,
  };
}

export function makeCounterfactual(
  overrides: Partial<CounterfactualDetail> = {},
): CounterfactualDetail {
  return {
    id: 10,
    track_kind: "rejected_counterfactual",
    baseline_price: "117800000",
    baseline_at: now,
    quantity: "0.25",
    payload: { reason: "baseline" },
    notes: "Track rejected path",
    created_at: now,
    ...overrides,
  };
}

export function makeOutcome(
  overrides: Partial<OutcomeDetail> = {},
): OutcomeDetail {
  return {
    id: 100,
    counterfactual_id: null,
    track_kind: "accepted_live",
    horizon: "1h",
    price_at_mark: "118000000",
    pnl_pct: "1.2500",
    pnl_amount: "1500.0000",
    marked_at: now,
    payload: null,
    created_at: now,
    ...overrides,
  };
}

export function makeAnalyticsCell(
  overrides: Partial<SessionAnalyticsCell> = {},
): SessionAnalyticsCell {
  return {
    track_kind: "accepted_live",
    horizon: "1h",
    outcome_count: 2,
    proposal_count: 1,
    mean_pnl_pct: "1.2500",
    sum_pnl_amount: "3000.0000",
    latest_marked_at: now,
    ...overrides,
  };
}

export function makeAnalyticsResponse(
  overrides: Partial<SessionAnalyticsResponse> = {},
): SessionAnalyticsResponse {
  return {
    session_uuid: "session-1",
    generated_at: now,
    tracks: [
      "accepted_live",
      "accepted_paper",
      "rejected_counterfactual",
      "analyst_alternative",
      "user_alternative",
    ],
    horizons: ["1h", "4h", "1d", "3d", "7d", "final"],
    cells: [makeAnalyticsCell()],
    ...overrides,
  };
}

export function makeProposal(
  overrides: Partial<ProposalDetail> = {},
): ProposalDetail {
  return {
    proposal_uuid: "proposal-btc",
    symbol: "BTC",
    instrument_type: "crypto",
    proposal_kind: "trim",
    side: "sell",
    user_response: "pending",
    responded_at: null,
    created_at: now,
    updated_at: now,
    original_quantity: null,
    original_quantity_pct: "20",
    original_amount: null,
    original_price: "117800000",
    original_trigger_price: null,
    original_threshold_pct: null,
    original_currency: "KRW",
    original_rationale: "Trim into strength.",
    original_payload: { confidence: "medium" },
    user_quantity: null,
    user_quantity_pct: null,
    user_amount: null,
    user_price: null,
    user_trigger_price: null,
    user_threshold_pct: null,
    user_note: null,
    actions: [],
    counterfactuals: [],
    outcomes: [],
    ...overrides,
  };
}

export function makeSessionSummary(
  overrides: Partial<SessionSummary> = {},
): SessionSummary {
  return {
    session_uuid: "session-1",
    source_profile: "roadmap",
    strategy_name: "Momentum rebalance",
    market_scope: "crypto",
    status: "open",
    generated_at: now,
    created_at: now,
    updated_at: now,
    proposals_count: 3,
    pending_count: 3,
    ...overrides,
  };
}

export function makeSessionList(
  overrides: Partial<SessionListResponse> = {},
): SessionListResponse {
  return {
    sessions: [makeSessionSummary()],
    total: 1,
    limit: 50,
    offset: 0,
    ...overrides,
  };
}

export function makeSessionDetail(
  overrides: Partial<SessionDetail> = {},
): SessionDetail {
  const proposals = [
    makeProposal({ actions: [makeAction()] }),
    makeProposal({
      proposal_uuid: "proposal-eth",
      symbol: "ETH",
      proposal_kind: "pullback_watch",
      side: "buy",
      original_quantity_pct: null,
      original_price: "3200",
      original_trigger_price: "3000",
      original_rationale: "Watch pullback support.",
      actions: [],
    }),
    makeProposal({
      proposal_uuid: "proposal-sol",
      symbol: "SOL",
      proposal_kind: "pullback_watch",
      side: "buy",
      original_quantity_pct: null,
      original_price: "150",
      original_trigger_price: "140",
      actions: [],
    }),
  ];

  return {
    ...makeSessionSummary(),
    market_brief: { regime: "risk-on", symbols: ["BTC", "ETH", "SOL"] },
    notes: "Review before market close.",
    proposals,
    ...overrides,
  };
}

export function makeReconciliationPayload(
  overrides: Partial<ReconciliationPayload> = {},
): ReconciliationPayload {
  return {
    research_run_id: "11111111-1111-1111-1111-111111111111",
    candidate_kind: "pending_order" as CandidateKind,
    pending_order_id: "ORD-1",
    reconciliation_status: "near_fill" as ReconciliationStatus,
    reconciliation_summary: "gap_within_near_fill_pct",
    nxt_classification: "buy_pending_actionable" as NxtClassification,
    nxt_summary: "Pending fill within 0.5% of current price.",
    nxt_eligible: true,
    venue_eligibility: { nxt: true, regular: true },
    live_quote: { price: "70200", as_of: "2026-04-29T01:00:00Z" },
    decision_support: {
      current_price: "70200",
      gap_pct: "0.2857",
      signed_distance_to_fill: "-0.2857",
      nearest_support_price: "69500",
      nearest_support_distance_pct: "1.0",
      nearest_resistance_price: "71000",
      nearest_resistance_distance_pct: "1.14",
      bid_ask_spread_pct: "0.05",
    },
    warnings: [],
    refreshed_at: "2026-04-29T01:00:00Z",
    ...overrides,
  };
}

export function makeResearchRunMarketBrief(
  overrides: Record<string, unknown> = {},
): Record<string, unknown> {
  return {
    advisory_only: true,
    execution_allowed: false,
    research_run_uuid: "11111111-1111-1111-1111-111111111111",
    refreshed_at: "2026-04-29T01:00:00Z",
    counts: { candidates: 3, reconciliations: 1 },
    reconciliation_summary: {
      maintain: 1,
      near_fill: 1,
      too_far: 0,
      chasing_risk: 0,
      data_mismatch: 0,
      kr_pending_non_nxt: 1,
      unknown_venue: 0,
      unknown: 0,
    },
    nxt_summary: {
      actionable: 1,
      too_far: 0,
      non_nxt: 1,
      watch_only: 1,
      data_mismatch_requires_review: 0,
      unknown: 0,
    },
    snapshot_warnings: ["missing_orderbook"],
    source_warnings: [],
    ...overrides,
  };
}

export function makeStrategyEvent(
  overrides: Partial<StrategyEventDetail> = {},
): StrategyEventDetail {
  return {
    id: 1,
    event_uuid: "event-uuid-1",
    session_uuid: "session-1",
    source: "user",
    event_type: "operator_market_event",
    source_text: "OpenAI earnings missed expectations",
    normalized_summary: null,
    affected_markets: ["us"],
    affected_sectors: [],
    affected_themes: ["ai"],
    affected_symbols: ["MSFT", "NVDA"],
    severity: 3,
    confidence: 60,
    created_by_user_id: 7,
    metadata: null,
    created_at: now,
    ...overrides,
  };
}

export function makeStrategyEventListResponse(
  overrides: Partial<StrategyEventListResponse> = {},
): StrategyEventListResponse {
  return {
    events: [makeStrategyEvent()],
    total: 1,
    limit: 50,
    offset: 0,
    ...overrides,
  };
}
