import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { DiscoverCalendarCard } from "../components/discover/DiscoverCalendarCard";
import type {
  DiscoverCalendarResponse,
} from "../types/marketEvents";

function makeResponse(over: Partial<DiscoverCalendarResponse> = {}): DiscoverCalendarResponse {
  return {
    headline: null,
    week_label: "5월 1주차",
    from_date: "2026-05-04",
    to_date: "2026-05-10",
    today: "2026-05-07",
    tab: "all",
    days: [
      {
        date: "2026-05-07",
        weekday: "목",
        is_today: true,
        hidden_count: 0,
        events: [],
      },
      {
        date: "2026-05-08",
        weekday: "금",
        is_today: false,
        hidden_count: 0,
        events: [],
      },
    ],
    ...over,
  };
}

const fetchMock = vi.fn();

beforeEach(() => {
  fetchMock.mockReset();
  vi.stubGlobal("fetch", fetchMock);
});
afterEach(() => {
  vi.unstubAllGlobals();
});

test("renders loading state initially", () => {
  fetchMock.mockReturnValue(new Promise(() => {}));
  render(<DiscoverCalendarCard fromDate="2026-05-04" toDate="2026-05-10" today="2026-05-07" />);
  expect(screen.getByText(/불러오는 중/)).toBeInTheDocument();
});

test("renders week label and headline when present", async () => {
  fetchMock.mockResolvedValueOnce({
    ok: true,
    json: async () => makeResponse({ headline: "이번 주 주요 이벤트 3건이 예정되어 있어요" }),
  });
  render(<DiscoverCalendarCard fromDate="2026-05-04" toDate="2026-05-10" today="2026-05-07" />);
  expect(await screen.findByText(/5월 1주차/)).toBeInTheDocument();
  expect(screen.getByText(/이번 주 주요 이벤트 3건이 예정되어 있어요/)).toBeInTheDocument();
});

test("highlights today's day chip via aria-current", async () => {
  fetchMock.mockResolvedValueOnce({ ok: true, json: async () => makeResponse() });
  render(<DiscoverCalendarCard fromDate="2026-05-04" toDate="2026-05-10" today="2026-05-07" />);
  const todayChip = await screen.findByRole("button", { name: /목.*7/ });
  expect(todayChip).toHaveAttribute("aria-current", "date");
});

test("shows held badge and event subtitle", async () => {
  fetchMock.mockResolvedValueOnce({
    ok: true,
    json: async () =>
      makeResponse({
        days: [
          {
            date: "2026-05-07",
            weekday: "목",
            is_today: true,
            hidden_count: 0,
            events: [
              {
                title: "AAPL 실적발표",
                badge: "보유",
                category: "earnings",
                market: "us",
                symbol: "AAPL",
                subtitle: "EPS -0.34 · 예측 -0.52",
                time_label: "장 마감 후",
                priority: "held",
                source_event_id: "x",
              },
            ],
          },
        ],
      }),
  });
  render(<DiscoverCalendarCard fromDate="2026-05-04" toDate="2026-05-10" today="2026-05-07" />);
  expect(await screen.findByText("AAPL 실적발표")).toBeInTheDocument();
  expect(screen.getByText("보유")).toBeInTheDocument();
  expect(screen.getByText(/EPS -0\.34/)).toBeInTheDocument();
  expect(screen.getByText("장 마감 후")).toBeInTheDocument();
});

test("renders +N hidden footer when hidden_count > 0", async () => {
  fetchMock.mockResolvedValueOnce({
    ok: true,
    json: async () =>
      makeResponse({
        days: [
          {
            date: "2026-05-07",
            weekday: "목",
            is_today: true,
            hidden_count: 580,
            events: [],
          },
        ],
      }),
  });
  render(<DiscoverCalendarCard fromDate="2026-05-04" toDate="2026-05-10" today="2026-05-07" />);
  expect(await screen.findByText(/\+580건 더보기/)).toBeInTheDocument();
});

test("clicking economic tab refetches with tab=economic", async () => {
  fetchMock
    .mockResolvedValueOnce({ ok: true, json: async () => makeResponse({ tab: "all" }) })
    .mockResolvedValueOnce({ ok: true, json: async () => makeResponse({ tab: "economic" }) });

  render(<DiscoverCalendarCard fromDate="2026-05-04" toDate="2026-05-10" today="2026-05-07" />);
  await screen.findByText(/5월 1주차/);

  fireEvent.click(screen.getByRole("button", { name: "경제지표" }));

  await waitFor(() => {
    const calls = fetchMock.mock.calls.map((c) => c[0] as string);
    expect(calls.some((u) => u.includes("tab=economic"))).toBe(true);
  });
});

test("renders empty state when no events", async () => {
  fetchMock.mockResolvedValueOnce({ ok: true, json: async () => makeResponse() });
  render(<DiscoverCalendarCard fromDate="2026-05-04" toDate="2026-05-10" today="2026-05-07" />);
  expect(await screen.findByText(/표시할 이벤트가 없습니다/)).toBeInTheDocument();
});

test("renders error state and retry button on error", async () => {
  fetchMock.mockResolvedValueOnce({ ok: false, status: 503, json: async () => ({}) });
  render(<DiscoverCalendarCard fromDate="2026-05-04" toDate="2026-05-10" today="2026-05-07" />);
  expect(await screen.findByText(/잠시 후 다시 시도해 주세요/)).toBeInTheDocument();
  expect(screen.getByRole("button", { name: /재시도/ })).toBeInTheDocument();
});
