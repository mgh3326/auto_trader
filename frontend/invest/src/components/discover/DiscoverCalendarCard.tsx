import { useState } from "react";
import { useDiscoverCalendar } from "../../hooks/useDiscoverCalendar";
import type {
  DiscoverCalendarDay,
  DiscoverCalendarEvent,
  DiscoverCalendarTab,
} from "../../types/marketEvents";

type Props = {
  fromDate: string;
  toDate: string;
  today: string;
};

const TAB_LABELS: Record<DiscoverCalendarTab, string> = {
  all: "전체",
  economic: "경제지표",
  earnings: "실적",
};

const BADGE_COLORS: Record<string, string> = {
  held: "var(--accent, #2962ff)",
  watched: "var(--info, #0288d1)",
  major: "var(--neutral-strong, #555)",
};

function dayOfMonth(iso: string): number {
  const [, , dd] = iso.split("-");
  return Number(dd);
}

function DayChip({
  day,
  active,
  onClick,
}: {
  day: DiscoverCalendarDay;
  active: boolean;
  onClick: () => void;
}) {
  const dom = dayOfMonth(day.date);
  return (
    <button
      type="button"
      onClick={onClick}
      aria-current={day.is_today ? "date" : undefined}
      aria-pressed={active}
      aria-label={`${day.weekday} ${dom}일`}
      style={{
        display: "flex",
        flexDirection: "column",
        alignItems: "center",
        padding: "6px 10px",
        borderRadius: 12,
        border: "1px solid var(--surface-2)",
        background: day.is_today
          ? "var(--accent, #2962ff)"
          : active
            ? "var(--surface-2)"
            : "transparent",
        color: day.is_today ? "#fff" : "inherit",
        fontSize: 12,
        minWidth: 44,
      }}
    >
      <span style={{ opacity: 0.8 }}>{day.weekday}</span>
      <strong>{dom}</strong>
    </button>
  );
}

function Badge({ label, priority }: { label: string; priority: string }) {
  const color = BADGE_COLORS[priority] ?? "var(--neutral-strong, #555)";
  return (
    <span
      style={{
        fontSize: 11,
        padding: "2px 6px",
        borderRadius: 999,
        background: color,
        color: "#fff",
      }}
    >
      {label}
    </span>
  );
}

function EventCard({ event }: { event: DiscoverCalendarEvent }) {
  return (
    <li
      style={{
        display: "flex",
        flexDirection: "column",
        gap: 4,
        padding: "8px 0",
        borderBottom: "1px solid var(--surface-2)",
      }}
    >
      <div style={{ display: "flex", justifyContent: "space-between", gap: 8, alignItems: "center" }}>
        <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
          {event.badge && <Badge label={event.badge} priority={event.priority} />}
          <strong style={{ fontSize: 13 }}>{event.title}</strong>
        </div>
        {event.time_label && (
          <span className="subtle" style={{ fontSize: 12 }}>{event.time_label}</span>
        )}
      </div>
      {event.subtitle && (
        <div className="subtle" style={{ fontSize: 12 }}>{event.subtitle}</div>
      )}
    </li>
  );
}

function DaySection({ day, focused }: { day: DiscoverCalendarDay; focused: boolean }) {
  return (
    <section
      style={{
        marginTop: 12,
        opacity: focused ? 1 : 0.65,
      }}
      aria-label={`${day.date} ${day.weekday}`}
    >
      <h3 style={{ fontSize: 13, margin: "0 0 4px 0" }}>
        {dayOfMonth(day.date)} {day.weekday}{day.is_today ? " · 오늘" : ""}
      </h3>
      {day.events.length === 0 ? (
        focused ? (
          <div className="subtle" style={{ fontSize: 12 }}>표시할 이벤트가 없습니다.</div>
        ) : null
      ) : (
        <ul style={{ listStyle: "none", padding: 0, margin: 0 }}>
          {day.events.map((e) => (
            <EventCard
              key={e.source_event_id ?? `${day.date}-${e.title}-${e.symbol ?? ""}`}
              event={e}
            />
          ))}
        </ul>
      )}
      {day.hidden_count > 0 && (
        <div style={{ marginTop: 4, fontSize: 12 }}>
          <span className="subtle">+{day.hidden_count}건 더보기</span>
        </div>
      )}
    </section>
  );
}

export function DiscoverCalendarCard({ fromDate, toDate, today }: Props) {
  const [tab, setTab] = useState<DiscoverCalendarTab>("all");
  const [activeDate, setActiveDate] = useState<string>(today);
  const { state, reload } = useDiscoverCalendar({
    fromDate,
    toDate,
    today,
    tab,
  });

  const days = state.status === "ready" ? state.data.days : [];

  return (
    <section
      aria-labelledby="discover-calendar-heading"
      style={{
        padding: 16,
        background: "var(--surface)",
        border: "1px solid var(--surface-2)",
        borderRadius: 14,
      }}
    >
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "baseline" }}>
        <h2 id="discover-calendar-heading" style={{ margin: 0, fontSize: 14, fontWeight: 700 }}>
          오늘의 주요 이벤트
        </h2>
        {state.status === "ready" && (
          <span className="subtle" style={{ fontSize: 12 }}>{state.data.week_label}</span>
        )}
      </div>

      {state.status === "ready" && state.data.headline && (
        <div className="subtle" style={{ marginTop: 4, fontSize: 12 }}>{state.data.headline}</div>
      )}

      <div style={{ display: "flex", gap: 8, marginTop: 8 }}>
        {(Object.keys(TAB_LABELS) as DiscoverCalendarTab[]).map((t) => (
          <button
            type="button"
            key={t}
            onClick={() => setTab(t)}
            aria-pressed={tab === t}
            style={{
              padding: "4px 10px",
              borderRadius: 999,
              border: "1px solid var(--surface-2)",
              background: tab === t ? "var(--surface-2)" : "transparent",
              fontSize: 12,
            }}
          >
            {TAB_LABELS[t]}
          </button>
        ))}
      </div>

      {state.status === "loading" && (
        <div className="subtle" style={{ marginTop: 8 }}>불러오는 중…</div>
      )}
      {state.status === "error" && (
        <div style={{ marginTop: 8 }}>
          <div>잠시 후 다시 시도해 주세요.</div>
          <button type="button" onClick={reload}>재시도</button>
          <div className="subtle">{state.message}</div>
        </div>
      )}

      {state.status === "ready" && (
        <>
          <div
            role="tablist"
            aria-label="주간 날짜"
            style={{ display: "flex", gap: 6, marginTop: 12, overflowX: "auto" }}
          >
            {days.map((d) => (
              <DayChip
                key={d.date}
                day={d}
                active={d.date === activeDate}
                onClick={() => setActiveDate(d.date)}
              />
            ))}
          </div>

          {days.map((d) => (
            <DaySection key={d.date} day={d} focused={d.date === activeDate} />
          ))}
        </>
      )}
    </section>
  );
}
