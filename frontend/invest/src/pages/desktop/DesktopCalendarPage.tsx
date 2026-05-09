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
import { ClusterRow } from "../../components/calendar/ClusterRow";
import { EmptyEventState } from "../../components/calendar/EmptyEventState";
import { EventDetailModal } from "../../components/calendar/EventDetailModal";
import {
  computeWeekLabel,
  dayOfWeekLabel,
  toClusterVM,
  toEventVM,
  type CalendarClusterVM,
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
  const year = d.getFullYear();
  const month = String(d.getMonth() + 1).padStart(2, "0");
  const day = String(d.getDate()).padStart(2, "0");
  return `${year}-${month}-${day}`;
}

type TypeFilter = "all" | DisplayEventType;
type RegionFilter = "all" | DisplayRegion;

type CalendarDaySection = {
  date: string;
  label: string;
  isSelected: boolean;
  events: CalendarEventVM[];
  clusters: CalendarClusterVM[];
  totalCount: number;
};

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
  const daySections: CalendarDaySection[] = useMemo(() => {
    const sections: CalendarDaySection[] = [];
    for (const d of days) {
      const events = d.events
        .map((event) => toEventVM(event, d.date))
        .filter((event) => matchesFilters(event, typeFilter, regionFilter));
      const clusters = d.clusters
        .map((cluster) => toClusterVM(cluster, d.date))
        .filter((cluster) => matchesFilters(cluster, typeFilter, regionFilter));
      if (events.length === 0 && clusters.length === 0) continue;
      const dayOfMonth = Number.parseInt(d.date.slice(8, 10), 10);
      sections.push({
        date: d.date,
        label: `${dayOfMonth} ${dayOfWeekLabel(d.date)}`,
        isSelected: d.date === selectedDate,
        events,
        clusters,
        totalCount: events.length + clusters.reduce((sum, cluster) => sum + cluster.count, 0),
      });
    }
    return sections;
  }, [days, regionFilter, selectedDate, typeFilter]);

  const weekLabel = computeWeekLabel(fmt(weekStart));

  return (
    <>
      <DesktopShell
        leftColumnWidth={300}
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
                  {weekLabel} 일정
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
                {daySections.length === 0 ? (
                  <EmptyEventState message="선택한 필터에 해당하는 이번 주 일정이 없습니다." />
                ) : (
                  daySections.map((section) => (
                    <section
                      key={section.date}
                      data-testid={`calendar-day-section-${section.date}`}
                      data-selected={section.isSelected ? "true" : "false"}
                      style={{ padding: "12px 8px 4px" }}
                    >
                      <div
                        style={{
                          display: "flex",
                          alignItems: "baseline",
                          gap: 8,
                          padding: "0 6px 8px",
                        }}
                      >
                        <h2 style={{ margin: 0, fontSize: 15, fontWeight: 800, color: "var(--fg)" }}>
                          {section.label}
                        </h2>
                        <span style={{ fontSize: 12, color: "var(--fg-3)", fontFeatureSettings: '"tnum"' }}>
                          {section.date} · {section.totalCount}건
                        </span>
                      </div>
                      <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
                        {section.clusters.map((cluster) => <ClusterRow key={cluster.id} cluster={cluster} />)}
                        {section.events.map((ev) => <EventRow key={ev.id} ev={ev} />)}
                      </div>
                    </section>
                  ))
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

function matchesFilters(
  item: { type: DisplayEventType; region: DisplayRegion },
  typeFilter: TypeFilter,
  regionFilter: RegionFilter,
): boolean {
  if (typeFilter !== "all" && item.type !== typeFilter) return false;
  if (regionFilter !== "all" && item.region !== regionFilter) return false;
  return true;
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
