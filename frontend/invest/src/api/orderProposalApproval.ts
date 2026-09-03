import { mutationHeaders } from "./lossCutApproval";
import type {
  ApprovalAction,
  ApprovalMutationResult,
  OrderProposalApprovalCard,
  OrderProposalApprovalList,
} from "../types/orderProposalApproval";

const BASE = "/invest/api/approvals";

export class ApprovalProcessingError extends Error {
  constructor() {
    super("processing");
    this.name = "ApprovalProcessingError";
  }
}

async function responseJson<T>(response: Response): Promise<T> {
  if (response.status === 409) {
    const payload = (await response.json().catch(() => null)) as {
      detail?: { error?: string };
    } | null;
    if (payload?.detail?.error === "processing") throw new ApprovalProcessingError();
  }
  if (!response.ok) {
    const payload = (await response.json().catch(() => null)) as { detail?: string } | null;
    throw new Error(payload?.detail ?? `approval request ${response.status}`);
  }
  return response.json() as Promise<T>;
}

export async function fetchOrderProposalApprovals(
  signal?: AbortSignal,
): Promise<OrderProposalApprovalList> {
  const response = await fetch(BASE, { credentials: "include", signal, cache: "no-store" });
  return responseJson(response);
}

export async function fetchOrderProposalApproval(
  proposalId: string,
  signal?: AbortSignal,
): Promise<OrderProposalApprovalCard> {
  const response = await fetch(`${BASE}/${encodeURIComponent(proposalId)}`, {
    credentials: "include",
    signal,
    cache: "no-store",
  });
  return responseJson(response);
}

function idempotencyKey(): string {
  return globalThis.crypto?.randomUUID?.() ?? `approval-${Date.now()}-${Math.random()}`;
}

export async function mutateOrderProposalApproval(
  proposalId: string,
  action: ApprovalAction,
): Promise<ApprovalMutationResult> {
  const response = await fetch(`${BASE}/${encodeURIComponent(proposalId)}/${action}`, {
    method: "POST",
    credentials: "include",
    headers: { ...mutationHeaders(), "Idempotency-Key": idempotencyKey() },
    body: JSON.stringify({}),
  });
  return responseJson(response);
}
