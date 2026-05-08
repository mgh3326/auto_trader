import { useEffect, useMemo, useState } from "react";
import { MobileShell } from "../../mobile/MobileShell";
import { fetchCalendar, fetchWeeklySummary } from "../../api/calendar";
import type { CalendarResponse, WeeklySummaryResponse } from "../../types/calendar";
import { Icon } from "../../ds";
import { WeekDateStrip } from "../../components/calendar/WeekDateStrip";
import { EmptyEventState } from "../../components/calendar/EmptyEventState";
import { EventDetailModal } from "../../components/calendar/EventDetailModal";
import { RegionBadge } from "../../components/calendar/RegionBadge";
import { OwnershipTag } from "../../components/calendar/OwnershipTag";
import { SparkleIcon } from "../../components/calendar/SparkleIcon";
import {
  computeWeekLabel,
  toEventVM,
  type CalendarEventVM,
  type DisplayEventType,
} from "../../components/calendar/vm";

function startOfWeek(d: Date): Date {
  const out = new Date(d);
  const day = (out.getDay() + 6) % 7;
  out.setDate(out.getDate() - day);
  out.setHours(0, 0, 0, 0);
  return out;
}

function fmt(d: Date): string {
  return d.toISOString().slice(0, 10);
}

type TypeFilter = "all" | DisplayEventType;

export function MobileCalendarPage() {
  const [weekStart, setWeekStart] = useState<Date>(() => startOfWeek(new Date()));
  const weekEnd = useMemo(() => {
    const e = new Date(weekStart);
    e.setDate(e.getDate() + 6);
    return e;
  }, [weekStart]);
  const today = fmt(new Date());
  const [selectedDate, setSelectedDate] = useState<string>(today);
  const [calendar, setCalendar] = useState<CalendarResponse | undefined>();
  const [calendarErr, setCalendarErr] = useState<string | undefined>();
  const [summary, setSummary] = useState<WeeklySummaryResponse | undefined>();
  const [summaryErr, setSummaryErr] = useState<string | undefined>();
  const [summaryLoading, setSummaryLoading] = useState(false);
  const [showSummary, setShowSummary] = useState(false);
  const [typeFilter, setTypeFilter] = useState<TypeFilter>("all");

  useEffect(() => {
    let cancel = false;
    setCalendar(undefined);
    setCalendarErr(undefined);
    fetchCalendar({ fromDate: fmt(weekStart), toDate: fmt(weekEnd), tab: "all" })
      .then((r) => !cancel && setCalendar(r))
      .catch((e) => !cancel && setCalendarErr(String(e?.message ?? e)));
    return () => {
      cancel = true;
    };
  }, [weekStart, weekEnd]);

  useEffect(() => {
    if (!showSummary) return;
    if (summary && summary.weekStart === fmt(weekStart)) return;
    let cancel = false;
    setSummary(undefined);
    setSummaryErr(undefined);
    setSummaryLoading(true);
    fetchWeeklySummary(fmt(weekStart))
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
  }, [showSummary, weekStart]);

  const days = calendar?.days ?? [];
  const allVM: CalendarEventVM[] = useMemo(() => {
    const out: CalendarEventVM[] = [];
    for (const d of days) {
      for (const e of d.events) out.push(toEventVM(e, d.date));
    }
    return out;
  }, [days]);
  const filtered = useMemo(
    () => (typeFilter === "all" ? allVM : allVM.filter((e) => e.type === typeFilter)),
    [allVM, typeFilter],
  );
  const eventsForSelected = useMemo(
    () => filtered.filter((e) => e.date === selectedDate),
    [filtered, selectedDate],
  );

  const weekLabel = computeWeekLabel(fmt(weekStart));

  return (
    <>
      <MobileShell title="캘린더">
        <div style={{ padding: "12px 16px 24px", display: "flex", flexDirection: "column", gap: 14 }}>
          <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between" }}>
            <button
              type="button"
              data-testid="calendar-prev-week"
              aria-label="이전 주"
              onClick={() =>
                setWeekStart((w) => {
                  const n = new Date(w);
                  n.setDate(n.getDate() - 7);
                  return n;
                })
              }
              style={iconBtn}
            >
              <Icon name="chev" size={16} />
            </button>
            <div style={{ fontSize: 14, fontWeight: 700 }}>{weekLabel}</div>
            <button
              type="button"
              data-testid="calendar-next-week"
              aria-label="다음 주"
              onClick={() =>
                setWeekStart((w) => {
                  const n = new Date(w);
                  n.setDate(n.getDate() + 7);
                  return n;
                })
              }
              style={{ ...iconBtn, transform: "scaleX(-1)" }}
            >
              <Icon name="chev" size={16} />
            </button>
          </div>

          <WeekDateStrip days={days} selectedDate={selectedDate} onSelect={setSelectedDate} today={today} />

          <button
            type="button"
            data-testid="open-weekly-summary"
            onClick={() => setShowSummary(true)}
            style={{
              border: "none",
              background: "var(--surface-2)",
              padding: "10px 14px",
              borderRadius: 12,
              cursor: "pointer",
              display: "inline-flex",
              alignItems: "center",
              gap: 8,
              fontFamily: "inherit",
              fontSize: 13,
              fontWeight: 700,
              color: "var(--accent-press)",
              alignSelf: "flex-start",
            }}
          >
            <SparkleIcon size={14} />
            이번주 AI 요약
            <Icon name="chev" size={12} />
          </button>

          <div style={{ display: "flex", gap: 6 }}>
            {([
              ["all", "전체"],
              ["macro", "경제지표"],
              ["earnings", "실적"],
            ] as const).map(([k, l]) => {
              const on = typeFilter === k;
              return (
                <button
                  key={k}
                  type="button"
                  onClick={() => setTypeFilter(k)}
                  style={{
                    flex: "0 0 auto",
                    padding: "6px 12px",
                    border: "none",
                    borderRadius: 999,
                    cursor: "pointer",
                    background: on ? "var(--fg)" : "var(--surface-2)",
                    color: on ? "#fff" : "var(--fg-2)",
                    fontWeight: 600,
                    fontSize: 12,
                    fontFamily: "inherit",
                  }}
                >
                  {l}
                </button>
              );
            })}
          </div>

          {calendarErr && <div style={{ color: "var(--danger)" }}>오류: {calendarErr}</div>}

          <div data-testid="day-events" style={{ display: "flex", flexDirection: "column", gap: 4 }}>
            {eventsForSelected.length === 0 ? (
              <EmptyEventState message="해당 날짜에는 일정이 없습니다." />
            ) : (
              eventsForSelected.map((ev) => (
                <article
                  key={ev.id}
                  data-testid="calendar-event"
                  data-event-id={ev.id}
                  data-event-type={ev.type}
                  data-relation={ev.own ?? "none"}
                  style={{
                    display: "flex",
                    gap: 12,
                    padding: "10px 0",
                    borderBottom: "1px solid var(--divider)",
                  }}
                >
                  <div style={{ flex: 1, minWidth: 0 }}>
                    <div style={{ display: "flex", alignItems: "center", gap: 6, flexWrap: "wrap" }}>
                      <RegionBadge region={ev.region} />
                      <span style={{ fontSize: 14, fontWeight: 600, color: "var(--fg)" }}>{ev.title}</span>
                      <OwnershipTag own={ev.own} />
                    </div>
                    <div
                      style={{
                        fontSize: 12,
                        color: ev.released ? "var(--fg-2)" : "var(--fg-3)",
                        marginTop: 2,
                        fontFeatureSettings: '"tnum"',
                      }}
                    >
                      {ev.actual != null && `발표 ${ev.actual} · `}
                      {ev.forecast != null && `예측 ${ev.forecast} · `}
                      {ev.previous != null && `이전 ${ev.previous}`}
                      {ev.actual == null && ev.forecast == null && ev.previous == null && (ev.time ?? "발표 예정")}
                    </div>
                  </div>
                </article>
              ))
            )}
          </div>
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

const iconBtn: React.CSSProperties = {
  width: 32,
  height: 32,
  border: "none",
  background: "var(--surface-2)",
  borderRadius: 8,
  cursor: "pointer",
  color: "var(--fg-1)",
  display: "grid",
  placeItems: "center",
};
