import { render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import * as orderDetailApi from "../api/orderDetail";
import { OrderDetailNotFoundError, OrderDetailUnknownLedgerError } from "../api/orderDetail";
import { AccountPanelProvider } from "../desktop/AccountPanelProvider";
import { OrderDetailRoute } from "../pages/OrderDetailRoute";
import { mockRightRail } from "../test/mockRightRail";
import type { LinkedOrder } from "../types/investmentReports";

function setWidth(w: number) {
  Object.defineProperty(window, "innerWidth", { writable: true, configurable: true, value: w });
}

function wrap(initialPath: string) {
  return (
    <AccountPanelProvider>
      <MemoryRouter basename="/invest" initialEntries={[initialPath]}>
        <Routes>
          <Route path="/orders/:broker/:market/:ledgerId" element={<OrderDetailRoute />} />
        </Routes>
      </MemoryRouter>
    </AccountPanelProvider>
  );
}

const ORDER: LinkedOrder = {
  broker: "kis",
  accountScope: "kis_live",
  market: "kr",
  orderNo: "0001234500",
  ledgerId: 42,
  symbol: "005930",
  side: "buy",
  status: "filled",
  filledQty: "10",
  avgFillPrice: "70000",
  orderTime: "093015",
  reconciledAt: "2026-05-10T09:05:00Z",
  exitReason: null,
  thesis: "실적 발표 전 저점 매수",
  reportItemUuid: null,
};

beforeEach(() => {
  localStorage.clear();
  mockRightRail();
});

afterEach(() => vi.restoreAllMocks());

describe("OrderDetailRoute responsive dispatch", () => {
  it("renders the desktop shell at >= 900px", async () => {
    vi.spyOn(orderDetailApi, "fetchOrderDetail").mockResolvedValue(ORDER);
    setWidth(1280);
    render(wrap("/invest/orders/kis/kr/42"));
    expect(screen.getByTestId("desktop-shell")).toBeInTheDocument();
    await waitFor(() => expect(screen.getByTestId("order-detail-card")).toBeInTheDocument());
    expect(screen.getByText("실적 발표 전 저점 매수")).toBeInTheDocument();
  });

  it("renders the mobile shell below 900px", async () => {
    vi.spyOn(orderDetailApi, "fetchOrderDetail").mockResolvedValue(ORDER);
    setWidth(600);
    render(wrap("/invest/orders/kis/kr/42"));
    expect(screen.getByTestId("mobile-shell")).toBeInTheDocument();
    await waitFor(() => expect(screen.getByTestId("order-detail-card")).toBeInTheDocument());
  });
});

describe("OrderDetailRoute not-found handling", () => {
  it("shows a not-found message on 404 instead of crashing", async () => {
    vi.spyOn(orderDetailApi, "fetchOrderDetail").mockRejectedValue(
      new OrderDetailNotFoundError("order not found"),
    );
    setWidth(1280);
    render(wrap("/invest/orders/kis/kr/999"));

    await waitFor(() => expect(screen.getByText("해당 주문을 찾을 수 없습니다.")).toBeInTheDocument());
    expect(screen.queryByTestId("order-detail-card")).toBeNull();
  });

  // verify-r1 BLOCKER-1 regression: an unrecognized (broker, market) combo
  // must fail closed (400 -> not-found UI), never silently fall through to
  // whichever ledger table happens to be the "else" branch.
  it("shows a not-found message on 400 (unknown broker+market combination)", async () => {
    vi.spyOn(orderDetailApi, "fetchOrderDetail").mockRejectedValue(
      new OrderDetailUnknownLedgerError("unknown ledger combination"),
    );
    setWidth(1280);
    render(wrap("/invest/orders/upbit/kr/1"));

    await waitFor(() => expect(screen.getByText("해당 주문을 찾을 수 없습니다.")).toBeInTheDocument());
    expect(screen.queryByTestId("order-detail-card")).toBeNull();
  });
});
