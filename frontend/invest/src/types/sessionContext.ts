// Read-only operator session-context surface (ROB-664).
// Mirrors app/schemas/session_context.py field-for-field (snake_case preserved).

export type SessionEntryType =
  | "plan"
  | "decision"
  | "deferred"
  | "rejected_candidate"
  | "constraint"
  | "open_question"
  | "next_action"
  | "handoff_note";

export type SessionEntryCreatedBy = "claude" | "operator" | "system";

export type SessionAccountScope =
  | "kis_live"
  | "kis_paper"
  | "upbit"
  | "toss"
  | "isa"
  | "pension";

export interface SessionContextEntry {
  entry_uuid: string;
  kst_date: string;
  market: "kr" | "us" | "crypto";
  account_scope: SessionAccountScope | null;
  entry_type: SessionEntryType;
  title: string;
  body: string;
  refs: Record<string, unknown>;
  created_by: SessionEntryCreatedBy;
  session_label: string | null;
  created_at: string;
}

export interface SessionContextRecentFilters {
  market: "kr" | "us" | "crypto" | null;
  account_scope: SessionAccountScope | null;
  kst_date_from: string | null;
  entry_type: SessionEntryType | null;
  limit: number;
}

export interface SessionContextRecentResponse {
  success: true;
  count: number;
  filters: SessionContextRecentFilters;
  entries: SessionContextEntry[];
}
