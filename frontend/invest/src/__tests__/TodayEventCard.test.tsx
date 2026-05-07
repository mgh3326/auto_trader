import { fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { TodayEventCard } from "../components/discover/TodayEventCard";
import type {
  MarketEvent,
  MarketEventsDayResponse,
} from "../types/marketEvents";

function makeEvent(over: Partial<MarketEvent>): MarketEvent {
  return {
    category: "earnings",
    market: "us",
    country: null,
    currency: null,
    symbol: null,
    company_name: null,
    title: null,
    event_date: "2026-05-13",
    release_time_utc: null,
    time_hint: null,
    importance: null,
    status: "scheduled",
    source: "finnhub",
    source_event_id: null,
    source_url: null,
    fiscal_year: null,
    fiscal_quarter: null,
    held: null,
    watched: null,
    values: [],
    ...over,
  };
}

function makeResponse(events: MarketEvent[]): MarketEventsDayResponse {
  return { date: "2026-05-13", events };
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
  render(<TodayEventCard />);
  expect(screen.getByText(/불러오는 중/)).toBeInTheDocument();
});

test("renders empty state when there are no events", async () => {
  fetchMock.mockResolvedValueOnce({ ok: true, json: async () => makeResponse([]) });
  render(<TodayEventCard />);
  expect(await screen.findByText(/오늘 표시할 이벤트가 없습니다/)).toBeInTheDocument();
});

test("renders error state when the fetch fails", async () => {
  fetchMock.mockResolvedValueOnce({ ok: false, status: 503, json: async () => ({}) });
  render(<TodayEventCard />);
  expect(await screen.findByText(/잠시 후 다시 시도해 주세요/)).toBeInTheDocument();
});

test("filters by tab — economic shows only economic rows", async () => {
  fetchMock.mockResolvedValueOnce({
    ok: true,
    json: async () =>
      makeResponse([
        makeEvent({
          category: "economic",
          market: "global",
          currency: "USD",
          country: "US",
          title: "US CPI",
          source: "forexfactory",
          importance: 3,
          values: [
            {
              metric_name: "actual",
              period: "2026-05-13",
              actual: "0.3",
              forecast: "0.3",
              previous: "0.4",
              revised_previous: null,
              unit: "%",
              surprise: null,
              surprise_pct: null,
              released_at: null,
            },
          ],
        }),
        makeEvent({
          category: "earnings",
          symbol: "IONQ",
          title: "IONQ earnings release",
        }),
      ]),
  });

  render(<TodayEventCard />);
  expect(await screen.findByText("US CPI")).toBeInTheDocument();
  expect(screen.getByText(/IONQ/)).toBeInTheDocument();

  fireEvent.click(screen.getByRole("button", { name: "경제지표" }));
  expect(screen.getByText("US CPI")).toBeInTheDocument();
  expect(screen.queryByText(/IONQ earnings release/)).not.toBeInTheDocument();

  fireEvent.click(screen.getByRole("button", { name: "실적" }));
  expect(screen.queryByText("US CPI")).not.toBeInTheDocument();
  expect(screen.getByText(/IONQ earnings release/)).toBeInTheDocument();
});

test("renders forecast/previous/actual for economic events", async () => {
  fetchMock.mockResolvedValueOnce({
    ok: true,
    json: async () =>
      makeResponse([
        makeEvent({
          category: "economic",
          market: "global",
          currency: "USD",
          title: "US CPI",
          source: "forexfactory",
          values: [
            {
              metric_name: "actual",
              period: "2026-05-13",
              actual: "0.3",
              forecast: "0.3",
              previous: "0.4",
              revised_previous: null,
              unit: "%",
              surprise: null,
              surprise_pct: null,
              released_at: null,
            },
          ],
        }),
      ]),
  });

  render(<TodayEventCard />);
  expect(await screen.findByText(/예상/)).toBeInTheDocument();
  expect(screen.getByText(/이전/)).toBeInTheDocument();
  expect(screen.getByText(/실제/)).toBeInTheDocument();
  expect(screen.getByText(/0\.3/)).toBeInTheDocument();
});
