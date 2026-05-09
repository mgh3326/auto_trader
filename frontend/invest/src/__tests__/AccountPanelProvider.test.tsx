import { render, screen, waitFor } from "@testing-library/react";
import { vi, test, expect, beforeEach } from "vitest";
import { AccountPanelProvider, useAccountPanelContext } from "../desktop/AccountPanelProvider";
import * as panelApi from "../api/accountPanel";
import type { AccountPanelResponse } from "../types/invest";

const MOCK_RESP: AccountPanelResponse = {
  homeSummary: {
    includedSources: ["kis"],
    excludedSources: [],
    totalValueKrw: 2_000_000,
    pnlKrw: 100_000,
    pnlRate: 0.05,
  },
  accounts: [],
  groupedHoldings: [],
  watchSymbols: [],
  sourceVisuals: [],
  meta: { warnings: [], watchlistAvailable: true },
};

function StatusChild() {
  const ctx = useAccountPanelContext();
  if (ctx.loading) return <div data-testid="loading">loading</div>;
  if (ctx.error) return <div data-testid="error">{ctx.error}</div>;
  return <div data-testid="ready">{ctx.data?.homeSummary.totalValueKrw}</div>;
}

beforeEach(() => {
  vi.restoreAllMocks();
});

test("provides loaded data after fetch resolves", async () => {
  vi.spyOn(panelApi, "fetchAccountPanel").mockResolvedValue(MOCK_RESP);
  render(
    <AccountPanelProvider>
      <StatusChild />
    </AccountPanelProvider>,
  );
  expect(screen.getByTestId("loading")).toBeInTheDocument();
  await waitFor(() => expect(screen.getByTestId("ready")).toBeInTheDocument());
  expect(screen.getByTestId("ready").textContent).toBe("2000000");
});

test("shows error state when fetch fails", async () => {
  vi.spyOn(panelApi, "fetchAccountPanel").mockRejectedValue(new Error("network error"));
  render(
    <AccountPanelProvider>
      <StatusChild />
    </AccountPanelProvider>,
  );
  await waitFor(() => expect(screen.getByTestId("error")).toBeInTheDocument());
  expect(screen.getByTestId("error").textContent).toContain("network error");
});

test("throws when used outside provider", () => {
  const consoleError = vi.spyOn(console, "error").mockImplementation(() => {});
  expect(() => render(<StatusChild />)).toThrow();
  consoleError.mockRestore();
});
