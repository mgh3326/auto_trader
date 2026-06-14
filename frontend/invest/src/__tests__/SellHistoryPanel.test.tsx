import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { SellHistoryPanel } from "../components/my/SellHistoryPanel";

const fetchMock = vi.fn();

const baseResponse = {
  count: 1,
  data_state: "fresh",
  empty_reason: null,
  source_breakdown: { reconciler: 1, websocket: 0, manual_import: 0 },
  items: [
    {
      id: 5,
      broker: "kis",
      account_mode: "live",
      venue: "krx",
      instrument_type: "equity_kr",
      symbol: "000660",
      raw_symbol: "000660",
      symbol_name: "SK하이닉스",
      side: "sell",
      broker_order_id: "0006421200",
      fill_seq: 733331392,
      filled_qty: "1.00000000",
      filled_price: "1959000.00000000",
      filled_notional: "1959000.0000",
      fee_amount: "0.0",
      fee_currency: "KRW",
      filled_at: "2026-05-12T00:01:09Z",
      currency: "KRW",
      correlation_id: null,
      source: "reconciler",
      source_run_id: "run-1",
      created_at: "2026-05-13T07:20:32Z",
      updated_at: null,
      cost_basis_notional: "1500000.0000",
      realized_profit: "459000.0000",
      realized_profit_rate: "30.6000",
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

test("SellHistoryPanel renders sell ledger rows and uses include credentials", async () => {
  render(<SellHistoryPanel />);

  expect(await screen.findByText("SK하이닉스")).toBeInTheDocument();
  expect(screen.getByText(/000660/)).toBeInTheDocument();
  expect(screen.getByText("매도 이력")).toBeInTheDocument();
  expect(screen.getAllByText("₩1,959,000").length).toBeGreaterThan(0);
  expect(screen.getByText("총 판매금액 · KRW")).toBeInTheDocument();
  expect(screen.getByText("판매수익 · KRW")).toBeInTheDocument();
  expect(screen.getAllByText("+₩459,000").length).toBeGreaterThan(0);
  expect(screen.getAllByText("수익률 +30.6%").length).toBeGreaterThan(0);
  expect(screen.getByText("보정")).toBeInTheDocument();
  expect(screen.getByText("출처 보정 1")).toBeInTheDocument();

  expect(fetchMock).toHaveBeenCalledTimes(1);
  const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
  expect(url).toContain("/trading/api/invest/fills/sell-history");
  expect(url).toContain("days=30");
  expect(init.credentials).toBe("include");
});

test("SellHistoryPanel refetches with market filter", async () => {
  render(<SellHistoryPanel />);
  await screen.findByText("SK하이닉스");

  await userEvent.click(screen.getByRole("button", { name: "국내" }));

  await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));
  const [url] = fetchMock.mock.calls[1] as [string, RequestInit];
  expect(url).toContain("market=kr");
});

test("renders backend symbol_name instead of the code", async () => {
  const customResponse = {
    ...baseResponse,
    items: [
      {
        ...baseResponse.items[0],
        symbol: "035420",
        symbol_name: "NAVER",
      },
    ],
  };
  fetchMock.mockResolvedValue({ ok: true, json: async () => customResponse });
  render(<SellHistoryPanel />);
  expect(await screen.findByText("NAVER")).toBeInTheDocument();
  // the code still appears in the secondary line
  expect(screen.getByText(/035420/)).toBeInTheDocument();
});

test("falls back to the code when symbol_name is absent", async () => {
  const customResponse = {
    ...baseResponse,
    items: [
      {
        ...baseResponse.items[0],
        symbol: "035420",
        symbol_name: null,
      },
    ],
  };
  fetchMock.mockResolvedValue({ ok: true, json: async () => customResponse });
  render(<SellHistoryPanel />);
  const elements = await screen.findAllByText(/035420/);
  expect(elements.length).toBeGreaterThan(0);
});

