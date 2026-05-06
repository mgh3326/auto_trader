// frontend/trading-decision/src/api/tradeJournals.ts
import { apiFetch } from "./client";
import type {
  JournalCoverageResponse,
  JournalCreateRequest,
  JournalReadResponse,
  JournalUpdateRequest,
  Market,
} from "./types";

export function getJournalCoverage(
  market?: Market,
): Promise<JournalCoverageResponse> {
  const qs = market ? `?market=${encodeURIComponent(market)}` : "";
  return apiFetch<JournalCoverageResponse>(`/trade-journals/coverage${qs}`);
}

export function getJournalRetrospective(): Promise<JournalReadResponse[]> {
  return apiFetch<JournalReadResponse[]>("/trade-journals/retrospective");
}

export function createJournal(
  body: JournalCreateRequest,
): Promise<JournalReadResponse> {
  return apiFetch<JournalReadResponse>("/trade-journals", {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export function updateJournal(
  id: number,
  body: JournalUpdateRequest,
): Promise<JournalReadResponse> {
  return apiFetch<JournalReadResponse>(`/trade-journals/${id}`, {
    method: "PATCH",
    body: JSON.stringify(body),
  });
}
