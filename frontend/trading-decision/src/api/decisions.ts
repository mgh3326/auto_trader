import { apiFetch } from "./client";
import type {
  ProposalDetail,
  ProposalRespondRequest,
  SessionDetail,
  SessionListResponse,
  SessionStatus,
} from "./types";

export async function getDecisions(args: {
  limit: number;
  offset: number;
  status?: SessionStatus;
}): Promise<SessionListResponse> {
  const params = new URLSearchParams();
  params.set("limit", String(args.limit));
  params.set("offset", String(args.offset));
  if (args.status) params.set("status", args.status);
  return apiFetch<SessionListResponse>(`/decisions?${params.toString()}`);
}

export async function getSession(sessionUuid: string): Promise<SessionDetail> {
  return apiFetch<SessionDetail>(
    `/decisions/${encodeURIComponent(sessionUuid)}`,
  );
}

export async function respondToProposal(
  proposalUuid: string,
  body: ProposalRespondRequest,
): Promise<ProposalDetail> {
  return apiFetch<ProposalDetail>(
    `/proposals/${encodeURIComponent(proposalUuid)}/respond`,
    {
      method: "POST",
      body: JSON.stringify(body),
    },
  );
}
