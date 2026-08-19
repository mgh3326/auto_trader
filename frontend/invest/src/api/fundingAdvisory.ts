import type {
  ExternalCashDeclarePayload,
  ExternalCashDeclaration,
  ExternalCashForm,
  ExternalCashHeadsView,
  ExternalCashHistoryView,
  FundingAdvisoryListResponse,
  FundingAdvisoryView,
  FundingAllocationView,
} from "../types/fundingAdvisory";

const BASE = "/invest/api/funding";

async function getJson<T>(url: string, signal?: AbortSignal): Promise<T> {
  const response = await fetch(url, { credentials: "include", signal });
  if (!response.ok) throw new Error(`${url} ${response.status}`);
  return response.json();
}

export function fetchFundingAdvisories(signal?: AbortSignal): Promise<FundingAdvisoryListResponse> {
  return getJson(`${BASE}/advisories?state=active`, signal);
}

export function fetchFundingAdvisory(
  advisoryId: string,
  signal?: AbortSignal,
): Promise<FundingAdvisoryView> {
  return getJson(`${BASE}/advisories/${encodeURIComponent(advisoryId)}?refresh=true`, signal);
}

export function fetchFundingAllocation(signal?: AbortSignal): Promise<FundingAllocationView> {
  return getJson(`${BASE}/allocation`, signal);
}

export function fetchExternalCashCurrent(signal?: AbortSignal): Promise<ExternalCashHeadsView> {
  return getJson(`${BASE}/external-cash/current`, signal);
}

export function fetchExternalCashHistory(signal?: AbortSignal): Promise<ExternalCashHistoryView> {
  return getJson(`${BASE}/external-cash/history`, signal);
}

export async function fetchExternalCashForm(signal?: AbortSignal): Promise<ExternalCashForm | null> {
  const response = await fetch(`${BASE}/external-cash/form`, {
    credentials: "include",
    signal,
  });
  if (response.status === 403) return null;
  if (!response.ok) throw new Error(`${BASE}/external-cash/form ${response.status}`);
  return response.json();
}

export function readCsrfCookie(cookie = document.cookie): string | null {
  const token = cookie
    .split(";")
    .map((part) => part.trim())
    .find((part) => part.startsWith("csrftoken="));
  return token ? decodeURIComponent(token.slice("csrftoken=".length)) : null;
}

export class ExternalCashDeclareConflict extends Error {
  readonly error: string;
  readonly currentHead: ExternalCashDeclaration | null;

  constructor(detail: {
    error?: string;
    message?: string;
    current_head?: ExternalCashDeclaration | null;
  }) {
    super(detail.message ?? "expected declaration head does not match current head");
    this.name = "ExternalCashDeclareConflict";
    this.error = detail.error ?? "expected_head_conflict";
    this.currentHead = detail.current_head ?? null;
  }
}

export async function declareExternalCash(
  payload: ExternalCashDeclarePayload,
): Promise<ExternalCashDeclaration> {
  const csrfToken = readCsrfCookie();
  if (!csrfToken) throw new Error("CSRF token is unavailable; reload the form before submitting");
  const response = await fetch(`${BASE}/external-cash/declarations`, {
    method: "POST",
    credentials: "include",
    headers: {
      "Content-Type": "application/json",
      "x-csrftoken": csrfToken,
    },
    body: JSON.stringify(payload),
  });
  if (response.status === 409) {
    const body = (await response.json()) as {
      detail?: {
        error?: string;
        message?: string;
        current_head?: ExternalCashDeclaration | null;
      };
    };
    throw new ExternalCashDeclareConflict(body.detail ?? {});
  }
  if (!response.ok) throw new Error(`${BASE}/external-cash/declarations ${response.status}`);
  return response.json();
}
