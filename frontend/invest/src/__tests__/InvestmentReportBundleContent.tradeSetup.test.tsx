// ROB-690 — R:R chip rendered from evidenceSnapshot.trade_setup on ItemRow.

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

describe("InvestmentReportBundleContent — ROB-690 trade_setup R:R chip", () => {
  it("renders the long R:R chip when evidenceSnapshot.trade_setup is present", () => {
    renderContent(
      makeBundle(
        makeItem({
          evidenceSnapshot: {
            trade_setup: {
              direction: "long",
              stop: "65000",
              target: "78000",
              headline: {
                entry: "70000",
                risk_pct: "7.14",
                reward_pct: "11.43",
                rr_ratio: "1.60",
              },
              legs: [
                {
                  entry: "70000",
                  risk_pct: "7.14",
                  reward_pct: "11.43",
                  rr_ratio: "1.60",
                },
              ],
            },
          },
        }),
      ),
    );
    expect(screen.getByText("롱")).toBeInTheDocument();
    expect(
      screen.getByText("손익비 R:R 1.60 · 리스크 7.14% · 리워드 11.43%"),
    ).toBeInTheDocument();
  });

  it("renders the short R:R chip", () => {
    renderContent(
      makeBundle(
        makeItem({
          evidenceSnapshot: {
            trade_setup: {
              direction: "short",
              stop: "2600000",
              target: "2100000",
              headline: {
                entry: "2424000",
                risk_pct: "7.26",
                reward_pct: "13.37",
                rr_ratio: "1.84",
              },
              legs: [],
            },
          },
        }),
      ),
    );
    expect(screen.getByText("숏")).toBeInTheDocument();
    expect(
      screen.getByText("손익비 R:R 1.84 · 리스크 7.26% · 리워드 13.37%"),
    ).toBeInTheDocument();
  });

  it("renders no chip when trade_setup is absent (legacy/fail-closed items)", () => {
    renderContent(makeBundle(makeItem({ evidenceSnapshot: {} })));
    expect(screen.queryByText("롱")).toBeNull();
    expect(screen.queryByText("숏")).toBeNull();
    expect(screen.queryByText(/손익비 R:R/)).toBeNull();
  });

  it("renders no chip for a malformed trade_setup (missing headline)", () => {
    renderContent(
      makeBundle(
        makeItem({
          evidenceSnapshot: {
            trade_setup: { direction: "long" },
          },
        }),
      ),
    );
    expect(screen.queryByText(/손익비 R:R/)).toBeNull();
  });
});
