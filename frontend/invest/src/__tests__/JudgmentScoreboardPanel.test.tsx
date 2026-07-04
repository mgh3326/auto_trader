import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import { JudgmentScoreboardPanel } from "../components/insights/JudgmentScoreboardPanel";
import type { ScoreboardResponse } from "../types/scoreboard";

const strategyResponse: ScoreboardResponse = {
  group_by: "strategy",
  market: "all",
  kst_date_from: "2026-04-06",
  kst_date_to: "2026-07-04",
  count: 2,
  as_of: "2026-07-04T00:00:00Z",
  groups: [
    {
      group: "A", sample_size: 3, wins: 2, misses: 1, win_rate_pct: 66.7,
      avg_pnl_pct: 1.1, realized_pnl_sum: { KRW: 50000 }, fx_pnl_krw_sum: 0,
      total_pnl_krw_sum: 50000, by_outcome: {}, by_trigger_type: {}, by_root_cause_class: {},
    },
    {
      group: "B", sample_size: 1, wins: 1, misses: 0, win_rate_pct: 100.0,
      avg_pnl_pct: 2.0, realized_pnl_sum: { USD: 10 }, fx_pnl_krw_sum: 0,
      total_pnl_krw_sum: 0, by_outcome: {}, by_trigger_type: {}, by_root_cause_class: {},
    },
  ],
  totals: {
    sample_size: 4, wins: 3, misses: 1, decided: 4, win_rate_pct: 75.0,
    realized_pnl_sum: { KRW: 50000, USD: 10 }, fx_pnl_krw_sum: 0,
    total_pnl_krw_sum: 50000, excluded_no_fill_evidence: 2,
  },
};

const dayResponse: ScoreboardResponse = {
  ...strategyResponse,
  group_by: "day",
  groups: [
    {
      group: "2026-07-04", sample_size: 4, wins: 3, misses: 1, win_rate_pct: 75.0,
      avg_pnl_pct: 1.3, realized_pnl_sum: { KRW: 50000, USD: 10 }, fx_pnl_krw_sum: 0,
      total_pnl_krw_sum: 50000, by_outcome: {}, by_trigger_type: {}, by_root_cause_class: {},
    },
  ],
};

const emptyResponse: ScoreboardResponse = {
  group_by: "strategy", market: "all", kst_date_from: null, kst_date_to: null,
  count: 0, as_of: "2026-07-04T00:00:00Z", groups: [],
  totals: {
    sample_size: 0, wins: 0, misses: 0, decided: 0, win_rate_pct: null,
    realized_pnl_sum: {}, fx_pnl_krw_sum: 0, total_pnl_krw_sum: 0,
    excluded_no_fill_evidence: 0,
  },
};

afterEach(() => vi.unstubAllGlobals());

test("renders headline totals + strategy breakdown", async () => {
  const fetchMock = vi.fn((url: string) => {
    const u = String(url);
    const body = u.includes("group_by=day") ? dayResponse : strategyResponse;
    return Promise.resolve({ ok: true, json: async () => body });
  });
  vi.stubGlobal("fetch", fetchMock as unknown as typeof fetch);

  render(<JudgmentScoreboardPanel />);

  // headline tiles (from totals, always group_by=strategy)
  await waitFor(() => expect(screen.getByText("75.0%")).toBeInTheDocument());
  const headline = within(screen.getByTestId("scoreboard-headline"));
  expect(headline.getByText(/결정 4건 중/)).toBeInTheDocument();
  expect(headline.getByText(/3승/)).toBeInTheDocument();
  expect(headline.getByText(/1패/)).toBeInTheDocument();
  expect(headline.getByText(/증거 부족 2건 제외/)).toBeInTheDocument();

  // breakdown table (default group_by=strategy) shows both groups
  const breakdown = within(screen.getByTestId("scoreboard-breakdown"));
  expect(breakdown.getByText("A")).toBeInTheDocument();
  expect(breakdown.getByText("B")).toBeInTheDocument();
});

test("group_by toggle refetches the breakdown with the new grouping", async () => {
  const calls: string[] = [];
  const fetchMock = vi.fn((url: string) => {
    const u = String(url);
    calls.push(u);
    const body = u.includes("group_by=day") ? dayResponse : strategyResponse;
    return Promise.resolve({ ok: true, json: async () => body });
  });
  vi.stubGlobal("fetch", fetchMock as unknown as typeof fetch);

  render(<JudgmentScoreboardPanel />);
  await waitFor(() => expect(screen.getByText("A")).toBeInTheDocument());

  fireEvent.click(screen.getByRole("button", { name: "일자" }));
  await waitFor(() => expect(screen.getByText("2026-07-04")).toBeInTheDocument());
  expect(calls.some((u) => u.includes("group_by=day"))).toBe(true);
});

test("market chip refetches both headline and breakdown", async () => {
  const calls: string[] = [];
  const fetchMock = vi.fn((url: string) => {
    const u = String(url);
    calls.push(u);
    const body = u.includes("group_by=day") ? dayResponse : strategyResponse;
    return Promise.resolve({ ok: true, json: async () => body });
  });
  vi.stubGlobal("fetch", fetchMock as unknown as typeof fetch);

  render(<JudgmentScoreboardPanel />);
  await waitFor(() => expect(screen.getByText("A")).toBeInTheDocument());

  fireEvent.click(screen.getByRole("button", { name: "국내" }));
  await waitFor(() =>
    expect(calls.some((u) => u.includes("market=kr"))).toBe(true),
  );
});

test("small decided sample shows a warning pill", async () => {
  const small: ScoreboardResponse = {
    ...strategyResponse,
    totals: { ...strategyResponse.totals, decided: 3, wins: 2, misses: 1 },
  };
  const fetchMock = vi.fn().mockResolvedValue({ ok: true, json: async () => small });
  vi.stubGlobal("fetch", fetchMock as unknown as typeof fetch);

  render(<JudgmentScoreboardPanel />);
  await waitFor(() => {
    const headline = within(screen.getByTestId("scoreboard-headline"));
    expect(headline.getByText(/소표본/)).toBeInTheDocument();
  });
});

test("empty totals reports emptiness via onEmptyChange", async () => {
  const fetchMock = vi.fn().mockResolvedValue({ ok: true, json: async () => emptyResponse });
  vi.stubGlobal("fetch", fetchMock as unknown as typeof fetch);
  const onEmptyChange = vi.fn();

  render(<JudgmentScoreboardPanel onEmptyChange={onEmptyChange} />);
  await waitFor(() => expect(onEmptyChange).toHaveBeenCalledWith(true));
});

test("fetch error surfaces an error message", async () => {
  const fetchMock = vi.fn().mockResolvedValue({ ok: false, status: 500 });
  vi.stubGlobal("fetch", fetchMock as unknown as typeof fetch);

  render(<JudgmentScoreboardPanel />);
  await waitFor(() => expect(screen.getAllByRole("alert").length).toBeGreaterThan(0));
});
