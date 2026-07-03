import type {
  SessionContextRecentResponse,
  SessionEntryType,
} from "../types/sessionContext";

const BASE = "/trading/api/invest/session-context";

export interface SessionContextRecentQuery {
  market?: "kr" | "us" | "crypto";
  accountScope?: string;
  entryType?: SessionEntryType;
  kstDateFrom?: string;
  limit?: number;
}

export async function fetchRecentSessionContext(
  params: SessionContextRecentQuery = {},
): Promise<SessionContextRecentResponse> {
  const q = new URLSearchParams();
  if (params.market) q.set("market", params.market);
  if (params.accountScope) q.set("account_scope", params.accountScope);
  if (params.entryType) q.set("entry_type", params.entryType);
  if (params.kstDateFrom) q.set("kst_date_from", params.kstDateFrom);
  if (params.limit != null) q.set("limit", String(params.limit));
  const qs = q.toString();
  const res = await fetch(`${BASE}/recent${qs ? `?${qs}` : ""}`, {
    credentials: "include",
  });
  if (!res.ok)
    throw new Error(`fetchRecentSessionContext failed: ${res.status}`);
  return res.json();
}
