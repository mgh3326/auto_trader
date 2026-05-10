import { fmtLocal, gridStartFromMonth, startOfMonth } from "./vm";

const WEEKDAY_LABELS = ["일", "월", "화", "수", "목", "금", "토"] as const;

export interface MonthCalendarGridProps {
  monthCursor: Date;
  selectedDate: string;
  today: string;
  countByDate: Map<string, number>;
  onSelect: (date: string) => void;
}

export function MonthCalendarGrid({
  monthCursor,
  selectedDate,
  today,
  countByDate,
  onSelect,
}: MonthCalendarGridProps) {
  const gridStart = gridStartFromMonth(monthCursor);
  const monthFirst = startOfMonth(monthCursor);
  const monthIndex = monthFirst.getMonth();

  const cells: { iso: string; day: number; outOfMonth: boolean }[] = [];
  for (let i = 0; i < 42; i += 1) {
    const d = new Date(gridStart);
    d.setDate(d.getDate() + i);
    cells.push({ iso: fmtLocal(d), day: d.getDate(), outOfMonth: d.getMonth() !== monthIndex });
  }

  return (
    <div data-testid="month-grid" style={{ display: "flex", flexDirection: "column", gap: 4 }}>
      <div
        data-testid="month-grid-weekday-header"
        style={{
          display: "grid",
          gridTemplateColumns: "repeat(7, 1fr)",
          fontSize: 11,
          fontWeight: 600,
          color: "var(--fg-3)",
          textAlign: "center",
          padding: "0 2px",
        }}
      >
        {WEEKDAY_LABELS.map((w) => (
          <span key={w}>{w}</span>
        ))}
      </div>
      <div
        style={{
          display: "grid",
          gridTemplateColumns: "repeat(7, 1fr)",
          gap: 4,
        }}
      >
        {cells.map((c) => {
          const isToday = c.iso === today;
          const isSelected = c.iso === selectedDate;
          const count = countByDate.get(c.iso) ?? 0;
          return (
            <button
              key={c.iso}
              type="button"
              data-testid={`month-grid-cell-${c.iso}`}
              data-date={c.iso}
              data-today={isToday ? "true" : "false"}
              data-selected={isSelected ? "true" : "false"}
              data-out-of-month={c.outOfMonth ? "true" : "false"}
              onClick={() => onSelect(c.iso)}
              style={{
                aspectRatio: "1 / 1",
                minHeight: 56,
                display: "flex",
                flexDirection: "column",
                alignItems: "center",
                justifyContent: "flex-start",
                gap: 2,
                padding: "8px 4px",
                border: "none",
                borderRadius: 10,
                cursor: "pointer",
                fontFamily: "inherit",
                background: isSelected ? "var(--surface-2)" : "transparent",
                opacity: c.outOfMonth ? 0.35 : 1,
              }}
            >
              <span
                style={{
                  width: 26,
                  height: 26,
                  borderRadius: 999,
                  display: "grid",
                  placeItems: "center",
                  background: isSelected ? "var(--accent)" : "transparent",
                  color: isSelected ? "var(--fg-on-accent)" : isToday ? "var(--accent)" : "var(--fg-1)",
                  fontWeight: isSelected || isToday ? 700 : 500,
                  fontSize: 13,
                  fontFeatureSettings: '"tnum"',
                }}
              >
                {c.day}
              </span>
              {count > 0 && (
                <span style={{ fontSize: 10, fontWeight: 600, color: "var(--fg-3)" }}>{count}</span>
              )}
            </button>
          );
        })}
      </div>
    </div>
  );
}
