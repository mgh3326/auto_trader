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
    accountScope: "upbit_live",
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
    createdAt: "2026-06-12T00:00:00Z",
    updatedAt: "2026-06-12T00:00:00Z",
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
    itemUuid: "u1",
    itemKind: "action",
    symbol: "BTC",
    side: "buy",
    intent: "buy_review",
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
    createdAt: "2026-06-12T00:00:00Z",
    updatedAt: "2026-06-12T00:00:00Z",
    operation: null,
    targetRef: null,
    currentState: null,
    proposedState: null,
    diff: null,
    applyPolicy: null,
    ...overrides,
  };
}

function renderContent(bundle: InvestmentReportBundle) {
  (useInvestmentReportBundle as unknown as ReturnType<typeof vi.fn>).mockReturnValue(
    { status: "ready", bundle, error: null, reload: vi.fn() },
  );
  return render(
    <MemoryRouter initialEntries={["/reports/00000000-0000-0000-0000-000000000001"]}>
      <InvestmentReportBundleContent />
    </MemoryRouter>,
  );
}

function makeBundle(item: InvestmentReportItem): InvestmentReportBundle {
  return {
    report: makeReport(),
    items: [item],
    decisionsByItemUuid: {},
    alerts: [],
    events: [],
    forecastsByItemUuid: {},
    retrospectivesByItemUuid: {},
  };
}

describe("ROB-715 item loop section", () => {
  it("renders forecast status and retrospective outcome for an item", () => {
    const bundle = makeBundle(makeItem({}));
    bundle.forecastsByItemUuid = {
      u1: [
        {
          forecastId: "f1",
          status: "closed",
          outcome: true,
          reviewDate: "2026-07-20",
          direction: "at_or_above",
          targetPrice: 200000,
          probability: 0.6,
          brierScore: 0.09,
          resolutionSource: "ohlcv_day",
        },
      ],
    };
    bundle.retrospectivesByItemUuid = {
      u1: [
        {
          retrospectiveId: 1,
          outcome: "filled",
          lesson: "held to target",
          resultSummary: null,
          rootCauseClass: null,
          triggerType: null,
          pnlPct: 4.2,
          createdAt: null,
        },
      ],
    };
    renderContent(bundle);
    expect(screen.getByTestId("item-loop-forecast-f1")).toBeInTheDocument();
    expect(screen.getByText(/held to target/)).toBeInTheDocument();
  });

  it("renders the empty state when an item has no forecast or retrospective", () => {
    const bundle = makeBundle(makeItem({}));
    bundle.forecastsByItemUuid = {};
    bundle.retrospectivesByItemUuid = {};
    renderContent(bundle);
    expect(screen.getByText("해소 대기 / 미연결")).toBeInTheDocument();
  });
});