import { apiFetch } from "./client";
import type {
  StrategyEventCreateRequest,
  StrategyEventDetail,
  StrategyEventListResponse,
  Uuid,
} from "./types";

export interface GetStrategyEventsParams {
  sessionUuid: Uuid;
  limit?: number;
  offset?: number;
}

export function getStrategyEvents(
  params: GetStrategyEventsParams,
): Promise<StrategyEventListResponse> {
  const limit = params.limit ?? 50;
  const offset = params.offset ?? 0;
  const qs = new URLSearchParams();
  qs.set("session_uuid", params.sessionUuid);
  qs.set("limit", String(limit));
  qs.set("offset", String(offset));
  return apiFetch<StrategyEventListResponse>(
    `/strategy-events?${qs.toString()}`,
  );
}

export function createStrategyEvent(
  body: StrategyEventCreateRequest,
): Promise<StrategyEventDetail> {
  return apiFetch<StrategyEventDetail>(`/strategy-events`, {
    method: "POST",
    body: JSON.stringify(body),
  });
}
