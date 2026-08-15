import type {
  ExternalCashCurrentView,
  ExternalCashDeclaration,
  ExternalCashForm,
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

export function fetchExternalCashCurrent(signal?: AbortSignal): Promise<ExternalCashCurrentView> {
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

export async function declareExternalCash(
  form: ExternalCashForm,
  values: { amount: string; asOf: string; sourceNote: string },
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
    body: JSON.stringify({
      owner_user_id: form.owner_user_id,
      location_key: form.location_key,
      display_label: form.display_label,
      currency: form.currency,
      amount: values.amount,
      as_of: values.asOf,
      source_note: values.sourceNote,
      expected_head_declaration_id: form.expected_head_declaration_id,
      idempotency_key: form.idempotency_key,
    }),
  });
  if (!response.ok) throw new Error(`${BASE}/external-cash/declarations ${response.status}`);
  return response.json();
}
