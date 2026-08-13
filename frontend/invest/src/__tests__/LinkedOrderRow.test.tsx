import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it } from "vitest";

import { LinkedOrderRow, orderDetailPath } from "../components/orders/LinkedOrderRow";
import type { LinkedOrder } from "../types/investmentReports";

function makeOrder(overrides: Partial<LinkedOrder> = {}): LinkedOrder {
  return {
    ledgerId: 42,
    broker: "kis",
    accountScope: "kis_live",
    market: "kr",
    orderNo: "0001234500",
    symbol: "005930",
    side: "buy",
    status: "filled",
    filledQty: "10",
    avgFillPrice: "70000",
    ...overrides,
  };
}

describe("orderDetailPath", () => {
  it("builds a path from broker + ledgerId", () => {
    expect(orderDetailPath(makeOrder())).toBe("/orders/kis/42");
  });

  it("returns null when broker is missing", () => {
    expect(orderDetailPath(makeOrder({ broker: null }))).toBeNull();
  });

  it("returns null when ledgerId is 0/falsy", () => {
    expect(orderDetailPath(makeOrder({ ledgerId: 0 }))).toBeNull();
  });
});

describe("LinkedOrderRow deep link (INVEST-WATCH-UI §57차 item ②)", () => {
  it("renders the row as a link to the order detail page when broker+ledgerId resolve", () => {
    render(
      <MemoryRouter>
        <LinkedOrderRow order={makeOrder()} />
      </MemoryRouter>,
    );
    const link = screen.getByRole("link");
    expect(link).toHaveAttribute("href", "/orders/kis/42");
  });
});
