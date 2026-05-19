import { useCallback, useEffect, useMemo, useState } from "react";
import { MobileShell } from "../../mobile/MobileShell";
import { fetchCalendar, fetchWeeklySummary } from "../../api/calendar";
import type { WeeklySummaryResponse } from "../../types/calendar";
import { Icon } from "../../ds";
import { CalendarMonthHeader } from "../../components/calendar/CalendarMonthHeader";
import { CalendarSourceButton } from "../../components/calendar/CalendarSourceButton";
import { EventDetailModal } from "../../components/calendar/EventDetailModal";
import { MonthlyEventsTimeline } from "../../components/calendar/MonthlyEventsTimeline";
import { SparkleIcon } from "../../components/calendar/SparkleIcon";
import { WeekDateStrip, type WeekDateStripDay } from "../../components/calendar/WeekDateStrip";
import { useCalendarDayCache } from "../../components/calendar/useCalendarDayCache";
import {
  addMonths,
  fmtLocal,
  monthTitleLabel,
  startOfMonth,
  weekStartOf,
  type CalendarClusterVM,
  type CalendarEventVM,
  type DisplayEventType,
  type DisplayRegion,
} from "../../components/calendar/vm";

type TypeFilter = "all" | DisplayEventType;
type RegionFilter = "all" | DisplayRegion;

interface FilteredDay {
  events: CalendarEventVM[];
  clusters: CalendarClusterVM[];
  total: number;
}

const INITIAL_CHUNK_RADIUS = 3;

function weekStartDateOf(dateIso: string): Date {
  const d = new Date(`${dateIso}T00:00:00`);
  const offset = (d.getDay() + 6) % 7; // Mon=0
  d.setDate(d.getDate() - offset);
  d.setHours(0, 0, 0, 0);
  return d;
}

function matches(
  item: { type: DisplayEventType; region: DisplayRegion },
  typeFilter: TypeFilter,
  regionFilter: RegionFilter,
): boolean {
  if (typeFilter !== "all" && item.type !== typeFilter) return false;
  if (regionFilter !== "all" && item.region !== regionFilter) return false;
  return true;
}

export function MobileCalendarPage() {
  const [monthCursor, setMonthCursor] = useState<Date>(() => startOfMonth(new Date()));
  const today = fmtLocal(new Date());

  const [selectedDate, setSelectedDate] = useState<string>(() => {
    const now = new Date();
    if (now.getFullYear() === monthCursor.getFullYear() && now.getMonth() === monthCursor.getMonth()) {
      return fmtLocal(now);
    }
    return fmtLocal(monthCursor);
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

  const filteredByDate = useMemo<Map<string, FilteredDay>>(() => {
    const map = new Map<string, FilteredDay>();
    for (const [iso, day] of cache.byDate) {
      if (day.kind !== "loaded") continue;
      const events = day.events.filter((event) =>
        matches(event, typeFilter, regionFilter),
      );
      const clusters = day.clusters.filter((cluster) =>
        matches(cluster, typeFilter, regionFilter),
      );
      const total = events.length + clusters.reduce((s, c) => s + c.count, 0);
      if (total === 0) continue;
      map.set(iso, { events, clusters, total });
    }
    return map;
  }, [cache.byDate, typeFilter, regionFilter]);

  const weekDays: WeekDateStripDay[] = useMemo(() => {
    const start = weekStartDateOf(selectedDate);
    const out: WeekDateStripDay[] = [];
    for (let i = 0; i < 7; i += 1) {
      const d = new Date(start);
      d.setDate(d.getDate() + i);
      const iso = fmtLocal(d);
      // Pull the *filtered* total so the strip count tracks the active
      // type/region filter (matches the timeline's "이 날은 ..." rendering).
      const total = filteredByDate.get(iso)?.total ?? 0;
      out.push({ date: iso, eventCount: total });
    }
    return out;
  }, [filteredByDate, selectedDate]);

  const selectedDayState = cache.byDate.get(selectedDate);
  const calendarErr =
    selectedDayState?.kind === "error" ? selectedDayState.reason : null;
  const calendarLoading =
    calendarErr == null &&
    (selectedDayState == null ||
      selectedDayState.kind === "unloaded" ||
      selectedDayState.kind === "loading");

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
      <MobileShell title="캘린더">
        <div className="calendar-mobile">
          <CalendarMonthHeader
            title={monthTitleLabel(fmtLocal(monthCursor))}
            onPrev={goPrevMonth}
            onNext={goNextMonth}
          />

          <WeekDateStrip
            days={weekDays}
            selectedDate={selectedDate}
            onSelect={(iso) => {
              setSelectedDate(iso);
              ensureRange(iso, iso);
            }}
            today={today}
          />

          <button
            type="button"
            data-testid="open-weekly-summary"
            onClick={() => setShowSummary(true)}
            className="calendar-mobile__ai-btn"
          >
            <SparkleIcon size={14} />
            이번주 AI 요약
            <Icon name="chev" size={12} />
          </button>

          <div data-testid="calendar-mobile-filters" className="calendar-mobile-filters">
            {(
              [
                ["all", "전체"],
                ["macro", "경제지표"],
                ["earnings", "실적"],
              ] as const
            ).map(([k, l]) => {
              const on = typeFilter === k;
              return (
                <button
                  key={k}
                  type="button"
                  className="calendar-pill"
                  data-on={on ? "true" : "false"}
                  aria-pressed={on}
                  onClick={() => setTypeFilter(k)}
                >
                  {l}
                </button>
              );
            })}
          </div>

          <div style={{ display: "flex", justifyContent: "flex-end", padding: "0 8px" }}>
            <CalendarSourceButton sources={[]} />
          </div>
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
      </MobileShell>
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
