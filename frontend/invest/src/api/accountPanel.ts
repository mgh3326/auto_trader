import type { AccountPanelResponse } from "../types/invest";

export async function fetchAccountPanel(): Promise<AccountPanelResponse> {
  const res = await fetch("/invest/api/account-panel", { credentials: "include" });
  if (!res.ok) throw new Error(`account-panel ${res.status}`);
  return res.json();
}
