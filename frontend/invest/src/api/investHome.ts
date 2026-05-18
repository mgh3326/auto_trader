import type { InvestHomeResponse } from "../types/invest";

export interface FetchInvestHomeOptions {
  signal?: AbortSignal;
  includePaper?: boolean;
  paperSources?: readonly string[];
}

export async function fetchInvestHome(
  options: FetchInvestHomeOptions = {},
): Promise<InvestHomeResponse> {
  const params = new URLSearchParams();
  if (options.includePaper) {
    params.set("includePaper", "true");
  }
  if (options.paperSources && options.paperSources.length > 0) {
    params.set("paperSources", options.paperSources.join(","));
  }
  const qs = params.toString();
  const url = qs ? `/invest/api/home?${qs}` : "/invest/api/home";
  const res = await fetch(url, { credentials: "include", signal: options.signal });
  if (!res.ok) {
    throw new Error(`/invest/api/home ${res.status}`);
  }
  return (await res.json()) as InvestHomeResponse;
}
