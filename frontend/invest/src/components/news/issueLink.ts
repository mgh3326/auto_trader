import type { FeedTab } from "../../types/feedNews";
import type { MarketIssuesMarketFilter } from "../../types/newsIssues";

export function issueNamespaceForTab(tab: FeedTab): MarketIssuesMarketFilter {
  return tab === "kr" || tab === "us" || tab === "crypto" ? tab : "all";
}

export function issueDetailHref(
  prefix: string,
  issueId: string,
  market: MarketIssuesMarketFilter = "all",
): string {
  const params = new URLSearchParams({ market });
  return `${prefix}/${encodeURIComponent(issueId)}?${params.toString()}`;
}
