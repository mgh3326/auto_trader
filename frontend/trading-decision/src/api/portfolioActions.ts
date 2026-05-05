// frontend/trading-decision/src/api/portfolioActions.ts
import { apiFetch } from "./client";
import type { Market, PortfolioActionsResponse } from "./types";

export function getPortfolioActions(
  market?: Market,
): Promise<PortfolioActionsResponse> {
  const qs = market ? `?market=${encodeURIComponent(market)}` : "";
  return apiFetch<PortfolioActionsResponse>(`/portfolio-actions${qs}`);
}
