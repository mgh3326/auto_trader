import { forwardRef } from "react";
import { ClusterEventRows } from "./ClusterEventRows";
import { EventRow } from "./EventRow";
import type { CalendarClusterVM, CalendarEventVM } from "./vm";
import { dayEmptyLabel, dayHeaderLabel, dayTotalLabel } from "./vm";

export interface DaySectionProps {
  dateIso: string;
  todayIso: string;
  events: CalendarEventVM[];
  clusters: CalendarClusterVM[];
  selected: boolean;
}

export const DaySection = forwardRef<HTMLElement, DaySectionProps>(function DaySection(
  { dateIso, todayIso, events, clusters, selected },
  ref,
) {
  const total = events.length + clusters.reduce((s, c) => s + c.count, 0);
  const isEmpty = events.length === 0 && clusters.length === 0;
  const headerLabel = dayHeaderLabel(dateIso, todayIso);

  return (
    <section
      ref={ref}
      data-testid="calendar-day-section"
      data-day-anchor={dateIso}
      data-selected={selected ? "true" : "false"}
      aria-label={headerLabel}
      className={
        selected
          ? "calendar-day-section calendar-day-section--selected"
          : "calendar-day-section"
      }
    >
      <header
        data-testid="calendar-day-section-header"
        className="calendar-day-section__header"
      >
        <span className="calendar-day-section__label">{headerLabel}</span>
        {total > 0 && (
          <span className="calendar-day-section__total">{dayTotalLabel(total)}</span>
        )}
      </header>
      {isEmpty ? (
        <div className="calendar-day-section__empty">{dayEmptyLabel()}</div>
      ) : (
        <div className="calendar-day-section__body">
          <ClusterEventRows clusters={clusters} />
          {events.map((ev) => (
            <EventRow key={ev.id} ev={ev} />
          ))}
        </div>
      )}
    </section>
  );
});
