import type { CalendarEventVM } from "./vm";
import { RegionBadge } from "./RegionBadge";
import { OwnershipTag } from "./OwnershipTag";

export function EventRow({ ev }: { ev: CalendarEventVM }) {
  return (
    <article
      data-testid="calendar-event"
      data-event-id={ev.id}
      data-event-type={ev.type}
      data-relation={ev.own ?? "none"}
      style={{
        display: "grid",
        gridTemplateColumns: "44px minmax(0, 1fr) 76px 76px 76px",
        alignItems: "center",
        gap: 10,
        padding: "10px 12px",
        borderRadius: 10,
        background: "transparent",
      }}
    >
      <div
        style={{
          fontSize: 12,
          fontWeight: 700,
          color: "var(--fg-1)",
          fontFeatureSettings: '"tnum"',
        }}
      >
        {ev.monthDay}
      </div>
      <div style={{ minWidth: 0 }}>
        <div style={{ display: "flex", alignItems: "center", gap: 8, minWidth: 0 }}>
          <RegionBadge region={ev.region} />
          <span
            style={{
              fontSize: 14,
              fontWeight: 600,
              color: "var(--fg)",
              overflow: "hidden",
              textOverflow: "ellipsis",
              whiteSpace: "nowrap",
              minWidth: 0,
              flex: 1,
            }}
          >
            {ev.title}
          </span>
          <OwnershipTag own={ev.own} />
        </div>
        <div
          style={{
            fontSize: 11,
            color: ev.released ? "var(--fg-2)" : "var(--fg-3)",
            marginTop: 2,
          }}
        >
          {ev.time ?? (ev.released ? "발표 완료" : "발표 예정")}
        </div>
      </div>
      <div
        style={{
          textAlign: "right",
          fontWeight: 700,
          color: ev.released ? "var(--fg)" : "var(--fg-3)",
          fontSize: 13,
          fontFeatureSettings: '"tnum"',
        }}
      >
        {ev.actual ?? "—"}
      </div>
      <div style={{ textAlign: "right", color: "var(--fg-2)", fontSize: 13, fontFeatureSettings: '"tnum"' }}>
        {ev.forecast ?? "—"}
      </div>
      <div style={{ textAlign: "right", color: "var(--fg-3)", fontSize: 13, fontFeatureSettings: '"tnum"' }}>
        {ev.previous ?? "—"}
      </div>
    </article>
  );
}
