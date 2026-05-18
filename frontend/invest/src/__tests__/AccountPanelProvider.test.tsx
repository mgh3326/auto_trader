import { act, render, screen, waitFor } from "@testing-library/react";
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

function StatusChild({ autoLoad }: { autoLoad?: boolean }) {
  const ctx = useAccountPanelContext();
  if (autoLoad && !ctx.data && !ctx.loading && !ctx.error) {
    ctx.load();
  }
  if (ctx.loading) return <div data-testid="loading">loading</div>;
  if (ctx.error) return <div data-testid="error">{ctx.error}</div>;
  if (ctx.data) return <div data-testid="ready">{ctx.data.homeSummary.totalValueKrw}</div>;
  return <div data-testid="idle">idle</div>;
}

beforeEach(() => {
  vi.restoreAllMocks();
});

test("does not fetch on mount", async () => {
  const spy = vi.spyOn(panelApi, "fetchAccountPanel").mockResolvedValue(MOCK_RESP);
  render(
    <AccountPanelProvider>
      <StatusChild />
    </AccountPanelProvider>,
  );
  expect(screen.getByTestId("idle")).toBeInTheDocument();
  await act(async () => {
    await Promise.resolve();
  });
  expect(spy).not.toHaveBeenCalled();
});

test("load() triggers fetch and populates data", async () => {
  vi.spyOn(panelApi, "fetchAccountPanel").mockResolvedValue(MOCK_RESP);
  render(
    <AccountPanelProvider>
      <StatusChild autoLoad />
    </AccountPanelProvider>,
  );
  await waitFor(() => expect(screen.getByTestId("ready")).toBeInTheDocument());
  expect(screen.getByTestId("ready").textContent).toBe("2000000");
});

test("reload() is a no-op when not yet loaded", async () => {
  const spy = vi.spyOn(panelApi, "fetchAccountPanel").mockResolvedValue(MOCK_RESP);
  function ReloadButton() {
    const ctx = useAccountPanelContext();
    return <button onClick={ctx.reload}>reload</button>;
  }
  render(
    <AccountPanelProvider>
      <ReloadButton />
    </AccountPanelProvider>,
  );
  screen.getByText("reload").click();
  await act(async () => {
    await Promise.resolve();
  });
  expect(spy).not.toHaveBeenCalled();
});

test("reload() re-fetches with last params after load", async () => {
  const spy = vi
    .spyOn(panelApi, "fetchAccountPanel")
    .mockResolvedValue(MOCK_RESP);
  function Controls() {
    const ctx = useAccountPanelContext();
    return (
      <>
        <button onClick={() => ctx.load({ includePaper: true, paperSources: ["kis_mock"] })}>load-kis-mock</button>
        <button onClick={ctx.reload}>reload</button>
      </>
    );
  }
  render(
    <AccountPanelProvider>
      <Controls />
    </AccountPanelProvider>,
  );
  screen.getByText("load-kis-mock").click();
  await waitFor(() => expect(spy).toHaveBeenCalledTimes(1));
  expect(spy).toHaveBeenLastCalledWith(
    expect.objectContaining({ includePaper: true, paperSources: ["kis_mock"] }),
  );
  screen.getByText("reload").click();
  await waitFor(() => expect(spy).toHaveBeenCalledTimes(2));
  expect(spy).toHaveBeenLastCalledWith(
    expect.objectContaining({ includePaper: true, paperSources: ["kis_mock"] }),
  );
});

test("shows error state when fetch fails", async () => {
  vi.spyOn(panelApi, "fetchAccountPanel").mockRejectedValue(new Error("network error"));
  render(
    <AccountPanelProvider>
      <StatusChild autoLoad />
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
