import { useCallback, useEffect, useMemo, useState } from "react";
import { DesktopShell } from "../../desktop/DesktopShell";
import { useViewport } from "../../hooks/useViewport";
import { fetchCalendar, fetchWeeklySummary } from "../../api/calendar";
import type { CalendarSourceStatus, WeeklySummaryResponse } from "../../types/calendar";
import { Card } from "../../ds";
import { AIWeeklyCard } from "../../components/calendar/AIWeeklyCard";
import { CalendarMonthHeader } from "../../components/calendar/CalendarMonthHeader";
import { CalendarSourceButton } from "../../components/calendar/CalendarSourceButton";
import { EventDetailModal } from "../../components/calendar/EventDetailModal";
import { MonthCalendarGrid, type MonthCellInfo } from "../../components/calendar/MonthCalendarGrid";
import { MonthlyEventsTimeline } from "../../components/calendar/MonthlyEventsTimeline";
import { dayDisplayState } from "../../components/calendar/dayCache";
import { useCalendarDayCache } from "../../components/calendar/useCalendarDayCache";
import {
  addMonths,
  fmtLocal,
  monthDaysIso,
  monthLabel,
  monthTitleLabel,
  startOfMonth,
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

const INITIAL_CHUNK_RADIUS = 3;

export function CalendarRoute() {
  return useViewport() === "mobile" ? <MobileCalendarPage /> : <DesktopCalendarPage />;
}

export function DesktopCalendarPage() {
  const [monthCursor, setMonthCursor] = useState<Date>(() => startOfMonth(new Date()));

  const today = fmtLocal(new Date());
  const monthFirstIso = fmtLocal(monthCursor);

  const [selectedDate, setSelectedDate] = useState<string>(() => {
    const now = new Date();
    if (now.getFullYear() === monthCursor.getFullYear() && now.getMonth() === monthCursor.getMonth()) {
      return fmtLocal(now);
    }
    return monthFirstIso;
  });

  const { cache, ensureRange } = useCalendarDayCache({
    monthCursor,
    selectedDate,
    initialChunkRadius: INITIAL_CHUNK_RADIUS,
    fetchFn: fetchCalendar,
  });

  const [summary, setSummary] = useState<WeeklySummaryResponse | undefined>();
  const [summaryErr, setSummaryErr] = useState<string | undefined>();
  const [summaryLoading, setSummaryLoading] = useState(false);
  const [showSummary, setShowSummary] = useState(false);
  const [typeFilter, setTypeFilter] = useState<TypeFilter>("all");
  const [regionFilter, setRegionFilter] = useState<RegionFilter>("all");

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

  // Derived: filtered day rows for the timeline (only days we've actually loaded
  // and that have events matching the active filters land here).
  const filteredByDate = useMemo<Map<string, FilteredDay>>(() => {
    const map = new Map<string, FilteredDay>();
    for (const [iso, day] of cache.byDate) {
      if (day.kind !== "loaded") continue;
      const events = day.events.filter((event) =>
        matchesFilters(event, typeFilter, regionFilter),
      );
      const clusters = day.clusters.filter((cluster) =>
        matchesFilters(cluster, typeFilter, regionFilter),
      );
      const total = events.length + clusters.reduce((sum, c) => sum + c.count, 0);
      if (total === 0) continue;
      map.set(iso, { events, clusters, total });
    }
    return map;
  }, [cache.byDate, typeFilter, regionFilter]);

  // Derived: per-cell display state for the grid. "unloaded" is meaningful in
  // Phase 2 — a day we never fetched is rendered as a placeholder, not 0.
  const cellByDate = useMemo<Map<string, MonthCellInfo>>(() => {
    const m = new Map<string, MonthCellInfo>();
    for (const iso of monthDaysIso(monthCursor)) {
      const state = dayDisplayState(cache, iso);
      if (state === "loaded-nonzero") {
        const filtered = filteredByDate.get(iso);
        if (filtered && filtered.total > 0) {
          m.set(iso, { state: "loaded-nonzero", count: filtered.total });
        } else {
          // Loaded with data but filter hid everything — show as loaded-zero.
          m.set(iso, { state: "loaded-zero", count: 0 });
        }
      } else {
        m.set(iso, { state, count: 0 });
      }
    }
    return m;
  }, [cache, filteredByDate, monthCursor]);

  // Page-level loading/error: derive from the selectedDate's day-cache state.
  // The first ±3 fetch is the gate that flips the timeline from skeleton to
  // populated; per-day lazy loads after that do not re-enter the skeleton.
  const selectedDayState = cache.byDate.get(selectedDate);
  const calendarErr =
    selectedDayState?.kind === "error" ? selectedDayState.reason : null;
  const calendarLoading =
    calendarErr == null &&
    (selectedDayState == null ||
      selectedDayState.kind === "unloaded" ||
      selectedDayState.kind === "loading");

  // Source freshness comes from the most recent successful response. For now
  // we display whatever the last fetch returned — coverage stitching across
  // chunks is a follow-up if it becomes visually important.
  const [sourceFreshness, setSourceFreshness] = useState<CalendarSourceStatus[]>([]);
  useEffect(() => {
    // Update freshness once any day is loaded.
    const anyLoaded = Array.from(cache.byDate.values()).some(
      (s) => s.kind === "loaded" || s.kind === "empty",
    );
    if (!anyLoaded) return;
    // We don't currently surface per-chunk freshness; leave empty until a
    // follow-up wires it through the dayCache payload.
    setSourceFreshness((prev) => prev);
  }, [cache.byDate]);

  const onVisibleDaysChange = useCallback(
    (visibleIsos: string[]) => {
      if (visibleIsos.length === 0) return;
      ensureRange(visibleIsos[0]!, visibleIsos[visibleIsos.length - 1]!);
    },
    [ensureRange],
  );

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
                onSelect={(iso) => {
                  // Out-of-month grid cells (the gray prev/next-month days at
                  // the top and bottom of the 6-week grid) must NOT be silent-
                  // clamped — the user clicked a real date, so jump to that
                  // month. monthCursor change kicks off the hook's anchor ±3
                  // fetch for the new selectedDate; we still ensureRange for
                  // the clicked day so even mid-month clicks within the same
                  // month load instantly via the single-day lazy fetch.
                  const isoDate = new Date(`${iso}T00:00:00`);
                  if (
                    isoDate.getFullYear() !== monthCursor.getFullYear() ||
                    isoDate.getMonth() !== monthCursor.getMonth()
                  ) {
                    setMonthCursor(startOfMonth(isoDate));
                  }
                  setSelectedDate(iso);
                  ensureRange(iso, iso);
                }}
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
                <CalendarSourceButton sources={sourceFreshness} />
              </div>
              <div style={{ padding: "12px 8px 4px" }}>
                <MonthlyEventsTimeline
                  monthCursor={monthCursor}
                  selectedDate={selectedDate}
                  todayIso={today}
                  filteredByDate={filteredByDate}
                  loading={calendarLoading}
                  error={calendarErr}
                  onVisibleDaysChange={onVisibleDaysChange}
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
