import { apiFetch } from "./client";
import type {
  OutcomeCreateRequest,
  OutcomeDetail,
  ProposalDetail,
  ProposalRespondRequest,
  SessionAnalyticsResponse,
  SessionDetail,
  SessionListResponse,
  SessionStatus,
  WorkflowStatus,
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

export async function getSessionAnalytics(
  sessionUuid: string,
): Promise<SessionAnalyticsResponse> {
  return apiFetch<SessionAnalyticsResponse>(
    `/decisions/${encodeURIComponent(sessionUuid)}/analytics`,
  );
}

export async function createOutcomeMark(
  proposalUuid: string,
  body: OutcomeCreateRequest,
): Promise<OutcomeDetail> {
  return apiFetch<OutcomeDetail>(
    `/proposals/${encodeURIComponent(proposalUuid)}/outcomes`,
    { method: "POST", body: JSON.stringify(body) },
  );
}

export async function updateWorkflowStatus(
  sessionUuid: string,
  status: WorkflowStatus,
): Promise<SessionDetail> {
  const params = new URLSearchParams();
  params.set("status_update", status);
  return apiFetch<SessionDetail>(
    `/decisions/${encodeURIComponent(sessionUuid)}/workflow?${params.toString()}`,
    { method: "PATCH" },
  );
}

export async function updateArtifacts(
  sessionUuid: string,
  artifactsPatch: Record<string, unknown>,
): Promise<SessionDetail> {
  return apiFetch<SessionDetail>(
    `/decisions/${encodeURIComponent(sessionUuid)}/artifacts`,
    {
      method: "PATCH",
      body: JSON.stringify(artifactsPatch),
    },
  );
}
