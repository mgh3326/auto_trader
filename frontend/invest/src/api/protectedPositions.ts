// /invest/api/settings/protected-positions — #728 operator declarations.
import { readCsrfCookie } from "./fundingAdvisory";

const URL = "/invest/api/settings/protected-positions";

export type ProtectionMode = "off" | "shadow" | "enforce";
export type ProtectionState = "unprotected" | "covered" | "encroached" | "shortfall" | "unverified";

export interface ProtectedPositionRevision {
  revision: number;
  action: string;
  previous_quantity: string | null;
  new_quantity: string;
  broker_held: string;
  broker_sellable: string;
  broker_observed_at: string;
  reason: string;
  actor_user_id: number;
  actor: string | null;
  origin: string;
  recorded_at: string;
}

export interface ProtectedPositionView {
  account_scope: string;
  market: string;
  symbol: string;
  name: string;
  protected_quantity: string;
  broker_held: string | null;
  broker_sellable: string | null;
  headroom: string | null;
  state: ProtectionState;
  mode: ProtectionMode;
  broker_observed_at: string | null;
  read_error: string | null;
  revision: number | null;
  latest_revision: ProtectedPositionRevision | null;
  history_url: string;
}

export interface ProtectedPositionsResponse {
  can_edit: boolean;
  positions: ProtectedPositionView[];
}

export interface ProtectionPreview {
  account_scope: string;
  market: string;
  symbol: string;
  before_protected_quantity: string;
  after_protected_quantity: string;
  broker_held: string;
  broker_sellable: string;
  before_headroom: string;
  headroom: string;
  state: ProtectionState;
  broker_observed_at: string;
}

export interface ProtectedPositionSavePayload {
  protected_quantity: string;
  reason: string;
  expected_revision: number | null;
  idempotency_key: string;
  confirm_protection_change: boolean;
  confirm_symbol?: string;
  reconfirm?: boolean;
}

export class ProtectedPositionSaveError extends Error {
  readonly status: number;
  readonly error: string;
  readonly detail: Record<string, unknown>;

  constructor(status: number, detail: Record<string, unknown>) {
    super(typeof detail.message === "string" ? detail.message : `${URL} ${status}`);
    this.name = "ProtectedPositionSaveError";
    this.status = status;
    this.error = typeof detail.error === "string" ? detail.error : `http_${status}`;
    this.detail = detail;
  }

  get preview(): ProtectionPreview | null {
    const value = this.detail.preview;
    return value && typeof value === "object" ? value as ProtectionPreview : null;
  }
}

export async function fetchProtectedPositions(signal?: AbortSignal): Promise<ProtectedPositionsResponse> {
  const response = await fetch(URL, { credentials: "include", signal });
  if (!response.ok) throw new Error(`${URL} ${response.status}`);
  return response.json();
}

export async function fetchProtectedPositionHistory(
  path: string,
  signal?: AbortSignal,
): Promise<{ history: ProtectedPositionRevision[] }> {
  const response = await fetch(path, { credentials: "include", signal });
  if (!response.ok) throw new Error(`${path} ${response.status}`);
  return response.json();
}

export async function saveProtectedPosition(
  position: Pick<ProtectedPositionView, "account_scope" | "market" | "symbol">,
  payload: ProtectedPositionSavePayload,
): Promise<unknown> {
  const csrfToken = readCsrfCookie();
  if (!csrfToken) throw new Error("CSRF token is unavailable; reload the page before saving");
  const response = await fetch(
    `${URL}/${encodeURIComponent(position.account_scope)}/${encodeURIComponent(position.market)}/${encodeURIComponent(position.symbol)}`,
    {
      method: "PUT",
      credentials: "include",
      headers: { "Content-Type": "application/json", "x-csrftoken": csrfToken },
      body: JSON.stringify(payload),
    },
  );
  if (!response.ok) {
    let detail: Record<string, unknown> = {};
    try {
      const body = await response.json() as { detail?: unknown };
      if (body.detail && typeof body.detail === "object") detail = body.detail as Record<string, unknown>;
      else if (typeof body.detail === "string") detail = { message: body.detail };
    } catch {
      // Retain the HTTP status when a reverse proxy emitted a non-JSON body.
    }
    throw new ProtectedPositionSaveError(response.status, detail);
  }
  return response.json();
}
