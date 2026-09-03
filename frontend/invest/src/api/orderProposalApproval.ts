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

export class ApprovalTerminalError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "ApprovalTerminalError";
  }
}

async function responseJson<T>(response: Response): Promise<T> {
  const payload = (await response.json().catch(() => null)) as {
    detail?: string | { error?: string; reason?: string; operator_message?: string };
  } | null;
  if (response.status === 409) {
    const detail = typeof payload?.detail === "object" ? payload.detail : undefined;
    if (detail?.error === "processing") throw new ApprovalProcessingError();
    if (detail?.error === "terminal") {
      throw new ApprovalTerminalError(
        detail.operator_message ?? detail.reason ?? "승인은 운영자 확인이 필요합니다.",
      );
    }
  }
  if (!response.ok) {
    throw new Error(
      typeof payload?.detail === "string" ? payload.detail : `approval request ${response.status}`,
    );
  }
  return payload as T;
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
  confirmationToken?: string,
): Promise<ApprovalMutationResult> {
  const response = await fetch(`${BASE}/${encodeURIComponent(proposalId)}/${action}`, {
    method: "POST",
    credentials: "include",
    headers: { ...mutationHeaders(), "Idempotency-Key": idempotencyKey() },
    body: JSON.stringify(
      action === "loss-cut-confirm" ? { confirmation_token: confirmationToken } : {},
    ),
  });
  return responseJson(response);
}
