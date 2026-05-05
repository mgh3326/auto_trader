// frontend/trading-decision/src/api/candidates.ts
import { apiFetch } from "./client";
import type {
  CandidateScreenRequest,
  CandidateScreenResponse,
  ResearchSessionCreateResponse,
} from "./types";

export function screenCandidates(
  payload: CandidateScreenRequest,
): Promise<CandidateScreenResponse> {
  return apiFetch<CandidateScreenResponse>("/candidates/screen", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function startResearchSession(
  symbol: string,
  instrumentType: "equity_kr" | "equity_us" | "crypto",
): Promise<ResearchSessionCreateResponse> {
  return apiFetch<ResearchSessionCreateResponse>("/research-pipeline/sessions", {
    method: "POST",
    body: JSON.stringify({
      symbol,
      instrument_type: instrumentType,
      triggered_by: "user",
    }),
  });
}
