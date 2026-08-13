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
  it("builds a path from broker + market + ledgerId", () => {
    expect(orderDetailPath(makeOrder())).toBe("/orders/kis/kr/42");
  });

  it("returns null when broker is missing", () => {
    expect(orderDetailPath(makeOrder({ broker: null }))).toBeNull();
  });

  // verify-r1 BLOCKER-1 regression: market must be part of the key. Before
  // the fix this returned "/orders/kis/42" for BOTH a KR order and a US
  // order placed via the KIS broker — two different ledger tables with
  // independent id sequences, so the same path collided on ledger_id.
  it("returns null when market is missing (would otherwise collide across ledgers)", () => {
    expect(orderDetailPath(makeOrder({ market: null }))).toBeNull();
  });

  it("returns null when ledgerId is 0/falsy", () => {
    expect(orderDetailPath(makeOrder({ ledgerId: 0 }))).toBeNull();
  });

  it("builds distinct paths for the same broker+ledgerId under different markets", () => {
    const krOrder = makeOrder({ broker: "kis", market: "kr", ledgerId: 42 });
    const usOrder = makeOrder({ broker: "kis", market: "us", ledgerId: 42 });
    expect(orderDetailPath(krOrder)).not.toBe(orderDetailPath(usOrder));
    expect(orderDetailPath(krOrder)).toBe("/orders/kis/kr/42");
    expect(orderDetailPath(usOrder)).toBe("/orders/kis/us/42");
  });
});

describe("LinkedOrderRow deep link (INVEST-WATCH-UI §57차 item ②)", () => {
  it("renders the row as a link to the order detail page when broker+market+ledgerId resolve", () => {
    render(
      <MemoryRouter>
        <LinkedOrderRow order={makeOrder()} />
      </MemoryRouter>,
    );
    const link = screen.getByRole("link");
    expect(link).toHaveAttribute("href", "/orders/kis/kr/42");
  });

  it("renders a plain (non-link) row when market is missing", () => {
    render(
      <MemoryRouter>
        <LinkedOrderRow order={makeOrder({ market: null })} />
      </MemoryRouter>,
    );
    expect(screen.queryByRole("link")).toBeNull();
  });
});
