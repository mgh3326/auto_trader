export type ApprovalStatus = "pending" | "processing" | "terminal";

export interface ApprovalPreview {
  quantity: string | null;
  limit_price: string | null;
  expected_amount: string | null;
}

export interface ApprovalRungResult {
  rung_index: number;
  state: string;
  void_reason: string | null;
}

export interface OrderProposalApprovalCard {
  proposal_id: string;
  market: string;
  account_mode: string;
  symbol: string;
  side: string;
  action: string;
  exit_intent?: string | null;
  preview: ApprovalPreview[];
  rung_summary: Array<ApprovalPreview & { rung_index: number; state: string }>;
  valid_until: string | null;
  status: ApprovalStatus;
  approval_hash_present: boolean;
  approval_channel: "telegram" | "web" | null;
  approved_at: string | null;
  recent_result: ApprovalRungResult[];
}

export interface OrderProposalApprovalList {
  items: OrderProposalApprovalCard[];
}

export type ApprovalAction = "approve" | "deny" | "loss-cut-confirm";

export interface ApprovalMutationResult {
  handled: boolean;
  reason: string;
  confirmation_token?: string;
  proposal_id?: string;
  results?: string[];
  rung_results?: Array<{ rung_index: number; result: string }>;
}
