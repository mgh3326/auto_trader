import { apiFetch } from "./client";
import type {
  CreateFromResearchRunResponse,
  PreopenLatestResponse,
  Uuid,
} from "./types";

export function getLatestPreopen(
  marketScope: "kr" = "kr",
): Promise<PreopenLatestResponse> {
  return apiFetch<PreopenLatestResponse>(
    `/preopen/latest?market_scope=${encodeURIComponent(marketScope)}`,
  );
}

export function createDecisionFromResearchRun(args: {
  runUuid: Uuid;
}): Promise<CreateFromResearchRunResponse> {
  return apiFetch<CreateFromResearchRunResponse>("/decisions/from-research-run", {
    method: "POST",
    body: JSON.stringify({
      selector: { run_uuid: args.runUuid },
      include_tradingagents: false,
      notes: "Created from preopen dashboard",
    }),
  });
}
