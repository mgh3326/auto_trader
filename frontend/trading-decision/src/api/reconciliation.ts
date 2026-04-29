export type ReconciliationStatus =
  | "maintain"
  | "near_fill"
  | "too_far"
  | "chasing_risk"
  | "data_mismatch"
  | "kr_pending_non_nxt"
  | "unknown_venue"
  | "unknown";

export type NxtClassification =
  | "buy_pending_at_support"
  | "buy_pending_too_far"
  | "buy_pending_actionable"
  | "sell_pending_near_resistance"
  | "sell_pending_too_optimistic"
  | "sell_pending_actionable"
  | "non_nxt_pending_ignore_for_nxt"
  | "holding_watch_only"
  | "data_mismatch_requires_review"
  | "unknown";

export type CandidateKind =
  | "pending_order"
  | "holding"
  | "screener_hit"
  | "proposed"
  | "other";

export const KNOWN_RECON_CLASSIFICATIONS: ReadonlyArray<ReconciliationStatus> = [
  "maintain",
  "near_fill",
  "too_far",
  "chasing_risk",
  "data_mismatch",
  "kr_pending_non_nxt",
  "unknown_venue",
  "unknown",
];

export const KNOWN_NXT_CLASSIFICATIONS: ReadonlyArray<NxtClassification> = [
  "buy_pending_at_support",
  "buy_pending_too_far",
  "buy_pending_actionable",
  "sell_pending_near_resistance",
  "sell_pending_too_optimistic",
  "sell_pending_actionable",
  "non_nxt_pending_ignore_for_nxt",
  "holding_watch_only",
  "data_mismatch_requires_review",
  "unknown",
];

const KNOWN_CANDIDATE_KINDS: ReadonlyArray<CandidateKind> = [
  "pending_order",
  "holding",
  "screener_hit",
  "proposed",
  "other",
];

const WARNING_TOKEN = /^[a-z][a-z0-9_]{0,63}$/;

export interface VenueEligibility {
  nxt: boolean | null;
  regular: boolean | null;
}

export interface ReconciliationDecisionSupport {
  current_price: string | null;
  gap_pct: string | null;
  signed_distance_to_fill: string | null;
  nearest_support_price: string | null;
  nearest_support_distance_pct: string | null;
  nearest_resistance_price: string | null;
  nearest_resistance_distance_pct: string | null;
  bid_ask_spread_pct: string | null;
}

export interface LiveQuote {
  price: string;
  as_of: string;
}

export interface ReconciliationPayload {
  research_run_id: string | null;
  candidate_kind: CandidateKind | null;
  pending_order_id: string | null;
  reconciliation_status: ReconciliationStatus | null;
  reconciliation_summary: string | null;
  nxt_classification: NxtClassification | null;
  nxt_summary: string | null;
  nxt_eligible: boolean | null;
  venue_eligibility: VenueEligibility | null;
  live_quote: LiveQuote | null;
  decision_support: ReconciliationDecisionSupport;
  warnings: string[];
  refreshed_at: string | null;
}

function pickString(v: unknown): string | null {
  return typeof v === "string" && v.length > 0 ? v : null;
}

function pickStringOrNumber(v: unknown): string | null {
  if (typeof v === "string" && v.length > 0) return v;
  if (typeof v === "number" && Number.isFinite(v)) return String(v);
  return null;
}

function pickBool(v: unknown): boolean | null {
  return typeof v === "boolean" ? v : null;
}

function pickClassification(v: unknown): ReconciliationStatus | null {
  if (typeof v !== "string") return null;
  const found = KNOWN_RECON_CLASSIFICATIONS.find((c) => c === v);
  return found ?? "unknown";
}

function pickNxtClassification(v: unknown): NxtClassification | null {
  if (typeof v !== "string") return null;
  const found = KNOWN_NXT_CLASSIFICATIONS.find((c) => c === v);
  return found ?? "unknown";
}

function pickCandidateKind(v: unknown): CandidateKind | null {
  if (typeof v !== "string") return null;
  const found = KNOWN_CANDIDATE_KINDS.find((c) => c === v);
  return found ?? null;
}

function pickWarnings(v: unknown): string[] {
  if (!Array.isArray(v)) return [];
  const out: string[] = [];
  for (const item of v) {
    if (typeof item === "string" && WARNING_TOKEN.test(item)) {
      out.push(item);
    }
  }
  return out;
}

function pickDecisionSupport(v: unknown): ReconciliationDecisionSupport {
  const blank: ReconciliationDecisionSupport = {
    current_price: null,
    gap_pct: null,
    signed_distance_to_fill: null,
    nearest_support_price: null,
    nearest_support_distance_pct: null,
    nearest_resistance_price: null,
    nearest_resistance_distance_pct: null,
    bid_ask_spread_pct: null,
  };
  if (!v || typeof v !== "object") return blank;
  const o = v as Record<string, unknown>;
  return {
    current_price: pickStringOrNumber(o.current_price),
    gap_pct: pickStringOrNumber(o.gap_pct),
    signed_distance_to_fill: pickStringOrNumber(o.signed_distance_to_fill),
    nearest_support_price: pickStringOrNumber(o.nearest_support_price),
    nearest_support_distance_pct: pickStringOrNumber(
      o.nearest_support_distance_pct,
    ),
    nearest_resistance_price: pickStringOrNumber(o.nearest_resistance_price),
    nearest_resistance_distance_pct: pickStringOrNumber(
      o.nearest_resistance_distance_pct,
    ),
    bid_ask_spread_pct: pickStringOrNumber(o.bid_ask_spread_pct),
  };
}

function pickVenueEligibility(v: unknown): VenueEligibility | null {
  if (!v || typeof v !== "object") return null;
  const o = v as Record<string, unknown>;
  if (!("nxt" in o)) return null;
  return {
    nxt: pickBool(o.nxt),
    regular: pickBool(o.regular),
  };
}

function pickLiveQuote(v: unknown): LiveQuote | null {
  if (!v || typeof v !== "object") return null;
  const o = v as Record<string, unknown>;
  const price = pickStringOrNumber(o.price);
  const asOf = pickString(o.as_of);
  if (!price || !asOf) return null;
  return { price, as_of: asOf };
}

const HAS_PAYLOAD_KEYS: ReadonlyArray<string> = [
  "reconciliation_status",
  "nxt_classification",
  "candidate_kind",
  "research_run_id",
  "venue_eligibility",
];

export function parseReconciliationPayload(
  raw: unknown,
): ReconciliationPayload | null {
  if (!raw || typeof raw !== "object") return null;
  const o = raw as Record<string, unknown>;
  const hasAny = HAS_PAYLOAD_KEYS.some((k) => k in o);
  if (!hasAny) return null;

  return {
    research_run_id: pickString(o.research_run_id),
    candidate_kind: pickCandidateKind(o.candidate_kind),
    pending_order_id: pickString(o.pending_order_id),
    reconciliation_status: pickClassification(o.reconciliation_status),
    reconciliation_summary: pickString(o.reconciliation_summary),
    nxt_classification: pickNxtClassification(o.nxt_classification),
    nxt_summary: pickString(o.nxt_summary),
    nxt_eligible: pickBool(o.nxt_eligible),
    venue_eligibility: pickVenueEligibility(o.venue_eligibility),
    live_quote: pickLiveQuote(o.live_quote),
    decision_support: pickDecisionSupport(o.decision_support),
    warnings: pickWarnings(o.warnings),
    refreshed_at: pickString(o.refreshed_at),
  };
}
