import type { ReactElement } from "react";
import { render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, expect, test, vi } from "vitest";

import * as scalpingApi from "../api/scalping";
import { ScalpingRoute } from "../pages/desktop/DesktopScalpingPage";
import { AccountPanelProvider } from "../desktop/AccountPanelProvider";
import { mockRightRail } from "../test/mockRightRail";
import type {
  ScalpingReview,
  ScalpingReviewAction,
  ScalpingTrade,
} from "../types/scalping";

const REVIEW: ScalpingReview = {
  id: 1,
  reviewDate: "2026-05-25",
  product: "usdm_futures",
  accountScope: "binance_demo",
  sessionTag: "",
  metrics: {
    tradeCount: 2,
    winCount: 1,
    lossCount: 1,
    anomalyCount: 1,
    grossPnlUsdt: "0.0",
    netPnlUsdt: "-0.2",
    netReturnBps: "-10",
    avgSlippageBps: "3",
    avgSpreadBps: null, // n/a — no row carried it
    avgMaeBps: "-10",
    avgMfeBps: "40",
    avgHoldingSeconds: 15,
    exitReasonCounts: { take_profit: 1, stop_loss: 1 },
  },
  observation: "스프레드가 장중 확대됨",
  rootCause: null,
  improvement: null,
  nextRunPlan: null,
  decision: "adjust",
  status: "draft",
  sourcePayload: { row_count: 2 },
  createdAt: "2026-05-25T12:00:00Z",
  updatedAt: "2026-05-25T12:00:00Z",
};

const ACTION: ScalpingReviewAction = {
  id: 10,
  reviewId: 1,
  actionType: "parameter_change",
  title: "TP를 40bps로 확대",
  rationale: "MFE가 일관되게 30bps 초과",
  targetComponent: null,
  proposedChange: null,
  expectedEffect: null,
  status: "applied",
  createdAt: null,
  updatedAt: null,
};

const TRADE_OK: ScalpingTrade = {
  id: 1,
  openClientOrderId: "o-1",
  symbol: "XRPUSDT",
  side: "BUY",
  qty: "1",
  entryPrice: "100",
  exitPrice: "101",
  entrySlippageBps: "2",
  exitSlippageBps: "1",
  entrySpreadBps: "5",
  exitSpreadBps: "6",
  maeBps: "-10",
  mfeBps: "40",
  netPnlUsdt: "0.9",
  holdingSeconds: 12,
  exitReason: "take_profit",
  isAnomaly: false,
};

const TRADE_ANOMALY: ScalpingTrade = {
  ...TRADE_OK,
  id: 2,
  openClientOrderId: "o-2",
  entryPrice: null, // no derivable fill price → partial/anomaly
  exitPrice: null,
  entrySlippageBps: null,
  entrySpreadBps: null,
  maeBps: null,
  mfeBps: null,
  netPnlUsdt: null,
  holdingSeconds: null,
  exitReason: "timeout",
  isAnomaly: true,
};

function wrap(ui: ReactElement) {
  return (
    <AccountPanelProvider>
      <MemoryRouter basename="/invest" initialEntries={["/invest/scalping"]}>
        {ui}
      </MemoryRouter>
    </AccountPanelProvider>
  );
}

beforeEach(() => {
  localStorage.clear();
  mockRightRail();
});

test("empty state explains analytics rows are required", async () => {
  vi.spyOn(scalpingApi, "fetchScalpingReviews").mockResolvedValue({ items: [] });
  vi.spyOn(scalpingApi, "fetchScalpingTrades").mockResolvedValue({ items: [] });

  render(wrap(<ScalpingRoute />));

  await waitFor(() =>
    expect(screen.getByText(/아직 분석 데이터가 없습니다/)).toBeInTheDocument(),
  );
});

test("ready state renders summary, daily loop, trade table, and actions", async () => {
  vi.spyOn(scalpingApi, "fetchScalpingReviews").mockResolvedValue({ items: [REVIEW] });
  vi.spyOn(scalpingApi, "fetchScalpingReview").mockResolvedValue({
    review: REVIEW,
    actions: [ACTION],
  });
  vi.spyOn(scalpingApi, "fetchScalpingTrades").mockResolvedValue({
    items: [TRADE_OK, TRADE_ANOMALY],
  });

  render(wrap(<ScalpingRoute />));

  // Summary + decision label.
  await waitFor(() => expect(screen.getByText("조정")).toBeInTheDocument());
  // Daily loop card.
  expect(screen.getByText(/실행 → 관측 → 원인 → 개선/)).toBeInTheDocument();
  expect(screen.getByText("스프레드가 장중 확대됨")).toBeInTheDocument();
  // Trade table — both symbols, anomaly badge on the partial row.
  expect(screen.getAllByText("XRPUSDT").length).toBeGreaterThanOrEqual(2);
  // "이상치" appears both as the anomaly-count metric label and the row badge.
  expect(screen.getAllByText("이상치").length).toBeGreaterThanOrEqual(2);
  // Action list with status.
  expect(screen.getByText("TP를 40bps로 확대")).toBeInTheDocument();
  expect(screen.getByText("적용됨")).toBeInTheDocument();
});

test("renders per-session_tag comparison strip with labels", async () => {
  const ruleReview = { ...REVIEW, id: 1, sessionTag: "", metrics: { ...REVIEW.metrics, netReturnBps: "-10" } };
  const llmReview = { ...REVIEW, id: 2, sessionTag: "llm", metrics: { ...REVIEW.metrics, netReturnBps: "150" } };
  vi.spyOn(scalpingApi, "fetchScalpingReviews").mockResolvedValue({ items: [ruleReview, llmReview] });
  vi.spyOn(scalpingApi, "fetchScalpingReview").mockResolvedValue({ review: ruleReview, actions: [ACTION] });
  vi.spyOn(scalpingApi, "fetchScalpingTrades").mockResolvedValue({ items: [TRADE_OK] });

  render(wrap(<ScalpingRoute />));

  await waitFor(() => expect(screen.getByTestId("scalping-session-comparison")).toBeInTheDocument());
  const strip = screen.getByTestId("scalping-session-comparison");
  expect(within(strip).getByText("규칙")).toBeInTheDocument();
  expect(within(strip).getByText("LLM")).toBeInTheDocument();
});

test("null telemetry renders n/a, never 0 or blank", async () => {
  vi.spyOn(scalpingApi, "fetchScalpingReviews").mockResolvedValue({ items: [REVIEW] });
  vi.spyOn(scalpingApi, "fetchScalpingReview").mockResolvedValue({
    review: REVIEW,
    actions: [],
  });
  vi.spyOn(scalpingApi, "fetchScalpingTrades").mockResolvedValue({
    items: [TRADE_ANOMALY],
  });

  render(wrap(<ScalpingRoute />));

  // The anomaly row's null entry/exit/slippage/pnl all render "n/a".
  await waitFor(() =>
    expect(screen.getAllByText("이상치").length).toBeGreaterThanOrEqual(1),
  );
  expect(screen.getAllByText("n/a").length).toBeGreaterThan(0);
});
