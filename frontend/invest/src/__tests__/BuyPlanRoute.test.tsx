// §144차 — /invest/buy-plan renders the funding verdict the operator acts on.
//
// The assertions target the sentences that change behaviour ("입금 필요",
// "리저브 충분", 자동승인 vs 카드, 게이트 판정 불가) rather than layout, since
// a wrong label here is what would cause a wrong cash transfer.
import { render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import * as buyPlanApi from "../api/buyPlan";
import { BuyPlanRoute } from "../pages/BuyPlanRoute";
import { AccountPanelProvider } from "../desktop/AccountPanelProvider";
import { mockRightRail } from "../test/mockRightRail";
import type { BuyPlanResponse } from "../types/buyPlan";

function setWidth(w: number) {
  Object.defineProperty(window, "innerWidth", {
    writable: true,
    configurable: true,
    value: w,
  });
}

function wrap(ui: React.ReactElement) {
  return (
    <AccountPanelProvider>
      <MemoryRouter basename="/invest" initialEntries={["/invest/buy-plan"]}>
        {ui}
      </MemoryRouter>
    </AccountPanelProvider>
  );
}

const READY: BuyPlanResponse = {
  as_of: "2026-08-23T03:00:00Z",
  policy: { version: "2026-08-23.1", content_hash: "abcdef0123456789" },
  cache_ttl_seconds: 180,
  approximation_notice: "정책 산식의 표시용 근사입니다.",
  market: "all",
  averaging_triggers: [
    {
      market: "crypto",
      symbol: "XRP",
      symbol_name: "리플",
      currency: "KRW",
      account_sources: ["upbit"],
      quantity: "1000",
      average_price: "1100",
      cost_basis: "1100000",
      current_price: "1050",
      unrealized_pnl_pct: "-4.55",
      k: "0.1",
      turn_point_price: "1000",
      distance_to_turn_point_pct: "5",
      turn_point_reached: false,
      samples: [
        {
          offset_from_turn_point_pct: "-1",
          price: "990",
          additional_notional: "110000",
          target_average_price: "1089",
          approval_lane: "auto_submit",
          approval_lane_reason: "within_tier_auto_submit_notional",
        },
        {
          offset_from_turn_point_pct: "-3",
          price: "970",
          additional_notional: "340000",
          target_average_price: "1067",
          approval_lane: "human_card",
          approval_lane_reason: "above_tier_auto_submit_notional",
        },
      ],
      reserve_plan_notional: "340000",
      market_rank: 1,
      within_policy_add_cap: true,
      notes: [],
    },
  ],
  support_net: {
    policy_key: "buy.held_majors_support_net",
    enabled: true,
    currency: "KRW",
    tier_cap_notional: "900000",
    per_symbol_cap_notional: "300000",
    placed_notional: "110000",
    remaining_notional: "790000",
    distance_band_pct: ["-12", "-3"],
    review_date: "2026-09-19",
    rows: [
      {
        market: "crypto",
        symbol: "SOL",
        symbol_name: "솔라나",
        currency: "KRW",
        quantity: "10",
        average_price: "150000",
        current_price: "180000",
        unrealized_pnl_pct: "20",
        eligible: true,
        ineligible_reason: null,
        placements: [
          {
            form: "resting_order",
            reference: "upbit:o-1",
            anchor_price: "165000",
            quantity: "0.66",
            notional: "110000",
            distance_from_current_pct: "-8.33",
            valid_until: null,
            within_policy_distance_band: true,
          },
        ],
        placed_notional: "110000",
        per_symbol_cap_notional: "300000",
        remaining_headroom_notional: "190000",
      },
    ],
    notes: [],
  },
  active_buy_watches: [
    {
      market: "kr",
      symbol: "005930",
      symbol_name: "삼성전자",
      currency: "KRW",
      alert_uuid: "a1",
      metric: "price_below",
      operator: "below",
      threshold: "65000",
      threshold_high: null,
      current_price: "70000",
      distance_to_threshold_pct: "-7.14",
      valid_until: "2026-09-01T00:00:00Z",
      near_expiry: false,
      planned_notional: "650000",
      planned_notional_source: "max_action.quantity × 트리거 레벨",
      approval_lane: "human_card",
      approval_lane_reason: "above_tier_auto_submit_notional",
    },
  ],
  discovery_gates: [
    {
      market: "crypto",
      gate_key: "market_rules.crypto.recovery_gate",
      state: "indeterminate",
      min_conditions_met: 2,
      of: 2,
      met_count: 1,
      unavailable_count: 1,
      semantics: "reserve deployment recovery frame",
      conditions: [
        {
          condition_id: "alt_breadth_24h",
          metric: "upbit_alt_breadth_24h",
          comparison: "gt",
          threshold: "50",
          unit: "percent",
          current_value: null,
          state: "unavailable",
          source: "upbit_open_api_ticker_derived",
          note: "조회 실패",
        },
        {
          condition_id: "btc_long_short_ratio",
          metric: "btc_long_short_ratio",
          comparison: "lte",
          threshold: "1.5",
          unit: "ratio",
          current_value: "1.2",
          state: "met",
          source: "binance",
          note: null,
        },
      ],
      notes: ["미확인 조건은 충족으로 세지 않습니다."],
    },
  ],
  funding: {
    accounts: [
      {
        account_id: "upbit-1",
        display_name: "업비트",
        source: "upbit",
        currency: "KRW",
        available_cash: "200000",
        available_cash_source: "cashBalances",
        included_in_reserve: true,
      },
    ],
    currencies: [
      {
        currency: "KRW",
        available_cash: "200000",
        required_averaging_adds: "340000",
        required_support_net: "0",
        required_active_watches: "650000",
        required_total: "990000",
        verdict: "shortfall",
        shortfall: "790000",
        notes: ["이미 걸린 지정가는 브로커가 현금을 묶고 있어 합계에서 제외했습니다."],
      },
      {
        currency: "USD",
        available_cash: "1000",
        required_averaging_adds: "0",
        required_support_net: "0",
        required_active_watches: "0",
        required_total: "0",
        verdict: "sufficient",
        shortfall: "0",
        notes: [],
      },
    ],
  },
  value_sources: [
    { field: "averaging_triggers.*", source: "InvestHomeService + policy", note: null },
  ],
  warnings: [],
};

describe("BuyPlanRoute", () => {
  beforeEach(() => {
    mockRightRail();
    setWidth(1400);
    window.localStorage.clear();
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("leads with the deposit amount when cash falls short", async () => {
    vi.spyOn(buyPlanApi, "fetchBuyPlan").mockResolvedValue(READY);
    render(wrap(<BuyPlanRoute />));

    await waitFor(() => expect(screen.getByText("₩790,000 입금 필요")).toBeTruthy());
    expect(screen.getAllByText("입금 필요").length).toBeGreaterThan(0);
    expect(screen.getAllByText("리저브 충분").length).toBeGreaterThan(0);
  });

  it("shows the turn point and both sampled cash amounts", async () => {
    vi.spyOn(buyPlanApi, "fetchBuyPlan").mockResolvedValue(READY);
    render(wrap(<BuyPlanRoute />));

    await waitFor(() => expect(screen.getByText("리플")).toBeTruthy());
    expect(screen.getByText("전환점 P* (k=0.1)")).toBeTruthy();
    expect(screen.getByText("₩1,000")).toBeTruthy();
    expect(screen.getAllByText("₩110,000").length).toBeGreaterThan(0);
    expect(screen.getAllByText("₩340,000").length).toBeGreaterThan(0);
    // The lane split must be visible per sample, not summarised away.
    expect(screen.getByText("자동승인")).toBeTruthy();
    expect(screen.getAllByText("카드(수동 승인)").length).toBeGreaterThan(0);
  });

  it("marks a resting rung as 주문 상시형", async () => {
    vi.spyOn(buyPlanApi, "fetchBuyPlan").mockResolvedValue(READY);
    render(wrap(<BuyPlanRoute />));

    await waitFor(() => expect(screen.getByText("솔라나")).toBeTruthy());
    expect(screen.getByText("주문 상시형")).toBeTruthy();
    expect(screen.getByText("이익권")).toBeTruthy();
  });

  it("renders an unreadable gate condition as 판정 불가, never as open", async () => {
    vi.spyOn(buyPlanApi, "fetchBuyPlan").mockResolvedValue(READY);
    render(wrap(<BuyPlanRoute />));

    await waitFor(() => expect(screen.getByText("판정 불가")).toBeTruthy());
    expect(screen.getAllByText("확인 불가").length).toBeGreaterThan(0);
    expect(screen.queryByText("열림")).toBeNull();
  });

  it("always states that the board is an approximation, not a verdict", async () => {
    vi.spyOn(buyPlanApi, "fetchBuyPlan").mockResolvedValue(READY);
    render(wrap(<BuyPlanRoute />));

    await waitFor(() =>
      expect(
        screen.getByText("이 화면은 판정이 아니라 자금 준비용 근사입니다"),
      ).toBeTruthy(),
    );
    expect(
      screen.getByText(/이 화면은 주문·제안·워치를 만들거나 승인하지 않습니다/),
    ).toBeTruthy();
  });

  it("surfaces a fetch failure instead of rendering an empty plan", async () => {
    vi.spyOn(buyPlanApi, "fetchBuyPlan").mockRejectedValue(new Error("buy-plan 500"));
    render(wrap(<BuyPlanRoute />));

    await waitFor(() =>
      expect(screen.getByText(/불러오지 못했습니다 — buy-plan 500/)).toBeTruthy(),
    );
  });
});
