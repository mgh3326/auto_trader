import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, expect, test, vi } from "vitest";
import { RetrospectivesPanel } from "../components/my/RetrospectivesPanel";
import type {
  RetrospectiveActionsResponse,
  RetrospectivesResponse,
} from "../types/retrospectives";

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

function actionsFixture(overrides: Partial<RetrospectiveActionsResponse> = {}): RetrospectiveActionsResponse {
  return {
    total: 1, count: 1, limit: 10, offset: 0, as_of: "2026-07-01T00:00:00Z",
    items: [{
      action_id: "a1", version: 1, action: "재진입 룰 재검토",
      owner: null, issue_id: null, status: "open", due_kst_date: "2026-07-10",
      overdue: false, status_changed_at: null, resolved_at: null,
      status_actor: null, status_source: null, status_reason: null,
      retrospective_id: 1, correlation_id: "c", symbol: "005930", market: "kr",
      trigger_type: "fill", outcome: "win", realized_pnl: 1000,
      created_at: "2026-07-01T00:00:00Z",
    }],
    ...overrides,
  };
}

function mockFetch(opts: { actions?: RetrospectiveActionsResponse } = {}) {
  const actions = opts.actions ?? actionsFixture();
  const calls: string[] = [];
  const fetchMock = vi.fn((url: string) => {
    calls.push(String(url));
    return Promise.resolve({
      ok: true,
      json: async () => (String(url).includes("/actions") ? actions : list),
    });
  });
  return { fetchMock, calls };
}

afterEach(() => vi.unstubAllGlobals());

test("renders canonical action queue and retrospective row", async () => {
  const { fetchMock } = mockFetch();
  vi.stubGlobal("fetch", fetchMock as unknown as typeof fetch);

  render(
    <MemoryRouter>
      <RetrospectivesPanel />
    </MemoryRouter>,
  );

  await waitFor(() => expect(screen.getByText("재진입 룰 재검토")).toBeInTheDocument());
  expect(screen.getByText(/분할 매수가 유효했다/)).toBeInTheDocument();
});

test("ROB-885: action request always sends status=open,in_progress explicitly", async () => {
  const { fetchMock, calls } = mockFetch();
  vi.stubGlobal("fetch", fetchMock as unknown as typeof fetch);

  render(
    <MemoryRouter>
      <RetrospectivesPanel />
    </MemoryRouter>,
  );
  await waitFor(() => expect(screen.getByText("재진입 룰 재검토")).toBeInTheDocument());

  const actionCall = calls.find((u) => u.includes("/actions"))!;
  const params = new URLSearchParams(actionCall.split("?")[1]);
  expect(params.get("status")).toBe("open,in_progress");
});

test("ROB-885: no legacy /next-actions network call", async () => {
  const { calls } = mockFetch();
  vi.stubGlobal("fetch", calls.length ? vi.fn() : vi.fn());

  const fresh = mockFetch();
  vi.stubGlobal("fetch", fresh.fetchMock as unknown as typeof fetch);

  render(
    <MemoryRouter>
      <RetrospectivesPanel />
    </MemoryRouter>,
  );
  await waitFor(() => expect(screen.getByText("재진입 룰 재검토")).toBeInTheDocument());

  expect(fresh.calls.some((u) => u.includes("/next-actions"))).toBe(false);
  expect(fresh.calls.some((u) => u.includes("/actions"))).toBe(true);
});

test("ROB-885: distinguishes open and in_progress with distinct labels", async () => {
  const { fetchMock } = mockFetch({
    actions: actionsFixture({
      items: [
        { ...actionsFixture().items[0]!, action_id: "a1", status: "open", action: "열린 작업" },
        { ...actionsFixture().items[0]!, action_id: "a2", status: "in_progress", action: "진행 작업" },
      ],
      count: 2, total: 2,
    }),
  });
  vi.stubGlobal("fetch", fetchMock as unknown as typeof fetch);

  render(
    <MemoryRouter>
      <RetrospectivesPanel />
    </MemoryRouter>,
  );

  await waitFor(() => expect(screen.getByText("열린 작업")).toBeInTheDocument());
  expect(screen.getByText("진행 작업")).toBeInTheDocument();
  expect(screen.getByText("예정")).toBeInTheDocument();
  expect(screen.getByText("진행중")).toBeInTheDocument();
});

test("ROB-885: shows owner, issue id, due date, and overdue badge", async () => {
  const { fetchMock } = mockFetch({
    actions: actionsFixture({
      items: [{
        ...actionsFixture().items[0]!, action_id: "a1", action: "소유자 액션",
        owner: "김개발", issue_id: "ROB-885", due_kst_date: "2026-07-10", overdue: true,
      }],
    }),
  });
  vi.stubGlobal("fetch", fetchMock as unknown as typeof fetch);

  render(
    <MemoryRouter>
      <RetrospectivesPanel />
    </MemoryRouter>,
  );

  await waitFor(() => expect(screen.getByText("소유자 액션")).toBeInTheDocument());
  expect(screen.getByText(/김개발/)).toBeInTheDocument();
  expect(screen.getByText(/ROB-885/)).toBeInTheDocument();
  expect(screen.getByText(/2026-07-10/)).toBeInTheDocument();
  expect(screen.getByText("지연")).toBeInTheDocument();
});

test("ROB-885: shows exact server total", async () => {
  const { fetchMock } = mockFetch({
    actions: actionsFixture({ total: 42, items: actionsFixture().items, count: 1 }),
  });
  vi.stubGlobal("fetch", fetchMock as unknown as typeof fetch);

  render(
    <MemoryRouter>
      <RetrospectivesPanel />
    </MemoryRouter>,
  );

  await waitFor(() => expect(screen.getByText(/미완료 액션 \(42\)/)).toBeInTheDocument());
});

test("ROB-885: shows dedicated action loading and error states", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn((url: string) => {
      if (String(url).includes("/actions")) {
        return Promise.resolve({ ok: false, status: 500 });
      }
      return Promise.resolve({ ok: true, json: async () => list });
    }) as unknown as typeof fetch,
  );

  render(
    <MemoryRouter>
      <RetrospectivesPanel />
    </MemoryRouter>,
  );

  await waitFor(() =>
    expect(screen.getByText(/액션을 불러오지 못했습니다/)).toBeInTheDocument(),
  );
});

test("ROB-885: shows action empty state when no active actions", async () => {
  const { fetchMock } = mockFetch({
    actions: actionsFixture({ total: 0, count: 0, items: [] }),
  });
  vi.stubGlobal("fetch", fetchMock as unknown as typeof fetch);

  render(
    <MemoryRouter>
      <RetrospectivesPanel />
    </MemoryRouter>,
  );

  await waitFor(() => expect(screen.getByText(/진행 중인 액션이 없습니다/)).toBeInTheDocument());
});

test("ROB-885: progressive expansion loads more actions via offset", async () => {
  const { fetchMock, calls } = mockFetch({
    actions: actionsFixture({
      total: 25, count: 10, limit: 10, offset: 0,
      items: Array.from({ length: 10 }, (_, i) => ({
        ...actionsFixture().items[0]!, action_id: `a${i}`, action: `액션${i}`,
      })),
    }),
  });
  vi.stubGlobal("fetch", fetchMock as unknown as typeof fetch);

  render(
    <MemoryRouter>
      <RetrospectivesPanel compact={false} />
    </MemoryRouter>,
  );

  await waitFor(() => expect(screen.getByText("액션0")).toBeInTheDocument());

  const moreBtn = await screen.findByRole("button", { name: /더 많은 액션 보기/ });
  fireEvent.click(moreBtn);

  await waitFor(() =>
    expect(calls.some((u) => u.includes("offset=10"))).toBe(true),
  );
});

test("ROB-885: filter change resets action offset to 0", async () => {
  const { calls } = mockFetch({
    actions: actionsFixture({ total: 25, count: 10, items: Array.from({ length: 10 }, (_, i) => ({ ...actionsFixture().items[0]!, action_id: `a${i}`, action: `액션${i}` })) }),
  });
  vi.stubGlobal("fetch", calls.length ? vi.fn() : vi.fn());

  const fresh = mockFetch({
    actions: actionsFixture({ total: 25, count: 10, items: Array.from({ length: 10 }, (_, i) => ({ ...actionsFixture().items[0]!, action_id: `a${i}`, action: `액션${i}` })) }),
  });
  vi.stubGlobal("fetch", fresh.fetchMock as unknown as typeof fetch);

  render(
    <MemoryRouter>
      <RetrospectivesPanel compact={false} />
    </MemoryRouter>,
  );
  await waitFor(() => expect(screen.getByText("액션0")).toBeInTheDocument());

  fireEvent.click(screen.getByRole("button", { name: "국내" }));

  await waitFor(() => {
    const actionCalls = fresh.calls.filter((u) => u.includes("/actions"));
    const last = actionCalls[actionCalls.length - 1]!;
    expect(new URLSearchParams(last.split("?")[1]).get("offset")).toBe("0");
    expect(new URLSearchParams(last.split("?")[1]).get("market")).toBe("kr");
  });
});

test("ROB-885: shared filters — outcome/symbol/date forwarded to action request", async () => {
  const { calls } = mockFetch();
  vi.stubGlobal("fetch", vi.fn());
  const fresh = mockFetch();
  vi.stubGlobal("fetch", fresh.fetchMock as unknown as typeof fetch);

  render(
    <MemoryRouter>
      <RetrospectivesPanel />
    </MemoryRouter>,
  );
  await waitFor(() => expect(screen.getByText(/분할 매수가 유효했다/)).toBeInTheDocument());

  fireEvent.click(screen.getByRole("button", { name: "승" }));
  await waitFor(() =>
    expect(fresh.calls.some((u) => u.includes("/actions") && u.includes("outcome_filter=win"))).toBe(true),
  );
});

test("ROB-885: no mutation/PATCH request is issued", async () => {
  const { fetchMock, calls } = mockFetch();
  vi.stubGlobal("fetch", fetchMock as unknown as typeof fetch);

  render(
    <MemoryRouter>
      <RetrospectivesPanel />
    </MemoryRouter>,
  );
  await waitFor(() => expect(screen.getByText("재진입 룰 재검토")).toBeInTheDocument());

  expect(calls.every((u) => !String(u).includes("PATCH") && !u.includes("/actions/"))).toBe(true);
});

test("retrospective crosslinks to its forecast by symbol key (ROB-682)", async () => {
  const listExec: RetrospectivesResponse = {
    ...list,
    items: [{ ...list.items[0]!, correlation_id: "toss_live:x" }],
  };
  const fetchMock = vi.fn((url: string) =>
    Promise.resolve({
      ok: true,
      json: async () => (String(url).includes("/actions") ? actionsFixture() : listExec),
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
  const { fetchMock, calls } = mockFetch();
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
  const { fetchMock, calls } = mockFetch();
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
  const { fetchMock, calls } = mockFetch();
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
  const { fetchMock } = mockFetch();
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
  expect(screen.getByRole("button", { name: "승" })).toBeInTheDocument();
});
