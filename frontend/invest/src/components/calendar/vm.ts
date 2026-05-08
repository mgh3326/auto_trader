import type { CalendarEvent } from "../../types/calendar";

export type DisplayEventType = "earnings" | "macro" | "other";
export type DisplayRegion = "kr" | "us";
export type DisplayOwnership = "holdings" | "watchlist" | "major" | null;

export interface CalendarEventVM {
  id: string;
  date: string;
  dayOfMonth: number;
  monthDay: string;
  type: DisplayEventType;
  region: DisplayRegion;
  title: string;
  time: string | null;
  released: boolean;
  actual: string | null;
  forecast: string | null;
  previous: string | null;
  own: DisplayOwnership;
  badges: string[];
}

export function mapEventType(eventType: CalendarEvent["eventType"]): DisplayEventType {
  if (eventType === "earnings") return "earnings";
  if (eventType === "economic") return "macro";
  return "other";
}

export function mapMarketToRegion(market: CalendarEvent["market"]): DisplayRegion {
  return market === "kr" ? "kr" : "us";
}

export function mapOwnership(event: CalendarEvent): DisplayOwnership {
  if (event.badges.includes("major")) return "major";
  if (event.relation === "held" || event.relation === "both") return "holdings";
  if (event.relation === "watchlist") return "watchlist";
  return null;
}

export function toEventVM(event: CalendarEvent, date: string): CalendarEventVM {
  const day = Number.parseInt(date.slice(8, 10), 10);
  const month = Number.parseInt(date.slice(5, 7), 10);
  return {
    id: event.eventId,
    date,
    dayOfMonth: day,
    monthDay: `${month}/${day}`,
    type: mapEventType(event.eventType),
    region: mapMarketToRegion(event.market),
    title: event.title,
    time: event.eventTimeLocal ?? null,
    released: event.actual != null,
    actual: event.actual ?? null,
    forecast: event.forecast ?? null,
    previous: event.previous ?? null,
    own: mapOwnership(event),
    badges: event.badges,
  };
}

export function computeWeekLabel(fromDate: string): string {
  const [, monthStr, dayStr] = fromDate.split("-");
  const month = Number.parseInt(monthStr ?? "0", 10);
  const day = Number.parseInt(dayStr ?? "0", 10);
  const weekOfMonth = Math.floor((day - 1) / 7) + 1;
  return `${month}월 ${weekOfMonth}주차`;
}

export function shortDateLabel(date: string): string {
  const [, monthStr, dayStr] = date.split("-");
  const month = Number.parseInt(monthStr ?? "0", 10);
  const day = Number.parseInt(dayStr ?? "0", 10);
  return `${month}/${day}`;
}

export function dayOfWeekLabel(date: string): string {
  const labels = ["일", "월", "화", "수", "목", "금", "토"];
  const idx = new Date(`${date}T00:00:00`).getDay();
  return labels[idx] ?? "";
}
