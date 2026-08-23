// /invest 매수 계획 (트리거 보드) — §144차.
//
// Every money field arrives as an exact decimal *string*, never a JSON number:
// these are the amounts the operator moves cash against, so no float rounding
// is allowed between the policy arithmetic and the screen.

export type BuyPlanMarketFilter = "all" | "kr" | "us" | "crypto";
export type BuyPlanMarket = "kr" | "us" | "crypto";
export type BuyPlanCurrency = "KRW" | "USD";
export type ApprovalLane = "auto_submit" | "human_card";
export type ApprovalLaneReason =
  | "within_tier_auto_submit_notional"
  | "above_tier_auto_submit_notional"
  | "above_per_order_auto_approve_cap"
  | "notional_unavailable";
export type PlacementForm = "resting_order" | "watch";
export type GateConditionState = "met" | "not_met" | "unavailable";
export type GateState = "open" | "closed" | "indeterminate";
export type FundingVerdict = "sufficient" | "shortfall" | "unknown";
export type SourceState = "ok" | "degraded" | "unavailable";
/** Cash at one broker cannot fund an order placed at another (verify-r1 B1). */
export type FundingBroker = "kis" | "upbit" | "toss" | "unattributed";
export type RequirementKind = "averaging_add" | "support_net" | "active_watch";

export interface PolicyStamp {
  version: string;
  content_hash: string;
}

export interface ApprovalContext {
  master_gate_enabled: boolean | null;
  master_gate_setting: string;
  master_gate_source: string;
  unevaluated_conditions: string[];
  notice: string;
}

export interface ValueSource {
  field: string;
  source: string;
  note: string | null;
}

export interface AveragingSampleRow {
  offset_from_turn_point_pct: string;
  price: string;
  additional_notional: string;
  target_average_price: string;
  approval_lane: ApprovalLane;
  approval_lane_reason: ApprovalLaneReason;
}

export interface AveragingTriggerRow {
  market: BuyPlanMarket;
  symbol: string;
  symbol_name: string | null;
  currency: BuyPlanCurrency;
  account_sources: string[];
  quantity: string;
  average_price: string;
  cost_basis: string;
  current_price: string;
  unrealized_pnl_pct: string | null;
  k: string;
  turn_point_price: string;
  distance_to_turn_point_pct: string;
  turn_point_reached: boolean;
  samples: AveragingSampleRow[];
  reserve_plan_notional: string;
  market_rank: number;
  within_policy_add_cap: boolean;
  funding_broker: FundingBroker;
  notes: string[];
}

export interface SupportNetPlacement {
  form: PlacementForm;
  reference: string;
  anchor_price: string | null;
  quantity: string | null;
  notional: string | null;
  distance_from_current_pct: string | null;
  valid_until: string | null;
  within_policy_distance_band: boolean | null;
}

export interface SupportNetRow {
  market: BuyPlanMarket;
  symbol: string;
  symbol_name: string | null;
  currency: BuyPlanCurrency;
  quantity: string;
  average_price: string | null;
  current_price: string | null;
  unrealized_pnl_pct: string | null;
  eligible: boolean;
  ineligible_reason: string | null;
  placements: SupportNetPlacement[];
  /** `null` when a placement source was degraded — unknown is not zero. */
  placed_notional: string | null;
  per_symbol_cap_notional: string;
  remaining_headroom_notional: string | null;
  placements_state: SourceState;
  placements_incomplete_reasons: string[];
}

export interface SupportNetTier {
  policy_key: string;
  enabled: boolean;
  currency: BuyPlanCurrency;
  tier_cap_notional: string | null;
  per_symbol_cap_notional: string | null;
  placed_notional: string | null;
  remaining_notional: string | null;
  placements_state: SourceState;
  placements_incomplete_reasons: string[];
  distance_band_pct: string[];
  review_date: string | null;
  rows: SupportNetRow[];
  notes: string[];
}

export interface ActiveBuyWatchRow {
  market: BuyPlanMarket;
  symbol: string;
  symbol_name: string | null;
  currency: BuyPlanCurrency;
  alert_uuid: string;
  metric: string;
  operator: "above" | "below" | "between";
  threshold: string;
  threshold_high: string | null;
  current_price: string | null;
  distance_to_threshold_pct: string | null;
  valid_until: string;
  near_expiry: boolean;
  planned_notional: string | null;
  planned_notional_source: string | null;
  funding_broker: FundingBroker;
  account_mode: string | null;
  approval_lane: ApprovalLane;
  approval_lane_reason: ApprovalLaneReason;
}

export interface DiscoveryGateCondition {
  condition_id: string;
  metric: string;
  comparison: string | null;
  threshold: string | null;
  unit: string | null;
  current_value: string | null;
  state: GateConditionState;
  source: string | null;
  note: string | null;
}

export interface DiscoveryGateRow {
  market: BuyPlanMarket;
  gate_key: string;
  state: GateState;
  min_conditions_met: number;
  of: number;
  met_count: number;
  unavailable_count: number;
  semantics: string | null;
  conditions: DiscoveryGateCondition[];
  notes: string[];
}

export interface CashAccountRow {
  account_id: string;
  display_name: string;
  source: string;
  broker: FundingBroker;
  currency: BuyPlanCurrency;
  available_cash: string | null;
  available_cash_source: string;
  included_in_reserve: boolean;
}

export interface ScopeReconciliation {
  scope_key: string;
  broker: FundingBroker;
  currency: BuyPlanCurrency;
  account_ids: string[];
  available_cash: string | null;
  required_averaging_adds: string;
  required_support_net: string;
  required_active_watches: string;
  required_total: string;
  unattributed_same_currency: string;
  worst_case_required: string;
  requirements_complete: boolean;
  incomplete_reasons: string[];
  verdict: FundingVerdict;
  shortfall: string | null;
  notes: string[];
}

export interface UnattributedRequirement {
  kind: RequirementKind;
  label: string;
  currency: BuyPlanCurrency;
  amount: string | null;
  reason: string;
}

export interface BuyPlanFunding {
  accounts: CashAccountRow[];
  scopes: ScopeReconciliation[];
  unattributed: UnattributedRequirement[];
  source_warnings: string[];
}

export interface BuyPlanResponse {
  as_of: string;
  policy: PolicyStamp;
  cache_ttl_seconds: number;
  approximation_notice: string;
  approval_context: ApprovalContext;
  market: BuyPlanMarketFilter;
  averaging_triggers: AveragingTriggerRow[];
  support_net: SupportNetTier;
  active_buy_watches: ActiveBuyWatchRow[];
  discovery_gates: DiscoveryGateRow[];
  funding: BuyPlanFunding;
  value_sources: ValueSource[];
  warnings: string[];
}
