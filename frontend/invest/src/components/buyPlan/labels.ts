// Closed-vocabulary label maps for the 매수 계획 board — §144차.
//
// Every backend enum is mapped exhaustively here so a value the UI does not
// recognise shows as the raw token rather than silently rendering as an empty
// cell that reads like "nothing to see".
import type {
  ApprovalLane,
  ApprovalLaneReason,
  FundingVerdict,
  GateConditionState,
  GateState,
  PlacementForm,
} from "../../types/buyPlan";

export const APPROVAL_LANE_LABEL: Record<ApprovalLane, string> = {
  auto_submit: "자동승인",
  human_card: "카드(수동 승인)",
};

export const APPROVAL_LANE_REASON_LABEL: Record<ApprovalLaneReason, string> = {
  within_tier_auto_submit_notional: "티어 자동제출 한도 이내",
  above_tier_auto_submit_notional: "티어 자동제출 한도 초과",
  above_per_order_auto_approve_cap: "건당 자동승인 상한 초과",
  notional_unavailable: "금액을 확정하지 못해 fail-closed",
};

export const PLACEMENT_FORM_LABEL: Record<PlacementForm, string> = {
  resting_order: "주문 상시형",
  watch: "워치형",
};

export const GATE_STATE_LABEL: Record<GateState, string> = {
  open: "열림",
  closed: "닫힘",
  indeterminate: "판정 불가",
};

export const GATE_CONDITION_STATE_LABEL: Record<GateConditionState, string> = {
  met: "충족",
  not_met: "미충족",
  unavailable: "확인 불가",
};

export const FUNDING_VERDICT_LABEL: Record<FundingVerdict, string> = {
  sufficient: "리저브 충분",
  shortfall: "입금 필요",
  unknown: "대조 보류",
};

export const COMPARISON_LABEL: Record<string, string> = {
  gt: ">",
  gte: "≥",
  lt: "<",
  lte: "≤",
  eq: "=",
};
