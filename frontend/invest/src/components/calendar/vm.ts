import type {
  CalendarCluster,
  CalendarDay,
  CalendarDayState,
  CalendarEvent,
  CalendarSourceStatus,
} from "../../types/calendar";

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

export interface CalendarClusterVM {
  id: string;
  date: string;
  dayOfMonth: number;
  monthDay: string;
  type: DisplayEventType;
  region: DisplayRegion;
  title: string;
  count: number;
  topEvents: CalendarEventVM[];
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

export function calendarDayEventCount(day: CalendarDay): number {
  return day.events.length + day.clusters.reduce((sum, cluster) => sum + cluster.eventCount, 0);
}

export function toClusterVM(cluster: CalendarCluster, date: string): CalendarClusterVM {
  const day = Number.parseInt(date.slice(8, 10), 10);
  const month = Number.parseInt(date.slice(5, 7), 10);
  const type = mapEventType(cluster.eventType);
  const region = mapMarketToRegion(cluster.market);
  const count = cluster.eventCount;
  return {
    id: cluster.clusterId,
    date,
    dayOfMonth: day,
    monthDay: `${month}/${day}`,
    type,
    region,
    title: formatClusterTitle({ label: cluster.label, eventType: cluster.eventType, market: cluster.market, count }),
    count,
    topEvents: cluster.topEvents.map((event) => toEventVM(event, date)),
  };
}

function formatClusterTitle({
  label,
  eventType,
  market,
  count,
}: {
  label: string;
  eventType: CalendarCluster["eventType"];
  market: CalendarCluster["market"];
  count: number;
}): string {
  if (eventType === "earnings" && market === "us") return `미국 실적 발표 ${count}건`;
  if (eventType === "earnings" && market === "kr") return `국내 실적 발표 ${count}건`;
  if (eventType === "economic" && market === "global") return `글로벌 경제지표 ${count}건`;
  if (eventType === "economic") return `해외 경제지표 ${count}건`;
  if (eventType === "disclosure" && market === "kr") return `국내 공시 ${count}건`;
  return `${label} ${count}건`;
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

// --- ROB-165 month/grid helpers (Sunday-first 6x7 grid) ---

export function fmtLocal(d: Date): string {
  const y = d.getFullYear();
  const m = String(d.getMonth() + 1).padStart(2, "0");
  const day = String(d.getDate()).padStart(2, "0");
  return `${y}-${m}-${day}`;
}

export function startOfMonth(d: Date): Date {
  const out = new Date(d);
  out.setDate(1);
  out.setHours(0, 0, 0, 0);
  return out;
}

export function endOfMonth(d: Date): Date {
  const out = new Date(d);
  out.setMonth(out.getMonth() + 1);
  out.setDate(0); // last day of previous (i.e., target) month
  out.setHours(0, 0, 0, 0);
  return out;
}

export function addMonths(d: Date, n: number): Date {
  const out = new Date(d);
  out.setDate(1); // avoid month-end overflow (e.g. Jan 31 + 1m -> Mar 3)
  out.setMonth(out.getMonth() + n);
  return out;
}

// Sunday-aligned start of the 6-week grid containing `monthFirst`.
export function gridStartFromMonth(monthFirst: Date): Date {
  const start = startOfMonth(monthFirst);
  const dow = start.getDay(); // 0=Sun
  start.setDate(start.getDate() - dow);
  start.setHours(0, 0, 0, 0);
  return start;
}

// Always 41 days after gridStart (6 weeks - 1).
export function gridEndFromMonth(monthFirst: Date): Date {
  const start = gridStartFromMonth(monthFirst);
  const end = new Date(start);
  end.setDate(end.getDate() + 41);
  return end;
}

// Mon-aligned start of the week containing `date` (matches backend weekly-summary semantics).
export function weekStartOf(dateIso: string): string {
  const d = new Date(`${dateIso}T00:00:00`);
  const offset = (d.getDay() + 6) % 7; // Mon=0
  d.setDate(d.getDate() - offset);
  return fmtLocal(d);
}

export function monthLabel(monthFirstIso: string): string {
  const [, m] = monthFirstIso.split("-");
  return `${Number.parseInt(m ?? "0", 10)}월 금융 캘린더`;
}

export function monthTitleLabel(monthFirstIso: string): string {
  const [y, m] = monthFirstIso.split("-");
  return `${y}년 ${Number.parseInt(m ?? "0", 10)}월`;
}

export function selectedDateLabel(dateIso: string): string {
  const [, m, d] = dateIso.split("-");
  const dow = dayOfWeekLabel(dateIso);
  return `${Number.parseInt(m ?? "0", 10)}월 ${Number.parseInt(d ?? "0", 10)}일 ${dow}요일 일정`;
}

// --- ROB-166 KST + relative-date helpers ---

/**
 * Render an event time string for KST consumers.
 * - Backend `eventTimeLocal` (e.g. "오후 9시 발표 예정") is already KST and
 *   passes through unchanged.
 * - When the backend gave us nothing AND the row is unreleased, return a
 *   stable "발표 예정 · KST" placeholder so dense days don't flicker between
 *   `null` and `발표 예정`.
 */
export function formatKstTime(eventTimeLocal: string | null | undefined): string {
  const trimmed = (eventTimeLocal ?? "").trim();
  if (trimmed.length > 0) return trimmed;
  return "발표 예정 · KST";
}

/** "오늘" if dateIso === todayIso, "내일" if dateIso === todayIso + 1d, else null. */
export function relativeDayPrefix(dateIso: string, todayIso: string): string | null {
  if (dateIso === todayIso) return "오늘";
  const t = new Date(`${todayIso}T00:00:00`);
  t.setDate(t.getDate() + 1);
  if (fmtLocal(t) === dateIso) return "내일";
  return null;
}

export function selectedDateLabelWithRelative(dateIso: string, todayIso: string): string {
  const base = selectedDateLabel(dateIso);
  const prefix = relativeDayPrefix(dateIso, todayIso);
  return prefix == null ? base : `${prefix} · ${base}`;
}

/** Force `selectedDate` back into the month containing `monthCursor` if it drifted. */
export function clampSelectedDateToMonth(selectedDateIso: string, monthCursor: Date): string {
  const sel = new Date(`${selectedDateIso}T00:00:00`);
  if (
    sel.getFullYear() === monthCursor.getFullYear() &&
    sel.getMonth() === monthCursor.getMonth()
  ) {
    return selectedDateIso;
  }
  return fmtLocal(startOfMonth(monthCursor));
}

// --- ROB-185 grouped-monthly timeline helpers ---

const KOREAN_DOW: readonly string[] = ["일", "월", "화", "수", "목", "금", "토"];

export function monthDaysIso(monthCursor: Date): string[] {
  const first = startOfMonth(monthCursor);
  const last = endOfMonth(monthCursor);
  const out: string[] = [];
  const cur = new Date(first);
  while (cur.getTime() <= last.getTime()) {
    out.push(fmtLocal(cur));
    cur.setDate(cur.getDate() + 1);
  }
  return out;
}

export function dayHeaderLabel(dateIso: string, todayIso: string): string {
  const [, mStr, dStr] = dateIso.split("-");
  const month = Number.parseInt(mStr ?? "0", 10);
  const day = Number.parseInt(dStr ?? "0", 10);
  const dow = KOREAN_DOW[new Date(`${dateIso}T00:00:00`).getDay()] ?? "";
  const base = `${month}월 ${day}일 (${dow})`;
  const prefix = relativeDayPrefix(dateIso, todayIso);
  return prefix == null ? base : `${prefix} · ${base}`;
}

export function dayTotalLabel(total: number): string {
  if (total <= 0) return "";
  return `일정 ${total}개`;
}

export function dayEmptyLabel(): string {
  return "이 날은 예정된 일정이 없어요";
}

export function monthEmptyLabel(): string {
  return "이번 달은 예정된 주요 일정이 없어요";
}

const SOURCE_FRIENDLY_MAP: Record<string, string> = {
  finnhub: "미국 실적 일정",
  dart: "한국 공시",
  forexfactory: "경제 지표",
};

export function sourceFriendlyLabel(source: string): string {
  return SOURCE_FRIENDLY_MAP[source] ?? "기타 일정";
}

export function sourceStaleStatusCopy(status: CalendarSourceStatus): string | null {
  switch (status.state) {
    case "fresh":
      return null;
    case "stale":
      return "방금 업데이트되지 않았어요";
    case "failed":
    case "missing":
      return "잠시 후 다시 확인할게요";
  }
}

// --- ROB-167 freshness helpers ---

export function dataStateLabel(state: CalendarDayState): string {
  switch (state) {
    case "loaded":
      return "최신";
    case "empty":
      return "일정 없음";
    case "partial":
      return "일부 수집 중";
    case "missing":
      return "미수집";
    case "error":
      return "수집 실패";
    case "stale":
      return "오래된 데이터";
  }
}

export function freshnessBadgeLabel(status: CalendarSourceStatus): string {
  const sourceLabel: Record<string, string> = {
    finnhub: "Finnhub 실적",
    dart: "DART 공시",
    forexfactory: "ForexFactory 경제지표",
  };
  const label = sourceLabel[status.source] ?? status.source;
  switch (status.state) {
    case "fresh":
      return `${label} · 최신`;
    case "stale":
      return `${label} · 오래됨`;
    case "failed":
      return `${label} · 수집 실패`;
    case "missing":
      return `${label} · 미수집`;
  }
}
