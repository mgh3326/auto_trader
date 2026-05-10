import { useEffect, useMemo, useState } from "react";
import { MobileShell } from "../../mobile/MobileShell";
import { fetchCalendar, fetchWeeklySummary } from "../../api/calendar";
import type { CalendarResponse, WeeklySummaryResponse } from "../../types/calendar";
import { Icon } from "../../ds";
import { CalendarMonthHeader } from "../../components/calendar/CalendarMonthHeader";
import { WeekDateStrip } from "../../components/calendar/WeekDateStrip";
import { SelectedDateEvents } from "../../components/calendar/SelectedDateEvents";
import { EventDetailModal } from "../../components/calendar/EventDetailModal";
import { SparkleIcon } from "../../components/calendar/SparkleIcon";
import {
  addMonths,
  clampSelectedDateToMonth,
  fmtLocal,
  gridEndFromMonth,
  gridStartFromMonth,
  monthTitleLabel,
  selectedDateLabelWithRelative,
  startOfMonth,
  toClusterVM,
  toEventVM,
  weekStartOf,
  type CalendarClusterVM,
  type CalendarEventVM,
  type DisplayEventType,
  type DisplayRegion,
} from "../../components/calendar/vm";
import type { CalendarDay } from "../../types/calendar";

type TypeFilter = "all" | DisplayEventType;
type RegionFilter = "all" | DisplayRegion;

function weekStartDateOf(dateIso: string): Date {
  const d = new Date(`${dateIso}T00:00:00`);
  const offset = (d.getDay() + 6) % 7; // Mon=0
  d.setDate(d.getDate() - offset);
  d.setHours(0, 0, 0, 0);
  return d;
}

function buildWeekDays(weekStart: Date, calendarDays: CalendarDay[]): CalendarDay[] {
  const byDate = new Map(calendarDays.map((d) => [d.date, d]));
  const out: CalendarDay[] = [];
  for (let i = 0; i < 7; i += 1) {
    const d = new Date(weekStart);
    d.setDate(d.getDate() + i);
    const iso = fmtLocal(d);
    out.push(byDate.get(iso) ?? { date: iso, events: [], clusters: [] });
  }
  return out;
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
  const gridStart = useMemo(() => gridStartFromMonth(monthCursor), [monthCursor]);
  const gridEnd = useMemo(() => gridEndFromMonth(monthCursor), [monthCursor]);
  const today = fmtLocal(new Date());

  const [selectedDate, setSelectedDate] = useState<string>(() => {
    const now = new Date();
    if (now.getFullYear() === monthCursor.getFullYear() && now.getMonth() === monthCursor.getMonth()) {
      return fmtLocal(now);
    }
    return fmtLocal(monthCursor);
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

  const weekDays = useMemo(
    () => buildWeekDays(weekStartDateOf(selectedDate), calendar?.days ?? []),
    [calendar?.days, selectedDate],
  );

  const filteredSelected = useMemo(() => {
    const day = (calendar?.days ?? []).find((d) => d.date === selectedDate);
    if (!day) return { events: [] as CalendarEventVM[], clusters: [] as CalendarClusterVM[] };
    const events = day.events
      .map((e) => toEventVM(e, day.date))
      .filter((e) => matches(e, typeFilter, regionFilter));
    const clusters = day.clusters
      .map((c) => toClusterVM(c, day.date))
      .filter((c) => matches(c, typeFilter, regionFilter));
    return { events, clusters };
  }, [calendar?.days, selectedDate, typeFilter, regionFilter]);

  const goPrevMonth = () => {
    setMonthCursor((m) => {
      const next = addMonths(m, -1);
      setSelectedDate((sel) => clampSelectedDateToMonth(sel, next));
      return next;
    });
  };
  const goNextMonth = () => {
    setMonthCursor((m) => {
      const next = addMonths(m, 1);
      setSelectedDate((sel) => clampSelectedDateToMonth(sel, next));
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
            onSelect={setSelectedDate}
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

          <SelectedDateEvents
            dateLabel={selectedDateLabelWithRelative(selectedDate, today)}
            dateIso={selectedDate}
            events={filteredSelected.events}
            clusters={filteredSelected.clusters}
            emptyMessage="해당 날짜에는 일정이 없습니다."
            loading={calendarLoading}
            error={calendarErr}
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
