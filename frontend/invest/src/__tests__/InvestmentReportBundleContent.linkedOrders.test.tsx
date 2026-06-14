// ROB-554 — order/fill card rendered on the decision-log item row.

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
    itemUuid: "00000000-0000-0000-0000-000000000002",
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
  };
}

describe("InvestmentReportBundleContent — ROB-554 linked orders", () => {
  it("renders the fill badge + order line for a linked filled order", () => {
    renderContent(
      makeBundle(
        makeItem({
          linkedOrders: [
            {
              broker: "upbit",
              accountScope: "upbit_live",
              market: "crypto",
              orderNo: "df98c030-1234",
              ledgerId: 7,
              symbol: "BTC",
              side: "buy",
              status: "filled",
              filledQty: "0.01",
              avgFillPrice: "96180000",
              orderTime: "2026-06-12T05:03:00Z",
            },
          ],
        }),
      ),
    );
    expect(screen.getByText("체결")).toBeInTheDocument();
    expect(screen.getByText(/order df98c030/)).toBeInTheDocument();
  });

  it("renders no order card when there are no linked orders", () => {
    renderContent(makeBundle(makeItem({ linkedOrders: null })));
    expect(screen.queryByText("주문 · 체결")).toBeNull();
  });
});
