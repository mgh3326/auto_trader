import { useEffect, useMemo, useRef } from "react";
import { DaySection } from "./DaySection";
import type { CalendarClusterVM, CalendarEventVM } from "./vm";
import { monthDaysIso, monthEmptyLabel } from "./vm";

export interface MonthlyDay {
  events: CalendarEventVM[];
  clusters: CalendarClusterVM[];
  total: number;
}

export interface MonthlyEventsTimelineProps {
  monthCursor: Date;
  selectedDate: string;
  todayIso: string;
  filteredByDate: Map<string, MonthlyDay>;
  loading?: boolean;
  error?: string | null;
}

export function MonthlyEventsTimeline({
  monthCursor,
  selectedDate,
  todayIso,
  filteredByDate,
  loading = false,
  error = null,
}: MonthlyEventsTimelineProps) {
  const days = useMemo(() => monthDaysIso(monthCursor), [monthCursor]);
  const refs = useRef<Map<string, HTMLElement | null>>(new Map());

  // First effective render = first time we render real day sections (not loading/error).
  // Until then we should not scroll: refs aren't populated and the page is still hydrating.
  const hasScrolledOnceRef = useRef(false);

  useEffect(() => {
    if (loading || error) return;
    const node = refs.current.get(selectedDate);
    if (!node) return;
    // Some test environments (older jsdom) leave scrollIntoView undefined;
    // guard so the page never crashes if the platform lacks it.
    if (typeof node.scrollIntoView !== "function") return;
    const reduceMotion =
      typeof window !== "undefined" &&
      typeof window.matchMedia === "function" &&
      window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    // First-mount scroll is instant so the page doesn't visibly drift on load.
    // Subsequent selectedDate changes are treated as user-initiated navigation and animate.
    const isFirstEffectiveScroll = !hasScrolledOnceRef.current;
    hasScrolledOnceRef.current = true;
    node.scrollIntoView({
      behavior: isFirstEffectiveScroll || reduceMotion ? "auto" : "smooth",
      block: "start",
    });
  }, [selectedDate, loading, error]);

  if (loading) {
    return (
      <div data-testid="calendar-loading" className="calendar-loading">
        {Array.from({ length: 3 }, (_, i) => (
          <div key={i} className="calendar-loading__row" aria-hidden="true" />
        ))}
        <span className="calendar-loading__sr">일정을 불러오는 중입니다…</span>
      </div>
    );
  }

  if (error) {
    return (
      <div data-testid="calendar-error" role="alert" className="calendar-error">
        <strong className="calendar-error__title">일정을 불러올 수 없습니다</strong>
        <span className="calendar-error__detail">{error}</span>
      </div>
    );
  }

  const monthHasAny = days.some((iso) => (filteredByDate.get(iso)?.total ?? 0) > 0);

  return (
    <div
      data-testid="calendar-timeline"
      className="calendar-timeline"
      role="region"
      aria-label="이번 달 일정"
    >
      {!monthHasAny && (
        <div data-testid="calendar-timeline-empty" className="calendar-timeline__empty">
          {monthEmptyLabel()}
        </div>
      )}
      {days.map((iso) => {
        const day = filteredByDate.get(iso) ?? { events: [], clusters: [], total: 0 };
        return (
          <DaySection
            key={iso}
            ref={(node) => {
              if (node) refs.current.set(iso, node);
              else refs.current.delete(iso);
            }}
            dateIso={iso}
            todayIso={todayIso}
            events={day.events}
            clusters={day.clusters}
            selected={iso === selectedDate}
          />
        );
      })}
    </div>
  );
}
