import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, expect, test, vi } from "vitest";

import { BuyHistoryPanel } from "../components/my/BuyHistoryPanel";

const fetchMock = vi.fn();

const baseResponse = {
  count: 1,
  data_state: "fresh",
  empty_reason: null,
  source_breakdown: { reconciler: 1, websocket: 0, manual_import: 0 },
  items: [
    {
      id: 7,
      broker: "kis",
      account_mode: "live",
      venue: "krx",
      instrument_type: "equity_kr",
      symbol: "005930",
      raw_symbol: "005930",
      symbol_name: "삼성전자",
      side: "buy",
      broker_order_id: "0006421201",
      fill_seq: 733331393,
      filled_qty: "2.00000000",
      filled_price: "70000.00000000",
      filled_notional: "140000.0000",
      fee_amount: "0.0",
      fee_currency: "KRW",
      filled_at: "2026-06-15T00:01:09Z",
      currency: "KRW",
      correlation_id: null,
      source: "reconciler",
      source_run_id: "run-1",
      created_at: "2026-06-15T00:02:00Z",
      updated_at: null,
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

function renderBuyHistoryPanel(compact = false) {
  return render(
    <MemoryRouter basename="/invest" initialEntries={["/invest/my?tab=buyHistory"]}>
      <BuyHistoryPanel compact={compact} />
    </MemoryRouter>,
  );
}

test("BuyHistoryPanel renders buy fills and calls side-filtered endpoint", async () => {
  renderBuyHistoryPanel();

  expect(await screen.findByText("삼성전자")).toBeInTheDocument();
  expect(screen.getByText("매수 이력")).toBeInTheDocument();
  expect(screen.getByText("총 매수금액 · KRW")).toBeInTheDocument();
  expect(screen.getAllByText("₩140,000").length).toBeGreaterThan(0);
  expect(screen.getByText("보정")).toBeInTheDocument();
  expect(screen.getByText("출처 보정 1")).toBeInTheDocument();

  expect(fetchMock).toHaveBeenCalledTimes(1);
  const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
  expect(url).toContain("/trading/api/invest/fills/recent");
  expect(url).toContain("limit=30");
  expect(url).toContain("side=buy");
  expect(init.credentials).toBe("include");
});

test("BuyHistoryPanel refetches with market filter", async () => {
  renderBuyHistoryPanel();
  await screen.findByText("삼성전자");

  await userEvent.click(screen.getByRole("button", { name: "국내" }));

  await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));
  const [url] = fetchMock.mock.calls[1] as [string, RequestInit];
  expect(url).toContain("market=kr");
  expect(url).toContain("side=buy");
});

test("BuyHistoryPanel links symbols to stock detail", async () => {
  renderBuyHistoryPanel();

  const link = await screen.findByRole("link", { name: /삼성전자/ });
  expect(link).toHaveAttribute("href", "/invest/stocks/kr/005930");
});

test("BuyHistoryPanel renders empty reason", async () => {
  fetchMock.mockResolvedValue({
    ok: true,
    json: async () => ({
      ...baseResponse,
      count: 0,
      items: [],
      empty_reason: "no buy fills in the requested window",
    }),
  });

  renderBuyHistoryPanel(true);

  expect(await screen.findByText("no buy fills in the requested window")).toBeInTheDocument();
});
