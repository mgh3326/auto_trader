// Closed-vocabulary label maps for the 매수 계획 board — §144차.
//
// Every backend enum is mapped exhaustively here so a value the UI does not
// recognise shows as the raw token rather than silently rendering as an empty
// cell that reads like "nothing to see".
import type {
  ApprovalLane,
  ApprovalLaneReason,
  FundingBroker,
  FundingVerdict,
  GateConditionState,
  GateState,
  PlacementForm,
  SourceState,
} from "../../types/buyPlan";

// verify-r1 B6: the backend classifies a notional against two caps and nothing
// else. Calling that "자동승인" told the operator an approval decision had been
// made, when real dispatch consults a default-off master gate plus conditions
// this board never evaluates. The label now says what was actually checked.
export const APPROVAL_LANE_LABEL: Record<ApprovalLane, string> = {
  auto_submit: "자동승인 가능(cap 기준)",
  human_card: "카드(수동 승인)",
};

/** What the badge means once the master gate is known to be off. */
export const APPROVAL_LANE_LABEL_GATE_OFF: Record<ApprovalLane, string> = {
  auto_submit: "카드(자동승인 꺼짐)",
  human_card: "카드(수동 승인)",
};

// verify-r2 SHOULD-2: an unreadable gate used to render identically to a gate
// known to be ON — same accent colour, same "자동승인 가능" wording. Not knowing
// is its own state and must look like one.
export const APPROVAL_LANE_LABEL_GATE_UNKNOWN: Record<ApprovalLane, string> = {
  auto_submit: "레인 판정 불가(게이트 상태 불명)",
  human_card: "카드(수동 승인)",
};

export const APPROVAL_LANE_REASON_LABEL: Record<ApprovalLaneReason, string> = {
  within_tier_auto_submit_notional: "티어 자동제출 한도 이내 (cap 기준 분류일 뿐, 승인 확정 아님)",
  above_tier_auto_submit_notional: "티어 자동제출 한도 초과",
  above_per_order_auto_approve_cap: "건당 자동승인 상한 초과",
  notional_unavailable: "금액을 확정하지 못해 fail-closed",
};

export const FUNDING_BROKER_LABEL: Record<FundingBroker, string> = {
  kis: "한국투자증권",
  upbit: "업비트",
  toss: "토스증권",
  unattributed: "계좌 미확정",
};

export const SOURCE_STATE_LABEL: Record<SourceState, string> = {
  ok: "정상",
  degraded: "일부 누락",
  unavailable: "조회 불가",
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
