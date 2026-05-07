// frontend/invest/src/components/discover/TodayEventCard.tsx
import { useMemo, useState } from "react";
import { useMarketEventsToday } from "../../hooks/useMarketEventsToday";
import type { MarketEvent } from "../../types/marketEvents";

type Tab = "all" | "economic" | "earnings";

const TAB_LABELS: Record<Tab, string> = {
  all: "전체",
  economic: "경제지표",
  earnings: "실적",
};

function formatLocalTime(iso: string | null): string {
  if (!iso) return "";
  try {
    const d = new Date(iso);
    return d.toLocaleTimeString("ko-KR", { hour: "2-digit", minute: "2-digit" });
  } catch {
    return "";
  }
}

function EconomicRow({ event }: { event: MarketEvent }) {
  const value = event.values.find((v) => v.metric_name === "actual");
  const unit = value?.unit ?? "";
  const time = formatLocalTime(event.release_time_utc) || event.time_hint || "";
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
      <div style={{ display: "flex", justifyContent: "space-between", gap: 8 }}>
        <div style={{ display: "flex", alignItems: "center", gap: 4 }}>
          {event.currency && (
            <span className="subtle" style={{ fontSize: 12 }}>[{event.currency}]</span>
          )}
          <strong style={{ fontSize: 13 }}>{event.title}</strong>
        </div>
        <span className="subtle" style={{ fontSize: 12 }}>
          {time}
        </span>
      </div>
      {value && (
        <div
          className="subtle"
          style={{ fontSize: 12 }}
        >
          예상 {value.forecast ?? "-"}{unit} &nbsp; 이전 {value.previous ?? "-"}{unit} &nbsp; 실제 {value.actual ?? "-"}{unit}
        </div>
      )}
    </li>
  );
}

function EarningsRow({ event }: { event: MarketEvent }) {
  const eps = event.values.find((v) => v.metric_name === "eps");
  const time = formatLocalTime(event.release_time_utc) || event.time_hint || "";
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
      <div style={{ display: "flex", justifyContent: "space-between", gap: 8 }}>
        <strong style={{ fontSize: 13 }}>
          {event.symbol ?? ""} {event.title ?? ""}
        </strong>
        <span className="subtle" style={{ fontSize: 12 }}>
          {time}
        </span>
      </div>
      {eps && (
        <div className="subtle" style={{ fontSize: 12, display: "flex", gap: 12 }}>
          <span>EPS 예상 {eps.forecast ?? "-"}</span>
          <span>EPS 실제 {eps.actual ?? "-"}</span>
        </div>
      )}
    </li>
  );
}

function DisclosureRow({ event }: { event: MarketEvent }) {
  return (
    <li
      style={{
        padding: "8px 0",
        borderBottom: "1px solid var(--surface-2)",
        fontSize: 13,
      }}
    >
      <strong>{event.company_name ?? event.symbol ?? "공시"}</strong>{" "}
      <span className="subtle">{event.title ?? ""}</span>
    </li>
  );
}

function EventRow({ event }: { event: MarketEvent }) {
  if (event.category === "economic") return <EconomicRow event={event} />;
  if (event.category === "earnings") return <EarningsRow event={event} />;
  return <DisclosureRow event={event} />;
}

export function TodayEventCard() {
  const [tab, setTab] = useState<Tab>("all");
  const { state, reload } = useMarketEventsToday();

  const filtered = useMemo(() => {
    if (state.status !== "ready") return [];
    if (tab === "all") return state.data.events;
    return state.data.events.filter((e) => e.category === tab);
  }, [state, tab]);

  return (
    <section
      aria-labelledby="today-event-heading"
      style={{
        padding: 16,
        background: "var(--surface)",
        border: "1px solid var(--surface-2)",
        borderRadius: 14,
      }}
    >
      <h2
        id="today-event-heading"
        style={{ margin: 0, fontSize: 14, fontWeight: 700 }}
      >
        오늘의 주요 이벤트
      </h2>
      <div style={{ display: "flex", gap: 8, marginTop: 8 }}>
        {(Object.keys(TAB_LABELS) as Tab[]).map((t) => (
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
        <div className="subtle" style={{ marginTop: 8 }}>
          불러오는 중…
        </div>
      )}
      {state.status === "error" && (
        <div style={{ marginTop: 8 }}>
          <div>잠시 후 다시 시도해 주세요.</div>
          <button type="button" onClick={reload}>
            재시도
          </button>
          <div className="subtle">{state.message}</div>
        </div>
      )}
      {state.status === "ready" && filtered.length === 0 && (
        <div className="subtle" style={{ marginTop: 8 }}>
          오늘 표시할 이벤트가 없습니다.
        </div>
      )}
      {state.status === "ready" && filtered.length > 0 && (
        <ul
          style={{
            listStyle: "none",
            padding: 0,
            margin: "8px 0 0 0",
          }}
        >
          {filtered.map((event) => (
            <EventRow
              key={
                event.source_event_id ??
                `${event.source}::${event.category}::${event.symbol ?? ""}::${event.event_date}::${event.title ?? ""}`
              }
              event={event}
            />
          ))}
        </ul>
      )}
    </section>
  );
}
