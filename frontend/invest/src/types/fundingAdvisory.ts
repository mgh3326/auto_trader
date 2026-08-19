export type FundingRouteId =
  | "EXTERNAL_PARKING_KRW"
  | "USD_CONVERSION"
  | "CREDIT_LINE_SHORT_TERM"
  | "PROFITABLE_TRIM"
  | "LOSS_CUT_ROTATION";

export interface FundingRouteView {
  route_id: FundingRouteId;
  label: string;
  amount_status: "known" | "unknown" | "conditional";
  route_fundable_amount: string | null;
  counted_fundable_amount: string;
  confidence: "broker_authoritative" | "operator_declared" | "conditional" | "unknown";
  source_as_of: string | null;
  deadline_status: "met" | "missed" | "unknown";
  explicit_cost: string | null;
  eta_minutes: number | null;
  realized_impact: string | null;
  reversibility: "reversible" | "conditional" | "irreversible" | "unknown";
  eligibility: "eligible" | "locked" | "comparison_unavailable";
  reason_codes: string[];
  comparison: "preferred" | "situation_dependent" | "dominated" | "unavailable";
}

export interface FundingAdvisoryView {
  status: "triggered";
  advisory_id: string;
  thread_key: string;
  state: "active" | "resolved" | "superseded";
  revision_id: string;
  revision_no: number;
  fingerprint: string;
  trigger: {
    source_kind: string;
    source_candidate_id: string;
    gate_name: string;
    gate_version: string;
    gate_version_kind: "contract_schema_version";
    gate_verdict: "passed";
    gate_evaluated_at: string;
    valid_until: string;
    upstream_priority: string | null;
  };
  target: {
    market: "crypto" | "equity_kr" | "equity_us";
    account_mode: "upbit" | "kis_live" | "toss_live";
    broker_account_id: string;
    currency: "KRW" | "USD";
    symbol: string;
    side: "buy";
  };
  need: {
    required_cash: string;
    target_buying_power: string;
    shortfall: string;
    funding_needed: string;
    other_pending_required: string;
    reserved_cash: string;
    operational_gap_including_other_pending: string;
    shortfall_scope: "this_candidate_only";
  };
  routes: FundingRouteView[];
  combination: {
    selected: false;
    scenario_kind: "reference_only";
    selection_basis: "operator_decision_required_multi_axis";
    legs: Array<{
      route_id: FundingRouteId;
      planned_amount: string;
      cumulative_planned_amount: string;
      remaining_gap: string;
    }>;
    remaining_gap: string;
  };
  safety: {
    advisory_only: true;
    executes_money_movement: false;
    creates_proposal: false;
    authoritative_for_order_gate: false;
  };
  proposal_handoff: {
    source_funding_advisory_id: string;
    provenance_only: true;
    classifier_input: false;
    sizing_input: false;
    eligibility_input: false;
    action_label: string;
    ordinary_trim: string;
    loss_cut: string;
  };
  evaluated_at: string;
  expires_at: string;
  delivery: Record<string, unknown>;
  refresh?: Record<string, unknown>;
}

export interface FundingAdvisoryListResponse {
  advisories: FundingAdvisoryView[];
  count: number;
}

export interface FundingAllocationBucket {
  currency: string;
  broker_confirmed_total_native: string;
  declared_total_native: string;
  conditional_total_native: string;
  display_total_native_including_declared: string;
  demands: Array<{
    advisory_id: string;
    market: string;
    symbol: string;
    shortfall: string;
    upstream_priority: string | null;
  }>;
  contention: boolean;
  krw_equivalent: string | null;
  krw_equivalent_status: "native" | "conversion_unavailable";
}

export interface FundingAllocationView {
  buckets: FundingAllocationBucket[];
  cross_currency_total: null;
  cross_currency_total_status: "not_summed_without_executable_fx";
  investment_priority_recomputed: false;
  generated_at: string;
}

export interface ExternalCashDeclaration {
  declaration_id: string;
  owner_user_id: number;
  location_key: string;
  display_label: string;
  currency: string;
  amount: string;
  as_of: string;
  fresh_until: string;
  source_note: string;
  declared_by_user_id: number;
  origin: "invest_ui";
  supersedes_declaration_id: string | null;
  idempotency_key: string;
  recorded_at: string;
}

export interface ExternalCashCurrentView {
  status: "missing" | "fresh" | "stale" | "future" | "ambiguous";
  amount_status: "known" | "unknown";
  current: ExternalCashDeclaration | null;
  route_fundable_amount: string | null;
  verification_badge: string;
  warning_code: string | null;
}

export interface ExternalCashHistoryView {
  declarations: ExternalCashDeclaration[];
  count: number;
}

export interface ExternalCashForm {
  owner_user_id: number;
  location_key: string;
  display_label: string;
  currency: "KRW";
  amount: string;
  as_of: null;
  source_note: string;
  expected_head_declaration_id: string | null;
  idempotency_key: string;
  requires_exact_operator_confirmed_time: true;
  creates_money_movement: false;
}
