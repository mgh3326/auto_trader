// ROB-559 — per-symbol order history card on the stock detail page.

import { render, screen } from "@testing-library/react";
import type { ReactElement } from "react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it } from "vitest";

import { OrderLedgerCard } from "../desktop/stock-detail/OrderLedgerCard";
import type { LinkedOrder } from "../types/investmentReports";

// INVEST-WATCH-UI §57차 item ②: LinkedOrderRow now links to the standalone
// order detail page, so it needs a Router in the tree even in this
// stock-detail-page test.
function renderWithRouter(ui: ReactElement) {
  return render(<MemoryRouter>{ui}</MemoryRouter>);
}

function makeOrder(overrides: Partial<LinkedOrder> = {}): LinkedOrder {
  return {
    ledgerId: 11,
    broker: "upbit",
    accountScope: "upbit_live",
    market: "crypto",
    orderNo: "7aeb17dd-2fa2-4dc0",
    symbol: "KRW-BTC",
    side: "buy",
    status: "filled",
    filledQty: "0.01",
    avgFillPrice: "96180000",
    ...overrides,
  };
}

describe("OrderLedgerCard (ROB-559)", () => {
  it("renders the 주문 기록 card with status badge + order id for an order", () => {
    renderWithRouter(<OrderLedgerCard orders={[makeOrder()]} />);
    expect(screen.getByText("주문 기록")).toBeInTheDocument();
    expect(screen.getByText("체결")).toBeInTheDocument();
    expect(screen.getByText(/order 7aeb17dd/)).toBeInTheDocument();
  });

  it("renders a 미체결 badge and no qty line for an unfilled order", () => {
    renderWithRouter(
      <OrderLedgerCard
        orders={[
          makeOrder({ status: "accepted", filledQty: null, avgFillPrice: null }),
        ]}
      />,
    );
    expect(screen.getByText("미체결")).toBeInTheDocument();
    expect(screen.queryByText(/@/)).toBeNull(); // no redundant "— @ —" line
  });

  it("formats tiny fill quantities without scientific notation", () => {
    renderWithRouter(<OrderLedgerCard orders={[makeOrder({ filledQty: "1E-8" })]} />);
    expect(screen.getByText(/0\.00000001/)).toBeInTheDocument();
  });

  it("renders the empty state when there are no orders", () => {
    render(<OrderLedgerCard orders={[]} />);
    expect(screen.getByText("주문 기록이 없습니다.")).toBeInTheDocument();
  });

  it("renders the loading state while undefined", () => {
    render(<OrderLedgerCard orders={undefined} />);
    expect(screen.getByText("불러오는 중입니다…")).toBeInTheDocument();
  });
});
