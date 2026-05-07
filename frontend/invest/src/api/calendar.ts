import type { CalendarResponse, CalendarTab, WeeklySummaryResponse } from "../types/calendar";

export async function fetchCalendar(params: {
  fromDate: string;
  toDate: string;
  tab?: CalendarTab;
}): Promise<CalendarResponse> {
  const q = new URLSearchParams();
  q.set("from_date", params.fromDate);
  q.set("to_date", params.toDate);
  if (params.tab) q.set("tab", params.tab);
  const res = await fetch(`/invest/api/calendar?${q}`, { credentials: "include" });
  if (!res.ok) throw new Error(`calendar ${res.status}`);
  return res.json();
}

export async function fetchWeeklySummary(weekStart: string): Promise<WeeklySummaryResponse> {
  const q = new URLSearchParams({ week_start: weekStart });
  const res = await fetch(`/invest/api/calendar/weekly-summary?${q}`, { credentials: "include" });
  if (!res.ok) throw new Error(`weekly-summary ${res.status}`);
  return res.json();
}
