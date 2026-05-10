import { fmtLocal, gridStartFromMonth, startOfMonth } from "./vm";

const WEEKDAY_LABELS = ["일", "월", "화", "수", "목", "금", "토"] as const;

export type MonthGridDensity = "comfortable" | "compact";

export interface MonthCalendarGridProps {
  monthCursor: Date;
  selectedDate: string;
  today: string;
  countByDate: Map<string, number>;
  onSelect: (date: string) => void;
  density?: MonthGridDensity;
  loading?: boolean;
}

function clampCount(n: number): string {
  if (n >= 1000) return "많음";
  return String(n);
}

function ariaLabel(iso: string, count: number, isToday: boolean): string {
  const [y, m, d] = iso.split("-");
  const y2 = Number.parseInt(y ?? "0", 10);
  const m2 = Number.parseInt(m ?? "0", 10);
  const d2 = Number.parseInt(d ?? "0", 10);
  const todayPart = isToday ? " (오늘)" : "";
  const countPart = count > 0 ? `, 일정 ${count}건` : "";
  return `${y2}년 ${m2}월 ${d2}일${todayPart}${countPart}`;
}

export function MonthCalendarGrid({
  monthCursor,
  selectedDate,
  today,
  countByDate,
  onSelect,
  density = "comfortable",
  loading = false,
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
    <div
      className="calendar-grid"
      data-testid="month-grid"
      data-density={density}
      role="grid"
      aria-label="월간 캘린더"
    >
      <div className="calendar-grid__weekdays" data-testid="month-grid-weekday-header" role="row">
        {WEEKDAY_LABELS.map((w) => (
          <span key={w} role="columnheader" aria-label={w}>{w}</span>
        ))}
      </div>
      <div className="calendar-grid__cells" role="rowgroup">
        {cells.map((c, idx) => {
          if (loading) {
            return (
              <div
                key={c.iso}
                data-testid={`month-grid-cell-skeleton-${idx}`}
                className="calendar-grid-cell calendar-grid-cell--skeleton"
                aria-hidden="true"
              />
            );
          }
          const isToday = c.iso === today;
          const isSelected = c.iso === selectedDate;
          const count = countByDate.get(c.iso) ?? 0;
          return (
            <button
              key={c.iso}
              type="button"
              className="calendar-grid-cell"
              data-testid={`month-grid-cell-${c.iso}`}
              data-date={c.iso}
              data-today={isToday ? "true" : "false"}
              data-selected={isSelected ? "true" : "false"}
              data-out-of-month={c.outOfMonth ? "true" : "false"}
              aria-current={isToday ? "date" : undefined}
              aria-pressed={isSelected ? "true" : "false"}
              aria-label={ariaLabel(c.iso, count, isToday)}
              onClick={() => onSelect(c.iso)}
            >
              <span className="calendar-grid-cell__day">{c.day}</span>
              {count > 0 && (
                <span className="calendar-grid-cell__count" aria-hidden="true">
                  {clampCount(count)}
                </span>
              )}
            </button>
          );
        })}
      </div>
    </div>
  );
}
