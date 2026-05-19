import { useEffect, useMemo, useState } from "react";
import { DesktopShell } from "../../desktop/DesktopShell";
import { useViewport } from "../../hooks/useViewport";
import { fetchCalendar, fetchWeeklySummary } from "../../api/calendar";
import type { CalendarDaySummary, CalendarResponse, WeeklySummaryResponse } from "../../types/calendar";
import { Card } from "../../ds";
import { AIWeeklyCard } from "../../components/calendar/AIWeeklyCard";
import { CalendarMonthHeader } from "../../components/calendar/CalendarMonthHeader";
import { CalendarSourceButton } from "../../components/calendar/CalendarSourceButton";
import { EventDetailModal } from "../../components/calendar/EventDetailModal";
import { MonthCalendarGrid, type MonthCellInfo } from "../../components/calendar/MonthCalendarGrid";
import { MonthlyEventsTimeline } from "../../components/calendar/MonthlyEventsTimeline";
import {
  addMonths,
  fmtLocal,
  gridEndFromMonth,
  gridStartFromMonth,
  monthLabel,
  monthTitleLabel,
  startOfMonth,
  toClusterVM,
  toEventVM,
  weekStartOf,
  type CalendarClusterVM,
  type CalendarEventVM,
  type DisplayEventType,
  type DisplayRegion,
} from "../../components/calendar/vm";
import { MobileCalendarPage } from "../mobile/MobileCalendarPage";

type TypeFilter = "all" | DisplayEventType;
type RegionFilter = "all" | DisplayRegion;

interface FilteredDay {
  events: CalendarEventVM[];
  clusters: CalendarClusterVM[];
  total: number;
  summary?: CalendarDaySummary | null;
}

export function CalendarRoute() {
  return useViewport() === "mobile" ? <MobileCalendarPage /> : <DesktopCalendarPage />;
}

export function DesktopCalendarPage() {
  const [monthCursor, setMonthCursor] = useState<Date>(() => startOfMonth(new Date()));
  const gridStart = useMemo(() => gridStartFromMonth(monthCursor), [monthCursor]);
  const gridEnd = useMemo(() => gridEndFromMonth(monthCursor), [monthCursor]);

  const today = fmtLocal(new Date());
  const monthFirstIso = fmtLocal(monthCursor);

  const [selectedDate, setSelectedDate] = useState<string>(() => {
    const now = new Date();
    if (now.getFullYear() === monthCursor.getFullYear() && now.getMonth() === monthCursor.getMonth()) {
      return fmtLocal(now);
    }
    return monthFirstIso;
  });

  const [calendar, setCalendar] = useState<CalendarResponse | undefined>();
  const [calendarLoading, setCalendarLoading] = useState(true);
  const [calendarErr, setCalendarErr] = useState<string | null>(null);
  const [summary, setSummary] = useState<WeeklySummaryResponse | undefined>();
  const [summaryErr, setSummaryErr] = useState<string | undefined>();
  const [summaryLoading, setSummaryLoading] = useState(false);
  const [showSummary, setShowSummary] = useState(false);
  const [typeFilter, setTypeFilter] = useState<TypeFilter>("all");
  const [regionFilter, setRegionFilter] = useState<RegionFilter>("all");

  // Fetch full grid range whenever month changes.
  useEffect(() => {
    let cancel = false;
    setCalendar(undefined);
    setCalendarLoading(true);
    setCalendarErr(null);
    fetchCalendar({ fromDate: fmtLocal(gridStart), toDate: fmtLocal(gridEnd), tab: "all" })
      .then((r) => {
        if (cancel) return;
        setCalendar(r);
        setCalendarLoading(false);
      })
      .catch((e) => {
        if (cancel) return;
        setCalendarErr(String(e?.message ?? e));
        setCalendarLoading(false);
      });
    return () => {
      cancel = true;
    };
  }, [gridStart, gridEnd]);

  // AI summary is keyed by the Mon-aligned week of the selected date.
  const summaryWeekStart = useMemo(() => weekStartOf(selectedDate), [selectedDate]);
  useEffect(() => {
    if (!showSummary) return;
    if (summary && summary.weekStart === summaryWeekStart) return;
    let cancel = false;
    setSummary(undefined);
    setSummaryErr(undefined);
    setSummaryLoading(true);
    fetchWeeklySummary(summaryWeekStart)
      .then((r) => {
        if (cancel) return;
        setSummary(r);
        setSummaryLoading(false);
      })
      .catch((e) => {
        if (cancel) return;
        setSummaryErr(String(e?.message ?? e));
        setSummaryLoading(false);
      });
    return () => {
      cancel = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [showSummary, summaryWeekStart]);

  // Build filtered-by-date map once per filter/data change.
  const filteredByDate = useMemo<Map<string, FilteredDay>>(() => {
    const map = new Map<string, FilteredDay>();
    for (const d of calendar?.days ?? []) {
      const events = d.events
        .map((event) => toEventVM(event, d.date))
        .filter((event) => matchesFilters(event, typeFilter, regionFilter));
      const clusters = d.clusters
        .map((cluster) => toClusterVM(cluster, d.date))
        .filter((cluster) => matchesFilters(cluster, typeFilter, regionFilter));
      const total = events.length + clusters.reduce((sum, c) => sum + c.count, 0);
      if (total === 0) continue;
      map.set(d.date, { events, clusters, total, summary: d.summary });
    }
    return map;
  }, [calendar?.days, typeFilter, regionFilter]);

  // Phase 1: every in-month day has data (42d fetch). Each filtered total
  // becomes "loaded-nonzero"; days absent from the map are "loaded-zero".
  // Phase 2 (step E) replaces this with the dayCache hook so "unloaded"
  // becomes a real third option.
  const cellByDate = useMemo<Map<string, MonthCellInfo>>(() => {
    const m = new Map<string, MonthCellInfo>();
    for (const [iso, day] of filteredByDate) {
      m.set(iso, { state: "loaded-nonzero", count: day.total });
    }
    return m;
  }, [filteredByDate]);

  const goPrevMonth = () => {
    setMonthCursor((m) => {
      const next = addMonths(m, -1);
      setSelectedDate(defaultSelectedDateForMonth(next, today));
      return next;
    });
  };
  const goNextMonth = () => {
    setMonthCursor((m) => {
      const next = addMonths(m, 1);
      setSelectedDate(defaultSelectedDateForMonth(next, today));
      return next;
    });
  };

  return (
    <>
      <DesktopShell
        leftColumnWidth={300}
        left={
          <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
            <Card style={{ padding: 16 }}>
              <div style={{ marginBottom: 10 }}>
                <CalendarMonthHeader
                  title={monthTitleLabel(monthFirstIso)}
                  onPrev={goPrevMonth}
                  onNext={goNextMonth}
                />
              </div>
              <MonthCalendarGrid
                monthCursor={monthCursor}
                selectedDate={selectedDate}
                today={today}
                cellByDate={cellByDate}
                onSelect={setSelectedDate}
              />
            </Card>

            <AIWeeklyCard
              summary={summary}
              loading={summaryLoading}
              onOpen={() => setShowSummary(true)}
              compact
            />
          </div>
        }
        center={
          <>
            <header>
              <h1 style={{ margin: 0, fontSize: 22, fontWeight: 800, letterSpacing: "-0.02em" }}>
                캘린더
              </h1>
              <p style={{ margin: "4px 0 0", fontSize: 13, color: "var(--fg-3)" }}>
                이번 달 실적·경제지표·주요 이벤트를 한눈에 확인하세요.
              </p>
            </header>

            <div style={{ display: "flex", gap: 10, alignItems: "center", flexWrap: "wrap" }}>
              <FilterGroup>
                {(
                  [
                    ["all", "전체"],
                    ["macro", "경제지표"],
                    ["earnings", "실적"],
                  ] as const
                ).map(([k, l]) => (
                  <SegPill key={k} on={typeFilter === k} onClick={() => setTypeFilter(k)}>
                    {l}
                  </SegPill>
                ))}
              </FilterGroup>
              <FilterGroup>
                {(
                  [
                    ["all", "전체"],
                    ["kr", "국내"],
                    ["us", "해외"],
                  ] as const
                ).map(([k, l]) => (
                  <SegPill key={k} on={regionFilter === k} onClick={() => setRegionFilter(k)}>
                    {l}
                  </SegPill>
                ))}
              </FilterGroup>
            </div>

            <Card style={{ padding: "16px 6px" }}>
              <div
                style={{
                  display: "flex",
                  alignItems: "baseline",
                  justifyContent: "space-between",
                  padding: "0 14px 10px",
                  borderBottom: "1px solid var(--divider)",
                }}
              >
                <div style={{ fontSize: 14, fontWeight: 800, color: "var(--fg)", letterSpacing: "-0.01em" }}>
                  {monthLabel(monthFirstIso)}
                </div>
                <CalendarSourceButton sources={calendar?.meta?.sourceFreshness ?? []} />
              </div>
              <div style={{ padding: "12px 8px 4px" }}>
                <MonthlyEventsTimeline
                  monthCursor={monthCursor}
                  selectedDate={selectedDate}
                  todayIso={today}
                  filteredByDate={filteredByDate}
                  loading={calendarLoading}
                  error={calendarErr}
                />
              </div>
            </Card>
          </>
        }
      />
      {showSummary && (
        <EventDetailModal
          summary={summary}
          loading={summaryLoading}
          error={summaryErr}
          onClose={() => setShowSummary(false)}
        />
      )}
    </>
  );
}

function matchesFilters(
  item: { type: DisplayEventType; region: DisplayRegion },
  typeFilter: TypeFilter,
  regionFilter: RegionFilter,
): boolean {
  if (typeFilter !== "all" && item.type !== typeFilter) return false;
  if (regionFilter !== "all" && item.region !== regionFilter) return false;
  return true;
}

function defaultSelectedDateForMonth(monthCursor: Date, todayIso: string): string {
  const monthFirst = startOfMonth(monthCursor);
  const todayDate = new Date(`${todayIso}T00:00:00`);
  if (
    todayDate.getFullYear() === monthFirst.getFullYear() &&
    todayDate.getMonth() === monthFirst.getMonth()
  ) {
    return todayIso;
  }
  return fmtLocal(monthFirst);
}

function FilterGroup({ children }: { children: React.ReactNode }) {
  return (
    <div style={{ display: "inline-flex", padding: 3, background: "var(--surface-2)", borderRadius: 999 }}>
      {children}
    </div>
  );
}

function SegPill({ on, children, onClick }: { on: boolean; children: React.ReactNode; onClick: () => void }) {
  return (
    <button
      type="button"
      onClick={onClick}
      style={{
        padding: "6px 14px",
        border: "none",
        borderRadius: 999,
        cursor: "pointer",
        background: on ? "var(--fg)" : "transparent",
        color: on ? "var(--bg)" : "var(--fg-2)",
        fontWeight: 600,
        fontSize: 13,
        fontFamily: "inherit",
        whiteSpace: "nowrap",
        flexShrink: 0,
      }}
    >
      {children}
    </button>
  );
}
