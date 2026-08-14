import type {
  LossCutBeginResponse,
  LossCutConfirmResponse,
  LossCutEvidenceResponse,
} from "../types/lossCutApproval";

const BASE = "/invest/api";

function readCookie(name: string): string | null {
  const prefix = `${name}=`;
  for (const part of document.cookie.split(";")) {
    const value = part.trim();
    if (value.startsWith(prefix)) return decodeURIComponent(value.slice(prefix.length));
  }
  return null;
}

async function responseJson<T>(response: Response): Promise<T> {
  if (!response.ok) {
    const payload = (await response.json().catch(() => null)) as { detail?: string } | null;
    throw new Error(payload?.detail ?? `loss-cut approval ${response.status}`);
  }
  return response.json() as Promise<T>;
}

function mutationHeaders(): HeadersInit {
  const csrf = readCookie("csrftoken");
  return {
    "Content-Type": "application/json",
    ...(csrf ? { "X-CSRFToken": csrf } : {}),
  };
}

export async function fetchLossCutSymbolEvidence(
  symbol: string,
  signal?: AbortSignal,
): Promise<LossCutEvidenceResponse> {
  const response = await fetch(
    `${BASE}/loss-cut-evidence/${encodeURIComponent(symbol)}`,
    { credentials: "include", signal, cache: "no-store" },
  );
  return responseJson(response);
}

export async function fetchLossCutProposalEvidence(
  proposalId: string,
  signal?: AbortSignal,
): Promise<LossCutEvidenceResponse> {
  const response = await fetch(
    `${BASE}/loss-cut-approvals/${encodeURIComponent(proposalId)}`,
    { credentials: "include", signal, cache: "no-store" },
  );
  return responseJson(response);
}

export async function beginLossCutApproval(
  proposalId: string,
): Promise<LossCutBeginResponse> {
  const response = await fetch(
    `${BASE}/loss-cut-approvals/${encodeURIComponent(proposalId)}/begin`,
    {
      method: "POST",
      credentials: "include",
      headers: mutationHeaders(),
      body: JSON.stringify({}),
    },
  );
  return responseJson(response);
}

export async function confirmLossCutApproval(
  proposalId: string,
  ceremonyId: string,
): Promise<LossCutConfirmResponse> {
  const response = await fetch(
    `${BASE}/loss-cut-approvals/${encodeURIComponent(proposalId)}/confirm`,
    {
      method: "POST",
      credentials: "include",
      headers: mutationHeaders(),
      body: JSON.stringify({ ceremony_id: ceremonyId }),
    },
  );
  return responseJson(response);
}
