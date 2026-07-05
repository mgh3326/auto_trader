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

describe("ROB-715 raw item fields", () => {
  it("renders trigger checklist, max_action, decision bucket badge, evidence summary", () => {
    const bundle = makeBundle(
      makeItem({
        triggerChecklist: ["RSI < 30", "종가 20MA 회복"],
        maxAction: { side: "buy", notional: 1000000, limit_price: 195000 },
        decisionBucket: "new_buy_candidate",
        structuredEvidenceSummary: "2 evidence fields: momentum, valuation",
      }),
    );
    renderContent(bundle);
    expect(screen.getByText("RSI < 30")).toBeInTheDocument();
    expect(screen.getByTestId("item-decision-bucket-badge")).toHaveTextContent(
      "new_buy_candidate",
    );
    expect(screen.getByText(/2 evidence fields/)).toBeInTheDocument();
    expect(screen.getByTestId("item-max-action")).toBeInTheDocument();
  });

  it("formats a quantity-based max_action with the 주 suffix (not mojibake)", () => {
    const bundle = makeBundle(
      makeItem({
        maxAction: { side: "buy", quantity: 10, limit_price: 195000 },
      }),
    );
    renderContent(bundle);
    const maxAction = screen.getByTestId("item-max-action");
    expect(maxAction).toHaveTextContent("10주");
    expect(maxAction.textContent).not.toContain("······");
  });
});