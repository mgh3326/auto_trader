import { useEffect, useMemo, useState } from "react";
import { DesktopShell } from "../../desktop/DesktopShell";
import { RightRemotePanel } from "../../desktop/RightRemotePanel";
import { useViewport } from "../../hooks/useViewport";
import { fetchCalendar, fetchWeeklySummary } from "../../api/calendar";
import type { CalendarResponse, WeeklySummaryResponse } from "../../types/calendar";
import { Card, Icon } from "../../ds";
import { WeekDateStrip } from "../../components/calendar/WeekDateStrip";
import { AIWeeklyCard } from "../../components/calendar/AIWeeklyCard";
import { EventRow } from "../../components/calendar/EventRow";
import { EmptyEventState } from "../../components/calendar/EmptyEventState";
import { EventDetailModal } from "../../components/calendar/EventDetailModal";
import {
  computeWeekLabel,
  toEventVM,
  type CalendarEventVM,
  type DisplayEventType,
  type DisplayRegion,
} from "../../components/calendar/vm";
import { MobileCalendarPage } from "../mobile/MobileCalendarPage";

function startOfWeek(d: Date): Date {
  const out = new Date(d);
  const day = (out.getDay() + 6) % 7; // Mon = 0
  out.setDate(out.getDate() - day);
  out.setHours(0, 0, 0, 0);
  return out;
}

function fmt(d: Date): string {
  return d.toISOString().slice(0, 10);
}

type TypeFilter = "all" | DisplayEventType;
type RegionFilter = "all" | DisplayRegion;

export function CalendarRoute() {
  return useViewport() === "mobile" ? <MobileCalendarPage /> : <DesktopCalendarPage />;
}

export function DesktopCalendarPage() {
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
  const [regionFilter, setRegionFilter] = useState<RegionFilter>("all");

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
    // summary identity check above is intentional; we want a fresh fetch when weekStart changes.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [showSummary, weekStart]);

  const days = calendar?.days ?? [];
  const allEventsVM: CalendarEventVM[] = useMemo(() => {
    const out: CalendarEventVM[] = [];
    for (const d of days) {
      for (const e of d.events) out.push(toEventVM(e, d.date));
    }
    return out;
  }, [days]);

  const filteredEvents = useMemo(() => {
    return allEventsVM.filter((e) => {
      if (typeFilter !== "all" && e.type !== typeFilter) return false;
      if (regionFilter !== "all" && e.region !== regionFilter) return false;
      return true;
    });
  }, [allEventsVM, typeFilter, regionFilter]);

  const eventsBySelectedDate = useMemo(
    () => filteredEvents.filter((e) => e.date === selectedDate),
    [filteredEvents, selectedDate],
  );

  const weekLabel = computeWeekLabel(fmt(weekStart));

  return (
    <>
      <DesktopShell
      left={
        <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
          <Card style={{ padding: 16 }}>
            <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: 10 }}>
              <div style={{ fontSize: 14, fontWeight: 700, letterSpacing: "-0.01em" }}>{weekLabel}</div>
              <div style={{ display: "flex", gap: 4 }}>
                <button
                  type="button"
                  aria-label="이전 주"
                  data-testid="calendar-prev-week"
                  onClick={() =>
                    setWeekStart((w) => {
                      const n = new Date(w);
                      n.setDate(n.getDate() - 7);
                      return n;
                    })
                  }
                  style={navBtnStyle}
                >
                  <Icon name="chev" size={14} />
                </button>
                <button
                  type="button"
                  aria-label="다음 주"
                  data-testid="calendar-next-week"
                  onClick={() =>
                    setWeekStart((w) => {
                      const n = new Date(w);
                      n.setDate(n.getDate() + 7);
                      return n;
                    })
                  }
                  style={{ ...navBtnStyle, transform: "scaleX(-1)" }}
                >
                  <Icon name="chev" size={14} />
                </button>
              </div>
            </div>
            <WeekDateStrip
              days={days}
              selectedDate={selectedDate}
              onSelect={setSelectedDate}
              today={today}
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
            <h1 style={{ margin: 0, fontSize: 22, fontWeight: 800, letterSpacing: "-0.02em" }}>캘린더</h1>
            <p style={{ margin: "4px 0 0", fontSize: 13, color: "var(--fg-3)" }}>
              실적·경제지표·주요 이벤트를 한 주 단위로 확인하세요.
            </p>
          </header>

          <div style={{ display: "flex", gap: 10, alignItems: "center", flexWrap: "wrap" }}>
            <FilterGroup>
              {([
                ["all", "전체"],
                ["macro", "경제지표"],
                ["earnings", "실적"],
              ] as const).map(([k, l]) => (
                <SegPill key={k} on={typeFilter === k} onClick={() => setTypeFilter(k)}>
                  {l}
                </SegPill>
              ))}
            </FilterGroup>
            <FilterGroup>
              {([
                ["all", "전체"],
                ["kr", "국내"],
                ["us", "해외"],
              ] as const).map(([k, l]) => (
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
                display: "grid",
                gridTemplateColumns: "44px minmax(0, 1fr) 76px 76px 76px",
                alignItems: "center",
                gap: 10,
                padding: "0 14px 10px",
                borderBottom: "1px solid var(--divider)",
              }}
            >
              <div
                style={{
                  fontSize: 14,
                  fontWeight: 800,
                  color: "var(--fg)",
                  letterSpacing: "-0.01em",
                  gridColumn: "1 / 3",
                }}
              >
                {selectedDate}
              </div>
              <div style={{ textAlign: "right", fontSize: 11, fontWeight: 600, color: "var(--fg-3)", letterSpacing: "0.04em" }}>
                발표
              </div>
              <div style={{ textAlign: "right", fontSize: 11, fontWeight: 600, color: "var(--fg-3)", letterSpacing: "0.04em" }}>
                예측
              </div>
              <div style={{ textAlign: "right", fontSize: 11, fontWeight: 600, color: "var(--fg-3)", letterSpacing: "0.04em" }}>
                이전
              </div>
            </div>

            <div data-testid="day-events" style={{ paddingTop: 4 }}>
              {eventsBySelectedDate.length === 0 ? (
                <EmptyEventState message="해당 날짜에는 일정이 없습니다." />
              ) : (
                eventsBySelectedDate.map((ev) => <EventRow key={ev.id} ev={ev} />)
              )}
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
