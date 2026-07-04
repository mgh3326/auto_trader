import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, expect, test, vi } from "vitest";
import { RetrospectivesPanel } from "../components/my/RetrospectivesPanel";
import type { NextActionsResponse, RetrospectivesResponse } from "../types/retrospectives";

const list: RetrospectivesResponse = {
  market: "all", trigger_type: null, root_cause_class: null, symbol: null,
  outcome_filter: null, q: null, kst_date_from: null, kst_date_to: null,
  count: 1, total: 1, as_of: "2026-07-01T00:00:00Z",
  items: [{
    id: 1, correlation_id: "c", symbol: "005930", market: "kr",
    instrument_type: "equity_kr", side: "buy", trigger_type: "fill",
    root_cause_class: "analysis", outcome: "win", realized_pnl: 1000,
    realized_pnl_currency: "KRW", pnl_pct: 2.5, result_summary: null,
    lesson: "분할 매수가 유효했다", next_strategy: null,
    intended_vs_happened: null, next_actions: null, guardrail_fired: null,
    policy_version: null, created_at: "2026-07-01T00:00:00Z",
  }],
};
const na: NextActionsResponse = {
  market: "all", symbol: null, count: 1, scan_limit: 200,
  items: [{
    action: "재진입 룰 재검토", owner: null, issue_id: null, status: "open",
    due_kst_date: "2026-07-10", symbol: "005930", market: "kr", retro_id: 1,
    correlation_id: "c", trigger_type: "fill", realized_pnl: 1000,
    created_at: "2026-07-01T00:00:00Z",
  }],
};

afterEach(() => vi.unstubAllGlobals());

test("renders pinned next-action checklist and retrospective row", async () => {
  const fetchMock = vi.fn((url: string) =>
    Promise.resolve({
      ok: true,
      json: async () => (String(url).includes("next-actions") ? na : list),
    }),
  );
  vi.stubGlobal("fetch", fetchMock as unknown as typeof fetch);

  render(
    <MemoryRouter>
      <RetrospectivesPanel />
    </MemoryRouter>,
  );

  await waitFor(() => expect(screen.getByText("재진입 룰 재검토")).toBeInTheDocument());
  expect(screen.getByText(/분할 매수가 유효했다/)).toBeInTheDocument();
});

test("retrospective crosslinks to its forecast by symbol key (ROB-682)", async () => {
  // correlation_id is deliberately exec-style text ("toss_live:x") — the
  // crosslink no longer keys on it (ROB-678's exact-id scheme was
  // structurally dead since forecast/retro correlation_ids never overlap).
  const listExec: RetrospectivesResponse = {
    ...list,
    items: [{ ...list.items[0]!, correlation_id: "toss_live:x" }],
  };
  const fetchMock = vi.fn((url: string) =>
    Promise.resolve({
      ok: true,
      json: async () => (String(url).includes("next-actions") ? na : listExec),
    }),
  );
  vi.stubGlobal("fetch", fetchMock as unknown as typeof fetch);

  render(
    <MemoryRouter>
      <RetrospectivesPanel linkedSymbolKeys={new Set(["kr:005930"])} />
    </MemoryRouter>,
  );

  const link = await screen.findByRole("link", { name: "예측↑" });
  expect(link).toHaveAttribute("href", "#forecast-kr-005930");
  expect(document.getElementById("retro-kr-005930")).not.toBeNull();
});

test("outcome chip refetches with outcome_filter (ROB-691)", async () => {
  const calls: string[] = [];
  const fetchMock = vi.fn((url: string) => {
    calls.push(String(url));
    return Promise.resolve({
      ok: true,
      json: async () => (String(url).includes("next-actions") ? na : list),
    });
  });
  vi.stubGlobal("fetch", fetchMock as unknown as typeof fetch);

  render(
    <MemoryRouter>
      <RetrospectivesPanel />
    </MemoryRouter>,
  );
  await waitFor(() => expect(screen.getByText(/분할 매수가 유효했다/)).toBeInTheDocument());

  fireEvent.click(screen.getByRole("button", { name: "승" }));
  await waitFor(() =>
    expect(calls.some((u) => u.includes("outcome_filter=win"))).toBe(true),
  );
});

test("symbol search input refetches with q (debounced, ROB-691)", async () => {
  const calls: string[] = [];
  const fetchMock = vi.fn((url: string) => {
    calls.push(String(url));
    return Promise.resolve({
      ok: true,
      json: async () => (String(url).includes("next-actions") ? na : list),
    });
  });
  vi.stubGlobal("fetch", fetchMock as unknown as typeof fetch);

  render(
    <MemoryRouter>
      <RetrospectivesPanel />
    </MemoryRouter>,
  );
  await waitFor(() => expect(screen.getByText(/분할 매수가 유효했다/)).toBeInTheDocument());

  fireEvent.change(screen.getByPlaceholderText("종목 검색"), {
    target: { value: "005" },
  });
  await waitFor(() => expect(calls.some((u) => u.includes("q=005"))).toBe(true), {
    timeout: 2000,
  });
});

test("date range inputs refetch with kst_date_from/to (ROB-691)", async () => {
  const calls: string[] = [];
  const fetchMock = vi.fn((url: string) => {
    calls.push(String(url));
    return Promise.resolve({
      ok: true,
      json: async () => (String(url).includes("next-actions") ? na : list),
    });
  });
  vi.stubGlobal("fetch", fetchMock as unknown as typeof fetch);

  render(
    <MemoryRouter>
      <RetrospectivesPanel />
    </MemoryRouter>,
  );
  await waitFor(() => expect(screen.getByText(/분할 매수가 유효했다/)).toBeInTheDocument());

  fireEvent.change(screen.getByLabelText("시작일"), {
    target: { value: "2026-07-01" },
  });
  fireEvent.change(screen.getByLabelText("종료일"), {
    target: { value: "2026-07-04" },
  });
  await waitFor(() =>
    expect(
      calls.some(
        (u) => u.includes("kst_date_from=2026-07-01") && u.includes("kst_date_to=2026-07-04"),
      ),
    ).toBe(true),
  );
});

test("compact mode hides symbol search and date range controls", async () => {
  const fetchMock = vi.fn((url: string) =>
    Promise.resolve({
      ok: true,
      json: async () => (String(url).includes("next-actions") ? na : list),
    }),
  );
  vi.stubGlobal("fetch", fetchMock as unknown as typeof fetch);

  render(
    <MemoryRouter>
      <RetrospectivesPanel compact />
    </MemoryRouter>,
  );
  await waitFor(() => expect(screen.getByText(/분할 매수가 유효했다/)).toBeInTheDocument());

  expect(screen.queryByPlaceholderText("종목 검색")).not.toBeInTheDocument();
  expect(screen.queryByLabelText("시작일")).not.toBeInTheDocument();
  expect(screen.queryByLabelText("종료일")).not.toBeInTheDocument();
  // outcome chips remain visible even in compact mode (small footprint, like market chips)
  expect(screen.getByRole("button", { name: "승" })).toBeInTheDocument();
});