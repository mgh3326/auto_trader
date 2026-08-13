import { render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { WatchesRoute } from "../pages/WatchesRoute";
import * as watchesApi from "../api/watches";
import { AccountPanelProvider } from "../desktop/AccountPanelProvider";
import { mockRightRail } from "../test/mockRightRail";
import type { WatchesResponse } from "../types/watches";

function setWidth(w: number) {
  Object.defineProperty(window, "innerWidth", { writable: true, configurable: true, value: w });
}

function wrap(ui: React.ReactElement, initialPath = "/invest/watches") {
  return (
    <AccountPanelProvider>
      <MemoryRouter basename="/invest" initialEntries={[initialPath]}>{ui}</MemoryRouter>
    </AccountPanelProvider>
  );
}

const READY: WatchesResponse = {
  market: "all",
  status: "active",
  count: 2,
  data_state: "ok",
  as_of: "2026-08-13T00:00:00Z",
  items: [
    {
      alert_uuid: "a1",
      source_report_uuid: "r1",
      market: "kr",
      symbol: "005930",
      symbol_name: "삼성전자",
      target_kind: "asset",
      metric: "price_below",
      operator: "below",
      threshold: "65000",
      threshold_high: null,
      status: "active",
      valid_until: "2026-09-01T00:00:00Z",
      intent: "buy_review",
      action_mode: "notify_only",
      rationale: "저점 매수",
      trigger_checklist: [],
      max_action: { side: "buy", quantity: 10 },
      current_price: "70000",
      proximity_band: "within_1_pct",
      last_event: null,
      near_expiry: false,
    },
    {
      alert_uuid: "a2",
      source_report_uuid: "r1",
      market: "kr",
      symbol: "005930",
      symbol_name: "삼성전자",
      target_kind: "asset",
      metric: "price_below",
      operator: "below",
      threshold: "60000",
      threshold_high: null,
      status: "active",
      valid_until: "2026-09-01T00:00:00Z",
      intent: "buy_review",
      action_mode: "notify_only",
      rationale: "2차 저점 매수",
      trigger_checklist: [],
      max_action: {},
      current_price: "70000",
      proximity_band: "outside",
      last_event: null,
      near_expiry: false,
    },
  ],
  warnings: [],
  empty_reason: null,
};

beforeEach(() => {
  localStorage.clear();
  mockRightRail();
  vi.spyOn(watchesApi, "fetchWatches").mockResolvedValue(READY);
});

afterEach(() => vi.restoreAllMocks());

describe("WatchesRoute responsive dispatch", () => {
  it("renders the desktop shell at >= 900px", async () => {
    setWidth(1280);
    render(wrap(<WatchesRoute />));
    expect(screen.getByTestId("desktop-shell")).toBeInTheDocument();
    expect(screen.queryByTestId("mobile-shell")).toBeNull();
    await waitFor(() => expect(screen.getByTestId("watch-group-list")).toBeInTheDocument());
  });

  it("renders the mobile shell below 900px", async () => {
    setWidth(600);
    render(wrap(<WatchesRoute />));
    expect(screen.getByTestId("mobile-shell")).toBeInTheDocument();
    expect(screen.queryByTestId("desktop-shell")).toBeNull();
    await waitFor(() => expect(screen.getByTestId("watch-group-list")).toBeInTheDocument());
  });
});

describe("WatchesRoute ladder grouping", () => {
  it("groups two watch rungs for the same symbol into a single card", async () => {
    setWidth(1280);
    render(wrap(<WatchesRoute />));

    await waitFor(() => expect(screen.getAllByTestId("watch-group-card")).toHaveLength(1));
    expect(screen.getAllByTestId("watch-ladder-rung")).toHaveLength(2);
    expect(screen.getByText(/2단계 래더/)).toBeInTheDocument();
  });

  it("shows an execution-plan badge only for the rung that carries max_action", async () => {
    setWidth(1280);
    render(wrap(<WatchesRoute />));

    await waitFor(() => expect(screen.getAllByTestId("watch-ladder-rung")).toHaveLength(2));
    expect(screen.getAllByText("실행플랜")).toHaveLength(1);
  });
});

// verify-r1 SHOULD-1: build_watches_url() (app/core/invest_deep_links.py)
// generates ?market=&status=&symbol=, but the page used to ignore all three
// — a symbol-scoped "워치 카드 → watches" deep link landed on the unscoped
// full list instead. These assert the page actually reads them.
describe("WatchesRoute deep-link query params (verify-r1 SHOULD-1)", () => {
  it("seeds the market/status filters from the URL and forwards symbol scope to the fetch", async () => {
    setWidth(1280);
    render(wrap(<WatchesRoute />, "/invest/watches?market=us&status=all&symbol=AAPL"));

    await waitFor(() => expect(watchesApi.fetchWatches).toHaveBeenCalledWith("us", "all", "AAPL"));
    // toggle buttons reflect the seeded state, not the component defaults
    expect(screen.getByRole("button", { name: "미국" })).toHaveStyle({ color: "var(--bg)" });
    // "AAPL" renders inside a <strong>, splitting the text node — match on
    // the status region's full textContent instead of a single text node.
    expect(screen.getByRole("status").textContent).toMatch(/AAPL 종목으로 범위가 좁혀졌습니다/);
  });

  it("falls back to defaults when the URL carries an unrecognized market/status value", async () => {
    setWidth(1280);
    render(wrap(<WatchesRoute />, "/invest/watches?market=bogus&status=nonsense"));

    await waitFor(() => expect(watchesApi.fetchWatches).toHaveBeenCalledWith("all", "active", undefined));
  });

  it("omits the symbol scope banner and passes undefined when no symbol param is present", async () => {
    setWidth(1280);
    render(wrap(<WatchesRoute />));

    await waitFor(() => expect(watchesApi.fetchWatches).toHaveBeenCalledWith("all", "active", undefined));
    expect(screen.queryByText(/범위가 좁혀졌습니다/)).toBeNull();
  });
});
