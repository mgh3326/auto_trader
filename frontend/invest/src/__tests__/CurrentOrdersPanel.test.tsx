import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, expect, test, vi } from "vitest";

import { CurrentOrdersPanel } from "../components/my/CurrentOrdersPanel";

const fetchMock = vi.fn();

const baseResponse = {
  market: "all",
  count: 2,
  data_state: "degraded",
  as_of: "2026-06-15T00:00:00Z",
  warnings: ["kis/us: NYSE=RuntimeError: down"],
  empty_reason: null,
  sources: [
    { broker: "kis", market: "kr", status: "ok", fetched_at: "2026-06-15T00:00:00Z", count: 1, message: null },
    { broker: "kis", market: "us", status: "degraded", fetched_at: "2026-06-15T00:00:00Z", count: 0, message: "NYSE=RuntimeError: down" },
  ],
  items: [
    {
      broker: "kis",
      market: "kr",
      symbol: "005930",
      symbol_name: "삼성전자",
      side: "buy",
      order_type: "지정가",
      time_in_force: null,
      price: "70000",
      quantity: "10",
      remaining_qty: "8",
      filled_qty: "2",
      status: "pending",
      raw_status: "접수",
      ordered_at: "2026-06-15T09:01:00+09:00",
      order_no: "K123456789",
      exchange: "KRX",
      currency: "KRW",
    },
    {
      broker: "upbit",
      market: "crypto",
      symbol: "KRW-BTC",
      symbol_name: null,
      side: "sell",
      order_type: "limit",
      time_in_force: null,
      price: "99000000",
      quantity: "0.02",
      remaining_qty: "0.02",
      filled_qty: null,
      status: "pending",
      raw_status: "wait",
      ordered_at: "2026-06-15T00:01:00Z",
      order_no: "UP123456789",
      exchange: "UPBIT",
      currency: "KRW",
    },
  ],
};

beforeEach(() => {
  fetchMock.mockReset();
  fetchMock.mockResolvedValue({ ok: true, json: async () => baseResponse });
  vi.stubGlobal("fetch", fetchMock);
});

afterEach(() => {
  vi.unstubAllGlobals();
});

test("CurrentOrdersPanel renders broker rows and degraded source warning", async () => {
  render(<CurrentOrdersPanel />);

  expect(await screen.findByText("삼성전자")).toBeInTheDocument();
  expect(screen.getByText("KRW-BTC")).toBeInTheDocument();
  expect(screen.getByText("현재 주문")).toBeInTheDocument();
  expect(screen.getByText("KIS")).toBeInTheDocument();
  expect(screen.getByText("UPBIT")).toBeInTheDocument();
  expect(screen.getByText(/kis\/us/)).toBeInTheDocument();
  expect(screen.getAllByText("미체결").length).toBeGreaterThan(0);
  expect(screen.queryByRole("button", { name: /취소|정정/ })).not.toBeInTheDocument();
});

test("CurrentOrdersPanel refetches with market filter", async () => {
  render(<CurrentOrdersPanel />);
  await screen.findByText("삼성전자");

  await userEvent.click(screen.getByRole("button", { name: "코인" }));

  await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));
  expect(String(fetchMock.mock.calls[1]?.[0])).toContain("market=crypto");
});

test("CurrentOrdersPanel renders empty reason", async () => {
  fetchMock.mockResolvedValue({
    ok: true,
    json: async () => ({
      ...baseResponse,
      count: 0,
      data_state: "ok",
      items: [],
      warnings: [],
      empty_reason: "no open orders for the selected market",
    }),
  });

  render(<CurrentOrdersPanel compact />);

  expect(await screen.findByText("no open orders for the selected market")).toBeInTheDocument();
});

import { MemoryRouter } from "react-router-dom";
import { PORTFOLIO_TABS, usePortfolioTabSearchParam } from "../components/my/portfolioTabs";

function TabProbe() {
  const [activeTab, setActiveTab] = usePortfolioTabSearchParam();
  return (
    <>
      <div data-testid="active-tab">{activeTab}</div>
      <button type="button" onClick={() => setActiveTab("currentOrders")}>set current</button>
    </>
  );
}

test("portfolio tabs include current orders and parse the search param", async () => {
  expect(PORTFOLIO_TABS.map((tab) => tab.key)).toContain("currentOrders");
  render(
    <MemoryRouter basename="/invest" initialEntries={["/invest/my?tab=currentOrders"]}>
      <TabProbe />
    </MemoryRouter>,
  );
  expect(screen.getByTestId("active-tab")).toHaveTextContent("currentOrders");
});

