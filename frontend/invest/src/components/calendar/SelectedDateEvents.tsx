import { ClusterEventRows } from "./ClusterEventRows";
import { EventRow } from "./EventRow";
import { EmptyEventState } from "./EmptyEventState";
import type { CalendarClusterVM, CalendarDaySummaryVM, CalendarEventVM } from "./vm";

export interface SelectedDateEventsProps {
  dateLabel: string;
  dateIso: string;
  events: CalendarEventVM[];
  clusters: CalendarClusterVM[];
  emptyMessage: string;
  loading?: boolean;
  error?: string | null;
  summary?: CalendarDaySummaryVM | null;
}

export function SelectedDateEvents({
  dateLabel,
  dateIso,
  events,
  clusters,
  emptyMessage,
  loading = false,
  error = null,
  summary = null,
}: SelectedDateEventsProps) {
  const total = events.length + clusters.reduce((s, c) => s + c.count, 0);

  return (
    <div
      className="calendar-selected-date"
      data-testid="selected-date-events"
      data-selected-date={dateIso}
    >
      <div className="calendar-selected-date__header">
        <h2 className="calendar-selected-date__label">{dateLabel}</h2>
        <span className="calendar-selected-date__meta">
          {dateIso} · {total}건{summary?.overflowLabel ? ` · ${summary.overflowLabel}` : ""}
        </span>
      </div>
      {!loading && !error && summary?.headline ? (
        <div data-testid="calendar-day-summary" className="calendar-selected-date__summary">
          {summary.headline}
        </div>
      ) : null}
      <div data-testid="day-events" className="calendar-selected-date__list">
        {loading ? (
          <SkeletonRows />
        ) : error ? (
          <ErrorBanner message={error} />
        ) : events.length === 0 && clusters.length === 0 ? (
          <EmptyEventState message={emptyMessage} />
        ) : (
          <>
            <ClusterEventRows clusters={clusters} />
            {events.map((ev) => (
              <EventRow key={ev.id} ev={ev} />
            ))}
          </>
        )}
      </div>
    </div>
  );
}

function SkeletonRows() {
  return (
    <div data-testid="calendar-loading" className="calendar-loading">
      {Array.from({ length: 3 }, (_, i) => (
        <div key={i} className="calendar-loading__row" aria-hidden="true" />
      ))}
      <span className="calendar-loading__sr">일정을 불러오는 중입니다…</span>
    </div>
  );
}

function ErrorBanner({ message }: { message: string }) {
  return (
    <div data-testid="calendar-error" role="alert" className="calendar-error">
      <strong className="calendar-error__title">일정을 불러올 수 없습니다</strong>
      <span className="calendar-error__detail">{message}</span>
    </div>
  );
}
