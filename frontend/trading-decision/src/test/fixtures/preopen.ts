import type {
  PreopenCandidateSummary,
  PreopenLatestResponse,
  PreopenLinkedSession,
  PreopenNewsArticlePreview,
  PreopenNewsReadinessSummary,
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

export function makePreopenNewsReady(
  overrides: Partial<PreopenNewsReadinessSummary> = {},
): PreopenNewsReadinessSummary {
  return {
    status: "ready",
    is_ready: true,
    is_stale: false,
    latest_run_uuid: "news-run-1",
    latest_status: "success",
    latest_finished_at: now,
    latest_article_published_at: now,
    source_counts: { mk_stock: 12, yna_market: 8 },
    warnings: [],
    max_age_minutes: 180,
    ...overrides,
  };
}

export function makePreopenNewsStale(
  overrides: Partial<PreopenNewsReadinessSummary> = {},
): PreopenNewsReadinessSummary {
  return {
    ...makePreopenNewsReady(),
    status: "stale",
    is_ready: false,
    is_stale: true,
    warnings: ["news_stale"],
    ...overrides,
  };
}

export function makePreopenNewsUnavailable(
  overrides: Partial<PreopenNewsReadinessSummary> = {},
): PreopenNewsReadinessSummary {
  return {
    status: "unavailable",
    is_ready: false,
    is_stale: true,
    latest_run_uuid: null,
    latest_status: null,
    latest_finished_at: null,
    latest_article_published_at: null,
    source_counts: {},
    warnings: ["news_unavailable", "news_stale"],
    max_age_minutes: 180,
    ...overrides,
  };
}

export function makePreopenNewsArticle(
  overrides: Partial<PreopenNewsArticlePreview> = {},
): PreopenNewsArticlePreview {
  return {
    id: 1001,
    title: "삼성전자 1분기 실적 발표",
    url: "https://example.com/article/1001",
    source: "MK",
    feed_source: "mk_stock",
    published_at: now,
    summary: null,
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
    news: makePreopenNewsReady(),
    news_preview: [makePreopenNewsArticle()],
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
    news: null,
    news_preview: [],
    ...overrides,
  };
}
