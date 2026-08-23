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
import type { BuyPlanResponse, ScopeReconciliation } from "../types/buyPlan";

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

const UPBIT_KRW_SCOPE: ScopeReconciliation = {
  scope_key: "upbit:KRW",
  broker: "upbit",
  currency: "KRW",
  account_ids: ["upbit-1"],
  available_cash: "200000",
  required_averaging_adds: "340000",
  required_support_net: "0",
  required_active_watches: "0",
  required_total: "340000",
  unattributed_same_currency: "0",
  upper_bound_if_all_unattributed_lands_here: "340000",
  requirements_complete: true,
  incomplete_reasons: [],
  verdict: "shortfall",
  shortfall: "140000",
  notes: ["이미 걸린 지정가는 브로커가 현금을 묶고 있어 합계에서 제외했습니다."],
};

const KIS_KRW_SCOPE: ScopeReconciliation = {
  scope_key: "kis:KRW",
  broker: "kis",
  currency: "KRW",
  account_ids: ["kis-1"],
  available_cash: "5000000",
  required_averaging_adds: "0",
  required_support_net: "0",
  required_active_watches: "650000",
  required_total: "650000",
  unattributed_same_currency: "0",
  upper_bound_if_all_unattributed_lands_here: "650000",
  requirements_complete: true,
  incomplete_reasons: [],
  verdict: "sufficient",
  shortfall: "0",
  notes: [],
};

const READY: BuyPlanResponse = {
  as_of: "2026-08-23T03:00:00Z",
  policy: { version: "2026-08-23.1", content_hash: "abcdef0123456789" },
  cache_ttl_seconds: 180,
  approximation_notice: "정책 산식의 표시용 근사입니다.",
  approval_context: {
    master_gate_enabled: false,
    master_gate_setting: "ORDER_PROPOSALS_AUTO_APPROVE",
    master_gate_source: "settings.ORDER_PROPOSALS_AUTO_APPROVE",
    unevaluated_conditions: [
      { code: "preview_guard_failed", label: "fresh preview 성공" },
      { code: "approval_required_tag", label: "승인 필요 태그 스캔" },
      { code: "daily_cap_exceeded", label: "일일 누적 cap 잔여" },
    ],
    evaluated_conditions: [
      { code: "per_order_cap_exceeded", label: "건당 자동승인 상한" },
    ],
    notice: "자동승인 마스터 게이트가 꺼져 있습니다 — 전건이 수동 승인 카드로 갑니다.",
  },
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
      funding_broker: "upbit",
      funding_broker_reason: null,
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
        placements_state: "ok",
        placements_incomplete_reasons: [],
      },
    ],
    placements_state: "ok",
    placements_incomplete_reasons: [],
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
      funding_broker: "kis",
      funding_broker_reason: null,
      account_mode: "kis_live",
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
        broker: "upbit",
        currency: "KRW",
        available_cash: "200000",
        available_cash_source: "cashBalances",
        included_in_reserve: true,
      },
    ],
    scopes: [UPBIT_KRW_SCOPE, KIS_KRW_SCOPE],
    unattributed: [],
    source_warnings: [],
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

  it("leads with the deposit amount of the account that is actually short", async () => {
    // verify-r1 B1: the shortfall belongs to the Upbit scope. A currency-only
    // total would have netted it against the funded KIS account and shown
    // 리저브 충분 for cash no Upbit order can spend.
    vi.spyOn(buyPlanApi, "fetchBuyPlan").mockResolvedValue(READY);
    render(wrap(<BuyPlanRoute />));

    await waitFor(() => expect(screen.getByText("₩140,000 입금 필요")).toBeTruthy());
    expect(screen.getByText("업비트 · KRW")).toBeTruthy();
    expect(screen.getByText("한국투자증권 · KRW")).toBeTruthy();
    expect(screen.getAllByText("입금 필요").length).toBeGreaterThan(0);
    expect(screen.getAllByText("리저브 충분").length).toBeGreaterThan(0);
  });

  it("never renders one broker's cash inside another broker's verdict", async () => {
    vi.spyOn(buyPlanApi, "fetchBuyPlan").mockResolvedValue(READY);
    render(wrap(<BuyPlanRoute />));

    await waitFor(() => expect(screen.getByText("업비트 · KRW")).toBeTruthy());
    // 5,200,000 would be the KRW-only aggregate of both accounts.
    expect(screen.queryByText("₩5,200,000")).toBeNull();
  });

  it("shows an unattributed requirement instead of dropping it", async () => {
    vi.spyOn(buyPlanApi, "fetchBuyPlan").mockResolvedValue({
      ...READY,
      funding: {
        ...READY.funding,
        unattributed: [
          {
            kind: "active_watch",
            label: "KRW-DOGE 워치 100",
            currency: "KRW",
            amount: "500000",
            reason: "워치 max_action이 실행 계좌를 지정하지 않았습니다.",
          },
        ],
      },
    });
    render(wrap(<BuyPlanRoute />));

    await waitFor(() =>
      expect(
        screen.getByText("어느 계좌에서 나갈 돈인지 확정하지 못한 소요액"),
      ).toBeTruthy(),
    );
    expect(screen.getByText("KRW-DOGE 워치 100")).toBeTruthy();
    expect(screen.getByText("₩500,000")).toBeTruthy();
  });

  it("shows the turn point and both sampled cash amounts", async () => {
    vi.spyOn(buyPlanApi, "fetchBuyPlan").mockResolvedValue(READY);
    render(wrap(<BuyPlanRoute />));

    await waitFor(() => expect(screen.getByText("리플")).toBeTruthy());
    expect(screen.getByText("전환점 P* (k=0.1)")).toBeTruthy();
    expect(screen.getByText("₩1,000")).toBeTruthy();
    expect(screen.getAllByText("₩110,000").length).toBeGreaterThan(0);
    expect(screen.getAllByText("₩340,000").length).toBeGreaterThan(0);
    // Both samples keep their own cap-classification reason even though the
    // master gate collapses both badges to a card.
    const reasons = screen
      .getAllByTitle(/한도/)
      .map((node) => node.getAttribute("title") ?? "");
    expect(reasons.some((r) => r.includes("티어 자동제출 한도 이내"))).toBe(true);
    expect(reasons.some((r) => r.includes("티어 자동제출 한도 초과"))).toBe(true);
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

  it("never greenlights a funded broker while money is unattributed", async () => {
    // verify-r2 B1 (a): KIS holding the cash does not make it the account the
    // order will use. Green anywhere here sends the operator past the deposit.
    vi.spyOn(buyPlanApi, "fetchBuyPlan").mockResolvedValue({
      ...READY,
      funding: {
        ...READY.funding,
        scopes: [
          {
            ...KIS_KRW_SCOPE,
            available_cash: "1000000",
            required_averaging_adds: "0",
            required_active_watches: "0",
            required_total: "0",
            unattributed_same_currency: "330000",
            upper_bound_if_all_unattributed_lands_here: "330000",
            verdict: "unknown",
            shortfall: null,
          },
        ],
        unattributed: [
          {
            kind: "active_watch",
            label: "KRW-XRP 워치 900",
            currency: "KRW",
            amount: "330000",
            reason: "워치 max_action이 실행 계좌(account_mode)를 지정하지 않았습니다.",
          },
        ],
      },
    });
    render(wrap(<BuyPlanRoute />));

    await waitFor(() =>
      expect(screen.getByText("귀속 미확정 소요가 있어 대조를 보류했습니다")).toBeTruthy(),
    );
    expect(screen.queryByText("리저브 충분")).toBeNull();
  });

  it("gives unattributed money its own destination row", async () => {
    // verify-r2 B1 (b)/(d): a need bound for a broker or currency with no cash
    // account used to reach no verdict anywhere on the board.
    vi.spyOn(buyPlanApi, "fetchBuyPlan").mockResolvedValue({
      ...READY,
      funding: {
        ...READY.funding,
        scopes: [
          {
            scope_key: "unattributed:USD",
            broker: "unattributed",
            currency: "USD",
            account_ids: [],
            available_cash: null,
            required_averaging_adds: "0",
            required_support_net: "0",
            required_active_watches: "200",
            required_total: "200",
            unattributed_same_currency: "0",
            upper_bound_if_all_unattributed_lands_here: "200",
            requirements_complete: false,
            incomplete_reasons: ["이 행은 귀속 계좌를 확정하지 못한 소요액 모음입니다"],
            verdict: "unknown",
            shortfall: null,
            notes: [],
          },
        ],
      },
    });
    render(wrap(<BuyPlanRoute />));

    await waitFor(() => expect(screen.getByText("목적지 미확정 · USD")).toBeTruthy());
    expect(
      screen.getByText("어느 계좌에서 나갈지 확정하지 못해 대조할 수 없습니다"),
    ).toBeTruthy();
    expect(screen.getAllByText("$200.00").length).toBeGreaterThan(0);
  });

  it("calls an incomplete shortfall a minimum", async () => {
    // verify-r2 SHOULD-4: the deficit is proven but is a floor.
    vi.spyOn(buyPlanApi, "fetchBuyPlan").mockResolvedValue({
      ...READY,
      funding: {
        ...READY.funding,
        scopes: [
          {
            ...UPBIT_KRW_SCOPE,
            requirements_complete: false,
            incomplete_reasons: ["워치 조회 상태 degraded"],
          },
        ],
      },
    });
    render(wrap(<BuyPlanRoute />));

    await waitFor(() =>
      expect(screen.getByText("최소 ₩140,000 입금 필요")).toBeTruthy(),
    );
    expect(
      screen.getByText(/이 금액은 하한이며 실제로는 더 필요할 수 있습니다/),
    ).toBeTruthy();
  });

  it("does not style an unreadable master gate like an enabled one", async () => {
    // verify-r2 SHOULD-2: null used to render exactly like ON.
    vi.spyOn(buyPlanApi, "fetchBuyPlan").mockResolvedValue({
      ...READY,
      approval_context: {
        ...READY.approval_context,
        master_gate_enabled: null,
        master_gate_source: "settings.ORDER_PROPOSALS_AUTO_APPROVE (읽기 실패)",
        notice: "자동승인 마스터 게이트 상태를 읽지 못했습니다.",
      },
    });
    render(wrap(<BuyPlanRoute />));

    await waitFor(() =>
      expect(screen.getByText("자동승인 게이트 상태 불명")).toBeTruthy(),
    );
    expect(
      screen.getAllByText("레인 판정 불가(게이트 상태 불명)").length,
    ).toBeGreaterThan(0);
    expect(screen.queryByText("자동승인 가능(cap 기준)")).toBeNull();
  });

  it("lists every eligibility gate it did not evaluate", async () => {
    // verify-r2 SHOULD-1: a short list still reads as a complete one.
    vi.spyOn(buyPlanApi, "fetchBuyPlan").mockResolvedValue(READY);
    render(wrap(<BuyPlanRoute />));

    await waitFor(() =>
      expect(
        screen.getByText(/이 보드가 확인하지 않은 자동제출 조건 \(3\)/),
      ).toBeTruthy(),
    );
    expect(screen.getByTitle("preview_guard_failed")).toBeTruthy();
    expect(screen.getByTitle("daily_cap_exceeded")).toBeTruthy();
  });

  it("surfaces a fetch failure instead of rendering an empty plan", async () => {
    vi.spyOn(buyPlanApi, "fetchBuyPlan").mockRejectedValue(new Error("buy-plan 500"));
    render(wrap(<BuyPlanRoute />));

    await waitFor(() =>
      expect(screen.getByText(/불러오지 못했습니다 — buy-plan 500/)).toBeTruthy(),
    );
  });
});
