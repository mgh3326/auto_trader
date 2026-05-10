import { afterEach, expect, test, vi } from "vitest";
import {
  fetchStockDetail,
  fetchStockDetailCandles,
  fetchStockDetailNews,
  fetchStockDetailOrders,
} from "../api/stockDetail";

afterEach(() => {
  vi.unstubAllGlobals();
});

test("stock detail API helpers call canonical read-only endpoints with credentials", async () => {
  const fetchMock = vi.fn().mockResolvedValue({ ok: true, json: async () => ({}) });
  vi.stubGlobal("fetch", fetchMock);

  await fetchStockDetail({ market: "us", symbol: "QQQM" });
  await fetchStockDetailCandles({ market: "us", symbol: "QQQM", period: "1d" });
  await fetchStockDetailNews({ market: "us", symbol: "QQQM", limit: 5 });
  await fetchStockDetailOrders({ market: "us", symbol: "QQQM" });

  expect(fetchMock).toHaveBeenNthCalledWith(1, "/invest/api/stock-detail/us/QQQM", { credentials: "include" });
  expect(fetchMock).toHaveBeenNthCalledWith(2, "/invest/api/stock-detail/us/QQQM/candles?period=1d", {
    credentials: "include",
  });
  expect(fetchMock).toHaveBeenNthCalledWith(3, "/invest/api/stock-detail/us/QQQM/news?limit=5", {
    credentials: "include",
  });
  expect(fetchMock).toHaveBeenNthCalledWith(4, "/invest/api/stock-detail/us/QQQM/orders", {
    credentials: "include",
  });
});
