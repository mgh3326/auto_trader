// ROB-322 PR2 — frontend tests for the five-section review surface.
//
// The KR report detail must render report-scoped review sections
// (신규매수 후보 → 보유종목 전략 변경 후보 → watch-only → 제외/확인 불가 →
// no-action summary) instead of the flat itemKind queue, while legacy
// reports without `reviewSections` fall back to the old grouping.

import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";

import { InvestmentReportBundleContent } from "../components/investment-reports/InvestmentReportBundleContent";
import type {
  InvestmentReport,
  InvestmentReportBundle,
  InvestmentReportItem,
  ReportReviewSections,
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
    accountScope: "kis_live",
    executionMode: "advisory_only",
    createdByProfile: "test",
    title: "KR Report",
    summary: "summary",
    riskSummary: null,
    thesisText: null,
    noActionNote: null,
    marketSnapshot: {},
    portfolioSnapshot: {},
    previousReportUuid: null,
    status: "draft",
    metadata: {},
    createdAt: "2026-05-27T00:00:00Z",
    updatedAt: "2026-05-27T00:00:00Z",
    publishedAt: null,
    validUntil: null,
    snapshotBundleUuid: null,
    snapshotPolicyVersion: null,
    snapshotCoverageSummary: null,
    snapshotFreshnessSummary: null,
    sourceConflicts: null,
    unavailableSources: null,
    snapshotReportDiagnostics: null,
  };
}

let uuidSeq = 0;
function makeItem(overrides: Partial<InvestmentReportItem>): InvestmentReportItem {
  uuidSeq += 1;
  return {
    itemUuid: `item-${uuidSeq}`,
    itemKind: "action",
    symbol: "005930",
    side: "buy",
    intent: "buy_review",
    rationale: "근거 텍스트",
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
    createdAt: "2026-05-27T00:00:00Z",
    updatedAt: "2026-05-27T00:00:00Z",
    operation: null,
    targetRef: null,
    currentState: null,
    proposedState: null,
    diff: null,
    applyPolicy: null,
    decisionBucket: null,
    citedSymbolReportUuid: null,
    citedDimensionReportUuids: [],
    ...overrides,
  };
}

function makeBundle(
  reviewSections: ReportReviewSections | null,
  extraItems: InvestmentReportItem[] = [],
): InvestmentReportBundle {
  const sectionItems = reviewSections
    ? reviewSections.sections.flatMap((s) => s.items)
    : [];
  return {
    report: makeReport(),
    items: [...sectionItems, ...extraItems],
    decisionsByItemUuid: {},
    alerts: [],
    events: [],
    reviewSections,
  };
}

function renderContent(bundle: InvestmentReportBundle) {
  (
    useInvestmentReportBundle as unknown as ReturnType<typeof vi.fn>
  ).mockReturnValue({
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

const buyItem = makeItem({
  symbol: "035720",
  decisionBucket: "new_buy_candidate",
});
const heldItem = makeItem({
  symbol: "005930",
  side: "sell",
  intent: "sell_review",
  decisionBucket: "open_action",
});
const watchItem = makeItem({
  symbol: "051910",
  itemKind: "watch",
  side: null,
  intent: "risk_review",
  decisionBucket: "risk_watch",
});
const excludedItem = makeItem({
  symbol: "207940",
  decisionBucket: "deferred_no_action",
});

function fullReviewSections(): ReportReviewSections {
  return {
    sections: [
      { key: "new_buy_candidate", labelKo: "신규매수 후보", items: [buyItem] },
      {
        key: "held_strategy_review",
        labelKo: "보유종목 전략 변경 후보",
        items: [heldItem],
      },
      { key: "watch_only", labelKo: "watch-only", items: [watchItem] },
      {
        key: "excluded_or_unavailable",
        labelKo: "제외 / 확인 불가",
        items: [excludedItem],
      },
    ],
    noActionSummary: {
      kind: "stale_gated",
      reasonKo: "스냅샷 stale — market 신선도 부족으로 매수/매도 권고 보류",
      blockingSources: ["market"],
      excludedCount: 1,
    },
  };
}

describe("InvestmentReportBundleContent — ROB-322 review sections", () => {
  it("renders the four review section headers in Korean", () => {
    renderContent(makeBundle(fullReviewSections()));
    expect(
      screen.getByRole("heading", { name: /신규매수 후보/ }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("heading", { name: /보유종목 전략 변경 후보/ }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("heading", { name: /watch-only/ }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("heading", { name: /제외 \/ 확인 불가/ }),
    ).toBeInTheDocument();
  });

  it("nests each item under its owning section", () => {
    renderContent(makeBundle(fullReviewSections()));
    const buySection = screen
      .getByRole("heading", { name: /신규매수 후보/ })
      .closest("section") as HTMLElement;
    expect(buySection.textContent).toContain("035720");
    // The excluded item must NOT leak into the new-buy section.
    expect(buySection.textContent).not.toContain("207940");
  });

  it("renders the no-action summary with reason and excluded count", () => {
    renderContent(makeBundle(fullReviewSections()));
    const summarySection = screen
      .getByRole("heading", { name: /no-action/i })
      .closest("section") as HTMLElement;
    // reasonKo is surfaced verbatim (unique phrase).
    expect(summarySection.textContent).toContain("매수/매도 권고 보류");
    expect(summarySection.textContent).toContain("제외 / 확인 불가 1건");
  });

  it("does NOT render the flat itemKind queue when review sections are present", () => {
    renderContent(makeBundle(fullReviewSections()));
    expect(
      screen.queryByRole("heading", { name: /^action \(/ }),
    ).toBeNull();
  });

  it("falls back to the flat itemKind grouping for legacy reports (no reviewSections)", () => {
    const legacy = makeBundle(null, [makeItem({ symbol: "068270" })]);
    renderContent(legacy);
    expect(
      screen.getByRole("heading", { name: /^action \(/ }),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("heading", { name: /신규매수 후보/ }),
    ).toBeNull();
  });

  it("does not hide items that are not projected into any section", () => {
    const unprojected = makeItem({ symbol: "068270", decisionBucket: null });
    renderContent(makeBundle(fullReviewSections(), [unprojected]));
    const fallback = screen
      .getByRole("heading", { name: /분류 없음/ })
      .closest("section") as HTMLElement;
    expect(fallback.textContent).toContain("068270");
  });
});
