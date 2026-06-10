import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { beforeAll, describe, expect, it, vi } from "vitest";

import { InvestmentReportBundleContent } from "../components/investment-reports/InvestmentReportBundleContent";
import type {
  InvestmentReportBundle,
  InvestmentWatchAlert,
  InvestmentWatchEvent,
} from "../types/investmentReports";

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

const ACTIVE_ALERT = {
  alertUuid: "alert-active-1",
  sourceReportUuid: "00000000-0000-0000-0000-000000000001",
  sourceItemUuid: "item-1",
  market: "crypto", targetKind: "asset", symbol: "KRW-BTC",
  metric: "price", operator: "below", threshold: "100000000",
  thresholdKey: "below:100000000", intent: "buy_review", actionMode: "notify_only",
  rationale: "r", triggerChecklist: [], maxAction: {},
  validUntil: "2026-12-31T00:00:00Z", status: "active", metadata: {},
  createdAt: "2026-06-10T00:00:00Z", activatedAt: "2026-06-10T00:00:00Z",
  updatedAt: "2026-06-10T00:00:00Z",
} as InvestmentWatchAlert;

const TRIGGERED_ALERT = {
  ...ACTIVE_ALERT,
  alertUuid: "alert-triggered-1",
  status: "triggered",
} as InvestmentWatchAlert;

const EVENT = {
  eventUuid: "event-1",
  sourceReportUuid: "00000000-0000-0000-0000-000000000001",
  sourceItemUuid: "item-1",
  market: "crypto", targetKind: "asset", symbol: "KRW-BTC",
  metric: "price", operator: "below", threshold: "100000000",
  thresholdKey: "below:100000000", intent: "buy_review", actionMode: "notify_only",
  currentValue: "99000000", scannerSnapshot: {}, outcome: "notified",
  correlationId: "c1", kstDate: "2026-06-10",
  deliveryStatus: "delivered", deliveryAttempts: 1,
  createdAt: "2026-06-10T00:00:00Z",
} as InvestmentWatchEvent;

function makeBundle(): InvestmentReportBundle {
  return {
    report: REPORT, items: [], decisionsByItemUuid: {},
    alerts: [ACTIVE_ALERT, TRIGGERED_ALERT], events: [EVENT],
    reviewSections: null, actionPacket: null,
  };
}

function renderWith(
  bundle: InvestmentReportBundle,
  initialEntry = "/reports/00000000-0000-0000-0000-000000000001",
) {
  (useInvestmentReportBundle as unknown as ReturnType<typeof vi.fn>).mockReturnValue({
    status: "ready", bundle, error: null, reload: vi.fn(),
  });
  return render(
    <MemoryRouter initialEntries={[initialEntry]}>
      <InvestmentReportBundleContent />
    </MemoryRouter>,
  );
}

beforeAll(() => {
  // jsdom does not implement scrollIntoView.
  Element.prototype.scrollIntoView = vi.fn();
});

describe("InvestmentReportBundleContent — watch anchors & sections (ROB-500)", () => {
  it("renders stable id anchors on alert and event rows", () => {
    const { container } = renderWith(makeBundle());
    expect(container.querySelector("#watch-alert-alert-active-1")).not.toBeNull();
    expect(container.querySelector("#watch-alert-alert-triggered-1")).not.toBeNull();
    expect(container.querySelector("#watch-event-event-1")).not.toBeNull();
  });

  it("splits active and triggered watches into separate sections", () => {
    renderWith(makeBundle());
    expect(
      screen.getByRole("heading", { name: /active watches \(1\)/ }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("heading", { name: /triggered \/ closed watches \(1\)/ }),
    ).toBeInTheDocument();
  });

  it("scrolls the anchored row into view when arriving with a hash", () => {
    const spy = vi.spyOn(Element.prototype, "scrollIntoView");
    renderWith(
      makeBundle(),
      "/reports/00000000-0000-0000-0000-000000000001#watch-event-event-1",
    );
    expect(spy).toHaveBeenCalled();
  });
});
