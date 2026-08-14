export type EvidenceStatus =
  | "filled"
  | "stale"
  | "missing"
  | "unavailable"
  | "source_error";

export interface LossCutEvidenceField {
  status: EvidenceStatus;
  label: string;
  value: Record<string, unknown> | null;
  reason: string | null;
  source: string | null;
  as_of: string | null;
  valid_until: string | null;
}

export interface LossCutPositionEvidence {
  account_ref: string;
  account_mode: string;
  market: string;
  symbol: string;
  total_quantity: string;
  sellable_quantity: string | null;
  pending_sell_quantity: string | null;
  average_price: string | null;
  current_price: string | null;
  source: string;
  source_status: EvidenceStatus;
  source_reason: string | null;
  observed_at: string;
}

export interface LossCutEvidenceResponse {
  mode: "symbol" | "proposal";
  symbol: string;
  proposal_id: string | null;
  generated_at: string;
  can_begin: boolean;
  positions: LossCutPositionEvidence[];
  loss: LossCutEvidenceField;
  reason: LossCutEvidenceField;
  r931: LossCutEvidenceField;
  consensus: LossCutEvidenceField;
  watch: LossCutEvidenceField;
  fingerprint: Record<string, unknown> | null;
  warnings: string[];
}

export interface LossCutBeginResponse {
  proposal_id: string;
  ceremony_id: string;
  expires_at: string;
  evidence: LossCutEvidenceResponse;
  fingerprint: Record<string, unknown>;
  next_step: "confirm";
}

export interface LossCutConfirmResponse {
  proposal_id: string;
  status: "validated_no_execution";
  evidence: LossCutEvidenceResponse;
  fingerprint: Record<string, unknown>;
}
