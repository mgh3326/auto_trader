import { useEffect, useMemo, useState } from "react";
import { DesktopShell } from "../../desktop/DesktopShell";
import { RightRemotePanel } from "../../desktop/RightRemotePanel";
import { useViewport } from "../../hooks/useViewport";
import { fetchCalendar, fetchWeeklySummary } from "../../api/calendar";
import type { CalendarResponse, WeeklySummaryResponse } from "../../types/calendar";
import { Card, Icon } from "../../ds";
import { AIWeeklyCard } from "../../components/calendar/AIWeeklyCard";
import { EventDetailModal } from "../../components/calendar/EventDetailModal";
import { MonthCalendarGrid } from "../../components/calendar/MonthCalendarGrid";
import { SelectedDateEvents } from "../../components/calendar/SelectedDateEvents";
import {
  addMonths,
  fmtLocal,
  gridEndFromMonth,
  gridStartFromMonth,
  monthLabel,
  monthTitleLabel,
  selectedDateLabel,
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
  const [calendarErr, setCalendarErr] = useState<string | undefined>();
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
    setCalendarErr(undefined);
    fetchCalendar({ fromDate: fmtLocal(gridStart), toDate: fmtLocal(gridEnd), tab: "all" })
      .then((r) => !cancel && setCalendar(r))
      .catch((e) => !cancel && setCalendarErr(String(e?.message ?? e)));
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
      map.set(d.date, { events, clusters, total });
    }
    return map;
  }, [calendar?.days, typeFilter, regionFilter]);

  const countByDate = useMemo<Map<string, number>>(() => {
    const m = new Map<string, number>();
    for (const [iso, day] of filteredByDate) m.set(iso, day.total);
    return m;
  }, [filteredByDate]);

  const selectedDay: FilteredDay = filteredByDate.get(selectedDate) ?? {
    events: [],
    clusters: [],
    total: 0,
  };

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
              <div
                style={{
                  display: "flex",
                  alignItems: "center",
                  justifyContent: "space-between",
                  marginBottom: 10,
                }}
              >
                <div style={{ fontSize: 14, fontWeight: 700, letterSpacing: "-0.01em" }}>
                  {monthTitleLabel(monthFirstIso)}
                </div>
                <div style={{ display: "flex", gap: 4 }}>
                  <button
                    type="button"
                    aria-label="이전 달"
                    data-testid="calendar-prev-month"
                    onClick={goPrevMonth}
                    style={navBtnStyle}
                  >
                    <Icon name="chev" size={14} />
                  </button>
                  <button
                    type="button"
                    aria-label="다음 달"
                    data-testid="calendar-next-month"
                    onClick={goNextMonth}
                    style={{ ...navBtnStyle, transform: "scaleX(-1)" }}
                  >
                    <Icon name="chev" size={14} />
                  </button>
                </div>
              </div>
              <MonthCalendarGrid
                monthCursor={monthCursor}
                selectedDate={selectedDate}
                today={today}
                countByDate={countByDate}
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

            {calendarErr && <div style={{ color: "var(--danger)" }}>오류: {calendarErr}</div>}

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
              </div>
              <div style={{ padding: "12px 8px 4px" }}>
                <SelectedDateEvents
                  dateLabel={selectedDateLabel(selectedDate)}
                  dateIso={selectedDate}
                  events={selectedDay.events}
                  clusters={selectedDay.clusters}
                  emptyMessage="선택한 날짜에 일정이 없습니다."
                />
              </div>
            </Card>
          </>
        }
        right={<RightRemotePanel />}
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

const navBtnStyle: React.CSSProperties = {
  width: 24,
  height: 24,
  border: "none",
  background: "transparent",
  borderRadius: 6,
  cursor: "pointer",
  color: "var(--fg-2)",
  display: "grid",
  placeItems: "center",
};

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
