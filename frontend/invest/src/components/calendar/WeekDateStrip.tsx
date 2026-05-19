import { dayOfWeekLabel } from "./vm";

export interface WeekDateStripDay {
  date: string;
  eventCount: number;
}

export function WeekDateStrip({
  days,
  selectedDate,
  onSelect,
  today,
}: {
  days: WeekDateStripDay[];
  selectedDate: string;
  onSelect: (date: string) => void;
  today?: string;
}) {
  return (
    <div
      data-testid="week-date-strip"
      style={{
        display: "grid",
        gridTemplateColumns: "repeat(7, 1fr)",
        gap: 4,
        textAlign: "center",
      }}
    >
      {days.map((d) => {
        const day = Number.parseInt(d.date.slice(8, 10), 10);
        const dow = dayOfWeekLabel(d.date);
        const isSelected = d.date === selectedDate;
        const isToday = today != null && d.date === today;
        return (
          <button
            key={d.date}
            type="button"
            data-testid={`day-${d.date}`}
            onClick={() => onSelect(d.date)}
            style={{
              display: "flex",
              flexDirection: "column",
              alignItems: "center",
              gap: 4,
              padding: "8px 4px",
              border: "none",
              background: isSelected ? "var(--surface-2)" : "transparent",
              borderRadius: 8,
              cursor: "pointer",
              fontFamily: "inherit",
            }}
          >
            <span style={{ fontSize: 11, color: "var(--fg-3)", fontWeight: 600 }}>{dow}</span>
            <span
              style={{
                width: 28,
                height: 28,
                borderRadius: 999,
                display: "grid",
                placeItems: "center",
                background: isSelected ? "var(--accent)" : "transparent",
                color: isSelected ? "var(--fg-on-accent)" : isToday ? "var(--accent)" : "var(--fg-1)",
                fontWeight: isSelected || isToday ? 700 : 500,
                fontSize: 14,
                fontFeatureSettings: '"tnum"',
              }}
            >
              {day}
            </span>
            {d.eventCount > 0 && (
              <span style={{ fontSize: 10, color: "var(--fg-3)", fontWeight: 600 }}>{d.eventCount}</span>
            )}
          </button>
        );
      })}
    </div>
  );
}
