import { freshnessBadgeLabel } from "./vm";
import type { CalendarSourceStatus } from "../../types/calendar";

export function CalendarFreshnessBanner({ sources }: { sources: CalendarSourceStatus[] }) {
  const stale = sources.filter((s) => s.state !== "fresh");
  if (stale.length === 0) return null;
  return (
    <div
      data-testid="calendar-freshness-banner"
      role="status"
      style={{
        display: "flex",
        flexWrap: "wrap",
        gap: 6,
        padding: "8px 12px",
        background: "var(--surface-2)",
        borderRadius: 10,
        fontSize: 12,
        color: "var(--fg-2)",
      }}
    >
      <span style={{ fontWeight: 700 }}>데이터 상태:</span>
      {stale.map((s) => (
        <span
          key={`${s.source}-${s.category}-${s.market}`}
          data-source={s.source}
          data-state={s.state}
          style={{
            padding: "2px 8px",
            borderRadius: 999,
            background: s.state === "failed" ? "#fef2f2" : "var(--surface-3, var(--surface-2))",
            color: s.state === "failed" ? "#ef4444" : "var(--fg-2)",
            fontWeight: 600,
          }}
        >
          {freshnessBadgeLabel(s)}
        </span>
      ))}
    </div>
  );
}
