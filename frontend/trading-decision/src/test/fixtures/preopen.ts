import type {
  PreopenBriefingArtifact,
  PreopenCandidateSummary,
  PreopenLatestResponse,
  PreopenLinkedSession,
  PreopenMarketNewsBriefing,
  PreopenMarketNewsItem,
  PreopenNewsArticlePreview,
  PreopenNewsReadinessSummary,
  PreopenQaEvaluatorSummary,
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

export function makePreopenMarketNewsItem(
  overrides: Partial<PreopenMarketNewsItem> = {},
): PreopenMarketNewsItem {
  return {
    id: 2001,
    title: "코스피 장전 AI 반도체 강세 전망",
    url: "https://example.com/briefing/2001",
    source: "Yonhap",
    feed_source: "yna_market",
    published_at: now,
    summary: "AI 반도체와 대형주 수급을 장전 핵심 변수로 정리했습니다.",
    briefing_relevance: {
      score: 82,
      reason: "matched_section_terms",
      section_id: "preopen_headlines",
      matched_terms: ["AI", "반도체"],
    },
    crypto_relevance: null,
    ...overrides,
  };
}

export function makePreopenMarketNewsBriefing(
  overrides: Partial<PreopenMarketNewsBriefing> = {},
): PreopenMarketNewsBriefing {
  return {
    briefing_filter: true,
    summary: {
      included: 3,
      excluded: 2,
      sections: 2,
      uncategorized: 1,
    },
    sections: [
      {
        section_id: "preopen_headlines",
        title: "Preopen headlines",
        items: [makePreopenMarketNewsItem()],
      },
      {
        section_id: "sector_theme",
        title: "Sector themes",
        items: [
          makePreopenMarketNewsItem({
            id: 2002,
            title: "조선·방산 업종 수주 모멘텀 점검",
            briefing_relevance: {
              score: 74,
              reason: "matched_section_terms",
              section_id: "sector_theme",
              matched_terms: ["방산", "수주"],
            },
          }),
        ],
      },
    ],
    excluded_count: 2,
    top_excluded: [
      makePreopenMarketNewsItem({
        id: 2999,
        title: "저신호 단신 모음",
        briefing_relevance: {
          score: 12,
          reason: "low_relevance",
          section_id: null,
          matched_terms: [],
        },
      }),
    ],
    ...overrides,
  };
}


export function makePreopenBriefingArtifact(
  overrides: Partial<PreopenBriefingArtifact> = {},
): PreopenBriefingArtifact {
  return {
    artifact_type: "preopen_briefing",
    artifact_version: "v1",
    status: "ready",
    run_uuid: "run-1111-2222-3333-444444444444",
    market_scope: "kr",
    stage: "preopen",
    generated_at: now,
    source_run_status: "open",
    readiness: [
      {
        key: "research_run",
        status: "ready",
        is_ready: true,
        warnings: [],
        details: { source_run_status: "open" },
      },
      {
        key: "news",
        status: "ready",
        is_ready: true,
        warnings: [],
        details: { latest_run_uuid: "news-run-1" },
      },
    ],
    market_summary: "Cautious but constructive setup.",
    news_summary: "장전 핵심 뉴스",
    sections: [
      {
        section_id: "market_news",
        title: "Market news briefing",
        item_count: 3,
        status: "ready",
        summary: "3 high-signal articles across 2 sections",
        items: [],
      },
      {
        section_id: "new_buy_candidates",
        title: "New buy candidates",
        item_count: 1,
        status: "ready",
        summary: "1 buy candidates prepared before decision-session review.",
        items: [{ symbol: "005930", confidence: 75 }],
      },
      {
        section_id: "holdings_actions",
        title: "Holdings actions",
        item_count: 1,
        status: "ready",
        summary: "0 candidate actions and 1 pending reconciliations.",
        items: [{ symbol: "005930", classification: "near_fill" }],
      },
    ],
    risk_notes: [],
    cta: {
      state: "create_available",
      label: "Create decision session",
      run_uuid: "run-1111-2222-3333-444444444444",
      linked_session_uuid: null,
      disabled_reason: null,
      requires_confirmation: true,
    },
    qa: { read_only: true, mutation_paths: [], decision_session_created: false },
    ...overrides,
  };
}

export function makePreopenUnavailableArtifact(
  overrides: Partial<PreopenBriefingArtifact> = {},
): PreopenBriefingArtifact {
  return makePreopenBriefingArtifact({
    status: "unavailable",
    run_uuid: null,
    market_scope: null,
    stage: null,
    generated_at: null,
    source_run_status: null,
    readiness: [
      {
        key: "research_run",
        status: "unavailable",
        is_ready: false,
        warnings: ["no_open_preopen_run"],
        details: {},
      },
    ],
    market_summary: null,
    news_summary: null,
    sections: [],
    risk_notes: ["no_open_preopen_run"],
    cta: {
      state: "unavailable",
      label: "Create decision session unavailable",
      run_uuid: null,
      linked_session_uuid: null,
      disabled_reason: "no_open_preopen_run",
      requires_confirmation: true,
    },
    ...overrides,
  });
}


export function makePreopenQaEvaluator(
  overrides: Partial<PreopenQaEvaluatorSummary> = {},
): PreopenQaEvaluatorSummary {
  return {
    status: "ready",
    generated_at: now,
    source: "deterministic_v1",
    overall: {
      score: 90,
      grade: "excellent",
      confidence: "high",
      reason: "deterministic checks over already-loaded preopen response data",
    },
    checks: [
      {
        id: "has_open_run",
        label: "Open preopen run",
        status: "pass",
        severity: "info",
        summary: "Open preopen research run loaded for read-only evaluation.",
        details: null,
      },
      {
        id: "actionability_guardrail",
        label: "Actionability guardrail",
        status: "pass",
        severity: "info",
        summary: "QA evaluator is advisory-only and execution remains disabled.",
        details: { advisory_only: true, execution_allowed: false },
      },
    ],
    blocking_reasons: [],
    warnings: [],
    coverage: {
      candidate_count: 1,
      reconciliation_count: 1,
      advisory_only: true,
      execution_allowed: false,
    },
    ...overrides,
  };
}

export function makePreopenUnavailableQaEvaluator(
  overrides: Partial<PreopenQaEvaluatorSummary> = {},
): PreopenQaEvaluatorSummary {
  return makePreopenQaEvaluator({
    status: "unavailable",
    generated_at: null,
    overall: {
      score: null,
      grade: "unavailable",
      confidence: "unavailable",
      reason: "no_open_preopen_run",
    },
    checks: [
      {
        id: "has_open_run",
        label: "Open preopen run",
        status: "fail",
        severity: "high",
        summary: "No open preopen research run is available.",
        details: { reason: "no_open_preopen_run" },
      },
      {
        id: "actionability_guardrail",
        label: "Actionability guardrail",
        status: "pass",
        severity: "info",
        summary: "Evaluator is advisory-only and execution remains disabled.",
        details: { advisory_only: true, execution_allowed: false },
      },
    ],
    blocking_reasons: ["no_open_preopen_run"],
    warnings: [],
    coverage: {
      candidate_count: 0,
      candidate_items: 0,
      reconciliation_count: 0,
      reconciliation_items: 0,
      linked_session_count: 0,
      news_status: "unavailable",
      market_news_sections: 0,
      briefing_artifact_status: "unavailable",
      advisory_only: true,
      execution_allowed: false,
      advisory_skipped_reason: "no_open_preopen_run",
    },
    ...overrides,
  });
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
    market_news_briefing: makePreopenMarketNewsBriefing(),
    briefing_artifact: makePreopenBriefingArtifact(),
    qa_evaluator: makePreopenQaEvaluator(),
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
    market_news_briefing: null,
    briefing_artifact: makePreopenUnavailableArtifact(),
    qa_evaluator: makePreopenUnavailableQaEvaluator(),
    ...overrides,
  };
}
