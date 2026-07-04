// ROB-693 — "무효화 조건" (invalidation_triggers) advisory narrative section
// rendered from evidenceSnapshot.invalidation_triggers on ItemRow.

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

describe("InvestmentReportBundleContent — ROB-693 invalidation_triggers section", () => {
  it("renders the '무효화 조건' section with each bullet when present", () => {
    renderContent(
      makeBundle(
        makeItem({
          evidenceSnapshot: {
            invalidation_triggers: [
              "분기 가이던스 하향 조정",
              "RSI 30 하회 후 5일 지속",
            ],
          },
        }),
      ),
    );
    expect(screen.getByText("무효화 조건")).toBeInTheDocument();
    expect(screen.getByText("분기 가이던스 하향 조정")).toBeInTheDocument();
    expect(screen.getByText("RSI 30 하회 후 5일 지속")).toBeInTheDocument();
  });

  it("renders no section when invalidation_triggers is absent", () => {
    renderContent(makeBundle(makeItem({ evidenceSnapshot: {} })));
    expect(screen.queryByText("무효화 조건")).toBeNull();
  });

  it("renders no section when invalidation_triggers is an empty array", () => {
    renderContent(
      makeBundle(makeItem({ evidenceSnapshot: { invalidation_triggers: [] } })),
    );
    expect(screen.queryByText("무효화 조건")).toBeNull();
  });

  it("renders no section when invalidation_triggers contains non-string elements", () => {
    renderContent(
      makeBundle(
        makeItem({
          evidenceSnapshot: { invalidation_triggers: ["ok", 42, null] },
        }),
      ),
    );
    expect(screen.queryByText("무효화 조건")).toBeNull();
  });

  it("renders no section when invalidation_triggers is not an array", () => {
    renderContent(
      makeBundle(
        makeItem({
          evidenceSnapshot: { invalidation_triggers: "not-an-array" },
        }),
      ),
    );
    expect(screen.queryByText("무효화 조건")).toBeNull();
  });

  it("coexists with the trade_setup R:R chip (both additive, independent)", () => {
    renderContent(
      makeBundle(
        makeItem({
          evidenceSnapshot: {
            trade_setup: {
              direction: "long",
              headline: {
                entry: "70000",
                risk_pct: "7.14",
                reward_pct: "11.43",
                rr_ratio: "1.60",
              },
              legs: [],
            },
            invalidation_triggers: ["실적 쇼크"],
          },
        }),
      ),
    );
    expect(screen.getByText("롱")).toBeInTheDocument();
    expect(screen.getByText("무효화 조건")).toBeInTheDocument();
    expect(screen.getByText("실적 쇼크")).toBeInTheDocument();
  });
});
