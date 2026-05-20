// ROB-274 — frontend tests for English category badges + proposal diff panel.

import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";

import { InvestmentReportBundleContent } from "../components/investment-reports/InvestmentReportBundleContent";
import type {
  InvestmentReport,
  InvestmentReportBundle,
  InvestmentReportItem,
} from "../types/investmentReports";

vi.mock("../hooks/useInvestmentReportBundle", () => ({
  useInvestmentReportBundle: vi.fn(),
}));

import { useInvestmentReportBundle } from "../hooks/useInvestmentReportBundle";

function makeReport(): InvestmentReport {
  return {
    reportUuid: "00000000-0000-0000-0000-000000000001",
    reportType: "kr_morning",
    market: "kr",
    marketSession: "regular",
    accountScope: "kis_mock",
    executionMode: "advisory_only",
    createdByProfile: "test",
    title: "Test Report",
    summary: "summary",
    riskSummary: null,
    thesisText: null,
    noActionNote: null,
    marketSnapshot: {},
    portfolioSnapshot: {},
    previousReportUuid: null,
    status: "published",
    metadata: {},
    createdAt: "2026-05-20T00:00:00Z",
    updatedAt: "2026-05-20T00:00:00Z",
    publishedAt: null,
    validUntil: null,
    snapshotBundleUuid: null,
    snapshotPolicyVersion: null,
    snapshotCoverageSummary: null,
    snapshotFreshnessSummary: null,
    sourceConflicts: null,
    unavailableSources: null,
  };
}

function makeItem(overrides: Partial<InvestmentReportItem>): InvestmentReportItem {
  return {
    itemUuid: "00000000-0000-0000-0000-000000000002",
    itemKind: "watch",
    symbol: "KRW-BTC",
    side: null,
    intent: "trend_recovery_review",
    rationale: "test rationale",
    targetKind: "asset",
    priority: 0,
    confidence: null,
    evidenceSnapshot: {},
    watchCondition: null,
    triggerChecklist: [],
    maxAction: {},
    validUntil: null,
    status: "proposed",
    metadata: {},
    createdAt: "2026-05-20T00:00:00Z",
    updatedAt: "2026-05-20T00:00:00Z",
    operation: null,
    targetRef: null,
    currentState: null,
    proposedState: null,
    diff: null,
    applyPolicy: null,
    ...overrides,
  };
}

function makeBundle(
  itemOverrides: Partial<InvestmentReportItem>,
  alertCount: number = 1,
): InvestmentReportBundle {
  return {
    report: makeReport(),
    items: [makeItem(itemOverrides)],
    decisionsByItemUuid: {},
    alerts: Array.from({ length: alertCount }, (_, i) => ({
      alertUuid: `alert-${i}`,
      sourceReportUuid: "00000000-0000-0000-0000-000000000001",
      sourceItemUuid: "00000000-0000-0000-0000-000000000002",
      market: "kr" as const,
      targetKind: "asset" as const,
      symbol: "KRW-BTC",
      metric: "price" as const,
      operator: "above" as const,
      threshold: "100",
      thresholdKey: "100",
      intent: "buy_review" as const,
      actionMode: "notify_only" as const,
      rationale: "watch rationale",
      triggerChecklist: [],
      maxAction: {},
      validUntil: "2026-06-01T00:00:00Z",
      status: "active" as const,
      metadata: {},
      createdAt: "2026-05-20T00:00:00Z",
      activatedAt: "2026-05-20T00:00:00Z",
      updatedAt: "2026-05-20T00:00:00Z",
    })),
    events: [],
  };
}

function renderContent(bundle: InvestmentReportBundle) {
  (useInvestmentReportBundle as unknown as ReturnType<typeof vi.fn>).mockReturnValue({
    status: "ready",
    bundle,
    error: null,
    reload: vi.fn(),
  });
  return render(
    <MemoryRouter initialEntries={["/reports/00000000-0000-0000-0000-000000000001"]}>
      <InvestmentReportBundleContent />
    </MemoryRouter>,
  );
}

describe("InvestmentReportBundleContent — ROB-274 proposal badges + diff", () => {
  it("renders watch badge as 'watch' (English) and not '와치'", () => {
    renderContent(makeBundle({}));
    expect(screen.getAllByText("watch").length).toBeGreaterThan(0);
    expect(screen.queryByText("와치")).toBeNull();
  });

  it("renders operation badge and diff table for modify", () => {
    const bundle = makeBundle({
      operation: "modify",
      targetRef: {
        type: "investment_watch_alert",
        id: "alert-1",
        status: "active",
      },
      currentState: { threshold: "100" },
      proposedState: { threshold: "120" },
      diff: [{ field: "threshold", from: "100", to: "120" }],
      applyPolicy: "requires_user_approval",
    });
    renderContent(bundle);
    expect(screen.getByText("modify")).toBeInTheDocument();
    expect(screen.getByText("threshold")).toBeInTheDocument();
  });

  it("renders 'active watches' counter (English) and not '활성 와치'", () => {
    renderContent(makeBundle({}, 2));
    expect(screen.getByText(/active watches/)).toBeInTheDocument();
    expect(screen.queryByText(/활성 와치/)).toBeNull();
  });
});
