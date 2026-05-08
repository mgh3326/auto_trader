import { useEffect, useMemo, useState } from "react";
import { DesktopShell } from "../../desktop/DesktopShell";
import { RightAccountPanel } from "../../desktop/RightAccountPanel";
import { useAccountPanel } from "../../desktop/useAccountPanel";
import { fetchCalendar, fetchWeeklySummary } from "../../api/calendar";
import type { CalendarResponse, WeeklySummaryResponse } from "../../types/calendar";

function startOfWeek(d: Date): Date {
  const out = new Date(d);
  const day = (out.getDay() + 6) % 7; // Mon=0
  out.setDate(out.getDate() - day);
  out.setHours(0, 0, 0, 0);
  return out;
}

function fmt(d: Date) { return d.toISOString().slice(0, 10); }

export function DesktopCalendarPage() {
  const panel = useAccountPanel();
  const [weekStart, setWeekStart] = useState<Date>(() => startOfWeek(new Date()));
  const weekEnd = useMemo(() => {
    const e = new Date(weekStart);
    e.setDate(e.getDate() + 6);
    return e;
  }, [weekStart]);
  const [selectedDate, setSelectedDate] = useState<string>(fmt(new Date()));
  const [calendar, setCalendar] = useState<CalendarResponse | undefined>();
  const [summary, setSummary] = useState<WeeklySummaryResponse | undefined>();
  const [showSummary, setShowSummary] = useState(false);
  const [err, setErr] = useState<string | undefined>();

  useEffect(() => {
    let cancel = false;
    setErr(undefined);
    fetchCalendar({ fromDate: fmt(weekStart), toDate: fmt(weekEnd), tab: "all" })
      .then((r) => !cancel && setCalendar(r))
      .catch((e) => !cancel && setErr(String(e?.message ?? e)));
    return () => { cancel = true; };
  }, [weekStart, weekEnd]);

  useEffect(() => {
    if (!showSummary) return;
    fetchWeeklySummary(fmt(weekStart)).then(setSummary).catch((e) => setErr(String(e?.message ?? e)));
  }, [showSummary, weekStart]);

  const days = calendar?.days ?? [];
  const selectedDay = days.find((d) => d.date === selectedDate);

  return (
    <DesktopShell
      center={
        <div>
          <header style={{ display: "flex", gap: 8, alignItems: "center", marginBottom: 16 }}>
            <button onClick={() => setWeekStart((w) => { const n = new Date(w); n.setDate(n.getDate() - 7); return n; })}>이전 주</button>
            <strong>{fmt(weekStart)} ~ {fmt(weekEnd)}</strong>
            <button onClick={() => setWeekStart((w) => { const n = new Date(w); n.setDate(n.getDate() + 7); return n; })}>다음 주</button>
            <button data-testid="open-weekly-summary" onClick={() => setShowSummary((s) => !s)} style={{ marginLeft: "auto" }}>
              이번주 AI 요약 {showSummary ? "닫기" : "열기"}
            </button>
          </header>

          {err && <div style={{ color: "#f59e9e", marginBottom: 12 }}>오류: {err}</div>}

          <div style={{ display: "flex", gap: 4, marginBottom: 16 }}>
            {days.map((d) => (
              <button
                key={d.date}
                data-testid={`day-${d.date}`}
                onClick={() => setSelectedDate(d.date)}
                style={{
                  flex: 1, padding: "8px 4px", borderRadius: 6,
                  background: selectedDate === d.date ? "var(--surface-2, #1c1e24)" : "var(--surface, #15181f)",
                  border: "none", color: "#e8eaf0", cursor: "pointer", fontSize: 12,
                }}
              >
                {d.date.slice(5)}
                <div style={{ fontSize: 10, color: "#9ba0ab" }}>{d.events.length + d.clusters.length}</div>
              </button>
            ))}
          </div>

          <section data-testid="day-events" style={{ display: "flex", flexDirection: "column", gap: 8 }}>
            {selectedDay?.clusters.map((c) => (
              <details key={c.clusterId} style={{ padding: 12, borderRadius: 10, background: "var(--surface, #15181f)" }}>
                <summary style={{ cursor: "pointer" }}>{c.label} · {c.eventCount}건</summary>
                <ul style={{ listStyle: "none", padding: 0, margin: 0, marginTop: 8 }}>
                  {c.topEvents.map((ev) => (
                    <li key={ev.eventId} style={{ fontSize: 13 }}>{ev.title}</li>
                  ))}
                </ul>
              </details>
            ))}
            {selectedDay?.events.map((ev) => (
              <article key={ev.eventId} data-testid="calendar-event" data-relation={ev.relation} style={{ padding: 12, borderRadius: 10, background: "var(--surface, #15181f)" }}>
                <div style={{ display: "flex", justifyContent: "space-between", fontSize: 13, fontWeight: 600 }}>
                  <span>{ev.title}</span>
                  <span style={{ color: "#9ba0ab", fontSize: 11 }}>{ev.market.toUpperCase()} · {ev.eventType}</span>
                </div>
                {ev.badges.length > 0 && (
                  <div style={{ marginTop: 4, fontSize: 11, color: "#9ba0ab" }}>{ev.badges.join(" · ")}</div>
                )}
              </article>
            ))}
            {selectedDay && selectedDay.events.length === 0 && selectedDay.clusters.length === 0 && (
              <div style={{ padding: 16, color: "#9ba0ab" }}>해당 날짜 이벤트 없음</div>
            )}
          </section>

          {showSummary && (
            <section data-testid="weekly-summary" style={{ marginTop: 16, padding: 16, borderRadius: 12, background: "var(--surface, #15181f)" }}>
              <h3 style={{ marginTop: 0 }}>이번주 AI 요약</h3>
              {!summary && <div>로딩 중…</div>}
              {summary && summary.partial && (
                <div style={{ fontSize: 12, color: "#9ba0ab" }}>일부 일자가 비어있습니다: {summary.missingDates.join(", ")}</div>
              )}
              {summary?.sections.map((sec, i) => (
                <article key={i} style={{ marginTop: 12 }}>
                  <h4 style={{ margin: 0, fontSize: 14 }}>{sec.title}</h4>
                  <pre style={{ whiteSpace: "pre-wrap", fontSize: 12, color: "#cfd2da" }}>{sec.body}</pre>
                </article>
              ))}
            </section>
          )}
        </div>
      }
      right={<RightAccountPanel data={panel.data} loading={panel.loading} error={panel.error} />}
    />
  );
}
