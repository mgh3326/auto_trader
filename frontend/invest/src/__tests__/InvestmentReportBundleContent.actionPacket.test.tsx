import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";

import { InvestmentReportBundleContent } from "../components/investment-reports/InvestmentReportBundleContent";
import type { ActionPacket, InvestmentReportBundle } from "../types/investmentReports";

vi.mock("../hooks/useInvestmentReportBundle", () => ({
  useInvestmentReportBundle: vi.fn(),
}));
import { useInvestmentReportBundle } from "../hooks/useInvestmentReportBundle";

const REPORT = {
  reportUuid: "00000000-0000-0000-0000-000000000001",
  reportType: "kr_morning", market: "kr", marketSession: "regular",
  accountScope: "kis_live", executionMode: "advisory_only", createdByProfile: "t",
  title: "KR", summary: "s", riskSummary: null, thesisText: null, noActionNote: null,
  marketSnapshot: {}, portfolioSnapshot: {}, previousReportUuid: null, status: "draft",
  metadata: {}, createdAt: "2026-05-27T00:00:00Z", updatedAt: "2026-05-27T00:00:00Z",
  publishedAt: null, validUntil: null, snapshotBundleUuid: null,
  snapshotPolicyVersion: null, snapshotCoverageSummary: null,
  snapshotFreshnessSummary: null, sourceConflicts: null, unavailableSources: null,
  snapshotReportDiagnostics: null,
} as InvestmentReportBundle["report"];

function makeBundle(actionPacket: ActionPacket | null): InvestmentReportBundle {
  return {
    report: REPORT, items: [], decisionsByItemUuid: {}, alerts: [], events: [],
    reviewSections: null, actionPacket,
  };
}

function renderWith(bundle: InvestmentReportBundle) {
  (useInvestmentReportBundle as unknown as ReturnType<typeof vi.fn>).mockReturnValue({
    status: "ready", bundle, error: null, reload: vi.fn(),
  });
  return render(
    <MemoryRouter initialEntries={["/reports/00000000-0000-0000-0000-000000000001"]}>
      <InvestmentReportBundleContent />
    </MemoryRouter>,
  );
}

describe("InvestmentReportBundleContent — ActionPacket mount", () => {
  it("renders ActionPacketView when actionPacket is present", () => {
    renderWith(makeBundle({
      heldActions: [{ verdict: "keep", symbol: "005930", side: null,
        rationale: "보유 유지 권장", itemUuid: "i1", evidenceSnapshot: {} }],
      newBuyCandidates: [], noNewBuyReason: "신규 후보 없음",
      riskReviews: [], noActionReason: null, dataGapsForNextCycle: [],
    }));
    expect(screen.getByTestId("action-packet")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: /오늘의 보유 액션/ })).toBeInTheDocument();
  });

  it("does not render ActionPacketView for legacy bundles (actionPacket null)", () => {
    renderWith(makeBundle(null));
    expect(screen.queryByTestId("action-packet")).not.toBeInTheDocument();
  });
});
