// /invest/api/settings/manual-cash — operator-entered user_settings.manual_cash (#671).
import { readCsrfCookie } from "./fundingAdvisory";

const URL = "/invest/api/settings/manual-cash";

export interface ManualCashAccountRow {
  name: string;
  amount: number;
}

export interface ManualCashView {
  present: boolean;
  amount: number | null;
  amount_valid: boolean;
  accounts: ManualCashAccountRow[];
  source: string | null;
  confirmed_at?: string | null;
  updated_at: string | null;
  stale: boolean;
  stale_at: string | null;
  stale_after_hours: number;
}

export interface ManualCashLimits {
  max_amount_krw: number;
  max_accounts: number;
  name_max_len: number;
  confirm_change_ratio: string;
}

export interface ManualCashResponse {
  manual_cash: ManualCashView;
  limits: ManualCashLimits;
  can_edit: boolean;
}

export interface ManualCashSavePayload {
  accounts: ManualCashAccountRow[];
  expected_updated_at: string | null;
  confirm_large_change: boolean;
}

export class ManualCashSaveError extends Error {
  readonly status: number;
  readonly error: string;
  readonly detail: Record<string, unknown>;

  constructor(status: number, detail: Record<string, unknown>) {
    super(typeof detail.message === "string" ? detail.message : `${URL} ${status}`);
    this.name = "ManualCashSaveError";
    this.status = status;
    this.error = typeof detail.error === "string" ? detail.error : `http_${status}`;
    this.detail = detail;
  }
}

export async function fetchManualCash(signal?: AbortSignal): Promise<ManualCashResponse> {
  const response = await fetch(URL, { credentials: "include", signal });
  if (!response.ok) throw new Error(`${URL} ${response.status}`);
  return response.json();
}

export async function saveManualCash(payload: ManualCashSavePayload): Promise<ManualCashResponse> {
  const csrfToken = readCsrfCookie();
  if (!csrfToken) throw new Error("CSRF token is unavailable; reload the page before saving");
  const response = await fetch(URL, {
    method: "PUT",
    credentials: "include",
    headers: { "Content-Type": "application/json", "x-csrftoken": csrfToken },
    body: JSON.stringify(payload),
  });
  if (!response.ok) {
    let detail: Record<string, unknown> = {};
    try {
      const body = (await response.json()) as { detail?: unknown };
      if (body.detail && typeof body.detail === "object") detail = body.detail as Record<string, unknown>;
      else if (typeof body.detail === "string") detail = { message: body.detail };
    } catch {
      // non-JSON error body — keep the status only
    }
    throw new ManualCashSaveError(response.status, detail);
  }
  return response.json();
}
