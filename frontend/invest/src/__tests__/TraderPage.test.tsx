import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, expect, test, vi } from "vitest";

import { TraderPage } from "../trader/TraderPage";
import type {
  TraderFillsResponse,
  TraderOpenOrdersResponse,
  TraderWatchesResponse,
} from "../trader/types";

const fetchMock = vi.fn();

const openOrdersResponse: TraderOpenOrdersResponse = {
  as_of: "2026-09-28T03:00:00Z",
  data_state: "degraded",
  count: 2,
  warnings: ["toss/kr: RuntimeError"],
  empty_reason: null,
  cache: { ttl_seconds: 45, cached_at: "2026-09-28T03:00:00Z", hit: false },
  notes: [
    "토스 앱에서 주문한 내역은 토스 Open API로 조회되지 않습니다.",
  ],
  sources: [
    {
      broker: "kis",
      market: "kr",
      status: "ok",
      count: 1,
      message: null,
      fetched_at: "2026-09-28T03:00:00Z",
      last_ok_at: "2026-09-28T03:00:00Z",
    },
    {
      broker: "toss",
      market: "kr",
      status: "unavailable",
      count: 0,
      message: "RuntimeError",
      fetched_at: "2026-09-28T03:00:00Z",
      last_ok_at: "2026-09-28T02:30:00Z",
    },
    {
      broker: "upbit",
      market: "crypto",
      status: "ok",
      count: 1,
      message: null,
      fetched_at: "2026-09-28T03:00:00Z",
      last_ok_at: "2026-09-28T03:00:00Z",
    },
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
      ordered_at: "2026-09-28T09:01:00+09:00",
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
      ordered_at: "2026-09-28T00:01:00Z",
      order_no: "UP123456789",
      exchange: "UPBIT",
      currency: "KRW",
    },
  ],
};

const fillsResponse: TraderFillsResponse = {
  day_kst: "2026-09-28",
  window_start: "2026-09-28T00:00:00+09:00",
  window_end: "2026-09-29T00:00:00+09:00",
  count: 1,
  data_state: "fresh",
  empty_reason: null,
  notes: ["토스 체결은 Toss fill poller(#824) 활성화 전까지 누락됩니다."],
  items: [
    {
      id: 1,
      broker: "kis",
      account_mode: "live",
      venue: "KRX",
      instrument_type: "equity_kr",
      symbol: "005930",
      raw_symbol: "005930",
      side: "buy",
      broker_order_id: "K123456789",
      fill_seq: 1,
      filled_qty: "2",
      filled_price: "70000",
      filled_notional: "140000",
      fee_amount: null,
      fee_currency: null,
      filled_at: "2026-09-28T09:05:00+09:00",
      currency: "KRW",
      correlation_id: null,
      source: "websocket",
      source_run_id: "run-1",
      created_at: "2026-09-28T09:05:00+09:00",
      updated_at: "2026-09-28T09:05:00+09:00",
      symbol_name: "삼성전자",
      trade_day_kst: "2026-09-28",
    },
  ],
};

const watchesResponse: TraderWatchesResponse = {
  as_of: "2026-09-28T03:00:00Z",
  count: 1,
  items: [
    {
      alert_uuid: "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
      market: "kr",
      symbol: "005930",
      intent: "buy_dip",
      metric: "price",
      operator: "below",
      threshold: "65000",
      threshold_high: null,
      valid_until: "2026-10-01T00:00:00+09:00",
      action_mode: "notify",
      status: "active",
    },
  ],
};

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

function mockAllOk() {
  fetchMock.mockImplementation((url: string) => {
    if (url.startsWith("/trading/api/trader/open-orders"))
      return Promise.resolve(jsonResponse(openOrdersResponse));
    if (url.startsWith("/trading/api/trader/fills/today"))
      return Promise.resolve(jsonResponse(fillsResponse));
    if (url.startsWith("/trading/api/trader/watches"))
      return Promise.resolve(jsonResponse(watchesResponse));
    return Promise.resolve(jsonResponse({}, 404));
  });
}

beforeEach(() => {
  fetchMock.mockReset();
  vi.stubGlobal("fetch", fetchMock);
});

afterEach(() => {
  vi.unstubAllGlobals();
});

test("open orders panel renders per-broker sections and never blanks a failed broker", async () => {
  mockAllOk();
  render(<TraderPage />);

  const panel = await screen.findByTestId("panel-open-orders");

  // Failed broker shows 'unavailable' with last successful read — not 'no orders'.
  const unavailable = await within(panel).findByTestId("unavailable-toss");
  expect(unavailable).toHaveTextContent("미체결 조회 불가");
  expect(unavailable).toHaveTextContent("마지막 성공 조회");
  expect(within(panel).queryByText("미체결 주문 없음")).not.toBeInTheDocument();

  // Healthy brokers render their rows.
  expect(within(panel).getByTestId("broker-kis")).toHaveTextContent("삼성전자");
  expect(within(panel).getByTestId("broker-upbit")).toHaveTextContent("KRW-BTC");

  // Toss app-order visibility disclaimer is shown.
  const note = within(panel).getByTestId("toss-visibility-note");
  expect(note).toHaveTextContent("토스 앱에서 주문한 내역은 토스 Open API로 조회되지 않습니다");

  // Cache TTL is displayed.
  expect(panel).toHaveTextContent("캐시 TTL 45s");
});

test("fills panel shows KST day, fill rows, and the toss poller gap note", async () => {
  mockAllOk();
  render(<TraderPage />);

  const panel = await screen.findByTestId("panel-fills");
  await within(panel).findByText("삼성전자");

  expect(panel).toHaveTextContent("오늘의 체결 (KST · 2026-09-28)");
  expect(panel).toHaveTextContent("140,000 KRW");
  expect(within(panel).getByTestId("toss-fills-note")).toHaveTextContent(
    "Toss fill poller(#824)",
  );
});

test("watches panel lists intent, symbol, threshold, and valid_until", async () => {
  mockAllOk();
  render(<TraderPage />);

  const panel = await screen.findByTestId("panel-watches");
  await within(panel).findByText("buy_dip");

  expect(panel).toHaveTextContent("활성 감시 (1)");
  expect(panel).toHaveTextContent("005930");
  expect(panel).toHaveTextContent("price ≤");
  expect(panel).toHaveTextContent("65,000");
  expect(within(panel).getByText("유효기한")).toBeInTheDocument();
});

test("manual refresh calls the open-orders API with refresh=1", async () => {
  mockAllOk();
  const user = userEvent.setup();
  render(<TraderPage />);

  const panel = await screen.findByTestId("panel-open-orders");
  await within(panel).findByTestId("broker-kis");

  const button = within(panel).getByRole("button", { name: "새로고침" });
  await user.click(button);

  await waitFor(() => {
    expect(
      fetchMock.mock.calls.some(
        ([url]) => url === "/trading/api/trader/open-orders?refresh=1",
      ),
    ).toBe(true);
  });
});

test("page renders no order/approval/watch mutation controls", async () => {
  mockAllOk();
  render(<TraderPage />);
  await screen.findByTestId("panel-watches");

  const buttons = screen.getAllByRole("button").map((b) => b.textContent);
  expect(buttons.every((b) => b === "새로고침" || b === "조회 중…")).toBe(true);
  expect(screen.queryByRole("button", { name: /주문|취소|승인|거절|등록|해지/ }))
    .toBeNull();
});
