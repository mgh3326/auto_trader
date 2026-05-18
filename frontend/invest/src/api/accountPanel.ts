import type { AccountPanelResponse } from "../types/invest";

export interface FetchAccountPanelOptions {
  includePaper?: boolean;
  paperSources?: readonly string[];
  signal?: AbortSignal;
}

export async function fetchAccountPanel(
  options: FetchAccountPanelOptions = {},
): Promise<AccountPanelResponse> {
  const params = new URLSearchParams();
  if (options.includePaper) {
    params.set("includePaper", "true");
  }
  if (options.paperSources && options.paperSources.length > 0) {
    params.set("paperSources", options.paperSources.join(","));
  }
  const qs = params.toString();
  const url = qs ? `/invest/api/account-panel?${qs}` : "/invest/api/account-panel";
  const res = await fetch(url, { credentials: "include", signal: options.signal });
  if (!res.ok) throw new Error(`account-panel ${res.status}`);
  return res.json();
}
