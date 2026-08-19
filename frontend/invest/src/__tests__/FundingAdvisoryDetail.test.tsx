import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { FundingAdvisoryDetail } from "../pages/FundingRoute";
import type { FundingAdvisoryView, FundingRouteView } from "../types/fundingAdvisory";

function route(routeId: FundingRouteView["route_id"], label: string): FundingRouteView {
  return {
    route_id: routeId,
    label,
    amount_status: "conditional",
    route_fundable_amount: null,
    counted_fundable_amount: "0",
    confidence: "conditional",
    source_as_of: null,
    deadline_status: "unknown",
    explicit_cost: null,
    eta_minutes: null,
    realized_impact: null,
    reversibility: "irreversible",
    eligibility: "comparison_unavailable",
    reason_codes: ["review_required"],
    comparison: "unavailable",
  };
}

const ADVISORY: FundingAdvisoryView = {
  status: "triggered",
  advisory_id: "a67f8983-95ec-4d3f-aeb2-2b13f3ec6a0b",
  thread_key: "candidate:funding-contract-v1",
  state: "active",
  revision_id: "c0141bcb-90ec-437f-b516-ea039e0ee4c0",
  revision_no: 1,
  fingerprint: "fingerprint",
  trigger: {
    source_kind: "reservation_candidate",
    source_candidate_id: "candidate-1",
    gate_name: "candidate-gate",
    gate_version: "funding-contract-v1",
    gate_version_kind: "contract_schema_version",
    gate_verdict: "passed",
    gate_evaluated_at: "2026-08-15T00:00:00Z",
    valid_until: "2026-08-15T01:00:00Z",
    upstream_priority: "operator-ranked",
  },
  target: {
    market: "crypto",
    account_mode: "upbit",
    broker_account_id: "account-1",
    currency: "KRW",
    symbol: "BTC",
    side: "buy",
  },
  need: {
    required_cash: "1000000",
    target_buying_power: "200000",
    shortfall: "800000",
    funding_needed: "800000",
    other_pending_required: "150000",
    reserved_cash: "50000",
    operational_gap_including_other_pending: "1000000",
    shortfall_scope: "this_candidate_only",
  },
  routes: [
    route("PROFITABLE_TRIM", "수익 종목 축소 경로"),
    route("LOSS_CUT_ROTATION", "손실 종목 교체 경로"),
  ],
  combination: {
    selected: false,
    scenario_kind: "reference_only",
    selection_basis: "operator_decision_required_multi_axis",
    legs: [],
    remaining_gap: "800000",
  },
  safety: {
    advisory_only: true,
    executes_money_movement: false,
    creates_proposal: false,
    authoritative_for_order_gate: false,
  },
  proposal_handoff: {
    source_funding_advisory_id: "a67f8983-95ec-4d3f-aeb2-2b13f3ec6a0b",
    provenance_only: true,
    classifier_input: false,
    sizing_input: false,
    eligibility_input: false,
    action_label: "경로 설명 · 이 화면에서 주문 안 만듦",
    ordinary_trim: "별도 create 확인 뒤 기존 dispatch 분류와 승인/veto 경로 적용",
    loss_cut: "기존 loss_cut_intent 거절 규칙과 2-click nonce 유지",
  },
  evaluated_at: "2026-08-15T00:00:00Z",
  expires_at: "2026-08-15T01:00:00Z",
  delivery: { action: "none" },
};

describe("FundingAdvisoryDetail safety presentation", () => {
  it("discloses candidate shortfall, other pending demand, reserved cash, and operational gap separately", () => {
    render(<FundingAdvisoryDetail advisory={ADVISORY} />);

    expect(screen.getByText("이 후보 shortfall")).toBeInTheDocument();
    expect(screen.getByText("다른 pending limit buy")).toBeInTheDocument();
    expect(screen.getByText("별도 reserved")).toBeInTheDocument();
    expect(screen.getByText("pending/reserved 포함 운영상 gap")).toBeInTheDocument();
  });

  it("uses the fixed non-CTA label for both sell paths and offers no order button", () => {
    render(<FundingAdvisoryDetail advisory={ADVISORY} />);

    expect(screen.getAllByText("경로 설명 · 이 화면에서 주문 안 만듦")).toHaveLength(3);
    expect(screen.queryByText("매도 제안 검토")).toBeNull();
    expect(screen.queryByRole("button")).toBeNull();
  });

  it("states that the combination is a reference and preserves inherited approval rules", () => {
    render(<FundingAdvisoryDetail advisory={ADVISORY} />);

    expect(screen.getByText("selected: false")).toBeInTheDocument();
    expect(screen.getByText(/기존 dispatch 분류와 승인\/veto 경로 적용/)).toBeInTheDocument();
    expect(screen.getByText(/loss_cut_intent 거절 규칙과 2-click nonce 유지/)).toBeInTheDocument();
    expect(screen.getByText(/분류·사이징·eligibility 입력 아님/)).toBeInTheDocument();
  });
});
