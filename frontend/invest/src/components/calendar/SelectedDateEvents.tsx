import { ClusterRow } from "./ClusterRow";
import { EventRow } from "./EventRow";
import { EmptyEventState } from "./EmptyEventState";
import type { CalendarClusterVM, CalendarEventVM } from "./vm";

export interface SelectedDateEventsProps {
  dateLabel: string;
  dateIso: string;
  events: CalendarEventVM[];
  clusters: CalendarClusterVM[];
  emptyMessage: string;
}

export function SelectedDateEvents({
  dateLabel,
  dateIso,
  events,
  clusters,
  emptyMessage,
}: SelectedDateEventsProps) {
  const total = events.length + clusters.reduce((s, c) => s + c.count, 0);
  return (
    <div
      data-testid="selected-date-events"
      data-selected-date={dateIso}
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
          {dateLabel}
        </h2>
        <span style={{ fontSize: 12, color: "var(--fg-3)", fontFeatureSettings: '"tnum"' }}>
          {dateIso} · {total}건
        </span>
      </div>
      {/* Keep `day-events` test id for cross-cutting tests that rely on it. */}
      <div data-testid="day-events" style={{ display: "flex", flexDirection: "column", gap: 4 }}>
        {events.length === 0 && clusters.length === 0 ? (
          <EmptyEventState message={emptyMessage} />
        ) : (
          <>
            {clusters.map((c) => (
              <ClusterRow key={c.id} cluster={c} />
            ))}
            {events.map((ev) => (
              <EventRow key={ev.id} ev={ev} />
            ))}
          </>
        )}
      </div>
    </div>
  );
}
