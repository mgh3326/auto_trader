import type {
  PreopenCandidateSummary,
  PreopenLatestResponse,
  PreopenLinkedSession,
  PreopenReconciliationSummary,
} from "../../api/types";

const now = "2026-04-29T06:00:00Z";

export function makePreopenCandidate(
  overrides: Partial<PreopenCandidateSummary> = {},
): PreopenCandidateSummary {
  return {
    candidate_uuid: "cand-1111-1111-1111-111111111111",
    symbol: "005930",
    instrument_type: "equity_kr",
    side: "buy",
    candidate_kind: "proposed",
    proposed_price: "70000",
    proposed_qty: "10",
    confidence: 75,
    rationale: "Strong momentum play",
    currency: "KRW",
    warnings: [],
    ...overrides,
  };
}

export function makePreopenReconciliation(
  overrides: Partial<PreopenReconciliationSummary> = {},
): PreopenReconciliationSummary {
  return {
    order_id: "ORD-1",
    symbol: "005930",
    market: "kr",
    side: "buy",
    classification: "near_fill",
    nxt_classification: "buy_pending_actionable",
    nxt_actionable: true,
    gap_pct: "0.5000",
    summary: "Gap within near fill threshold",
    reasons: ["gap_within_near_fill_pct"],
    warnings: [],
    ...overrides,
  };
}

export function makePreopenLinkedSession(
  overrides: Partial<PreopenLinkedSession> = {},
): PreopenLinkedSession {
  return {
    session_uuid: "sess-aaaa-bbbb-cccc-dddddddddddd",
    status: "open",
    created_at: now,
    ...overrides,
  };
}

export function makePreopenResponse(
  overrides: Partial<PreopenLatestResponse> = {},
): PreopenLatestResponse {
  return {
    has_run: true,
    advisory_used: true,
    advisory_skipped_reason: null,
    run_uuid: "run-1111-2222-3333-444444444444",
    market_scope: "kr",
    stage: "preopen",
    status: "open",
    strategy_name: "Morning scan",
    source_profile: "roadmap",
    generated_at: now,
    created_at: now,
    notes: null,
    market_brief: null,
    source_freshness: null,
    source_warnings: [],
    advisory_links: [],
    candidate_count: 1,
    reconciliation_count: 1,
    candidates: [makePreopenCandidate()],
    reconciliations: [makePreopenReconciliation()],
    linked_sessions: [],
    ...overrides,
  };
}

export function makePreopenFailOpen(
  overrides: Partial<PreopenLatestResponse> = {},
): PreopenLatestResponse {
  return {
    has_run: false,
    advisory_used: false,
    advisory_skipped_reason: "no_open_preopen_run",
    run_uuid: null,
    market_scope: null,
    stage: null,
    status: null,
    strategy_name: null,
    source_profile: null,
    generated_at: null,
    created_at: null,
    notes: null,
    market_brief: null,
    source_freshness: null,
    source_warnings: [],
    advisory_links: [],
    candidate_count: 0,
    reconciliation_count: 0,
    candidates: [],
    reconciliations: [],
    linked_sessions: [],
    ...overrides,
  };
}
