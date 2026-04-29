import { formatDateTime } from "../format/datetime";
import type { StrategyEventDetail } from "../api/types";
import styles from "./StrategyEventTimeline.module.css";

interface StrategyEventTimelineProps {
  events: StrategyEventDetail[];
}

export default function StrategyEventTimeline({
  events,
}: StrategyEventTimelineProps) {
  if (events.length === 0) {
    return (
      <p className={styles.empty}>
        No strategy events yet for this session.
      </p>
    );
  }
  return (
    <ol className={styles.timeline} aria-label="Strategy events">
      {events.map((event) => {
        const summary = event.normalized_summary ?? event.source_text;
        return (
          <li key={event.event_uuid} className={styles.event}>
            <div className={styles.eventHeader}>
              <span className={styles.type}>{event.event_type}</span>
              <span>severity {event.severity}</span>
              <span>confidence {event.confidence}</span>
              <span className={styles.meta}>
                {formatDateTime(event.created_at)}
              </span>
            </div>
            <p className={styles.summary}>{summary}</p>
            {event.affected_symbols.length > 0 ||
            event.affected_markets.length > 0 ||
            event.affected_themes.length > 0 ||
            event.affected_sectors.length > 0 ? (
              <div className={styles.tags}>
                {event.affected_symbols.map((s) => (
                  <span key={`sym-${s}`} className={styles.tag}>
                    {s}
                  </span>
                ))}
                {event.affected_markets.map((m) => (
                  <span key={`mkt-${m}`} className={styles.tag}>
                    {m}
                  </span>
                ))}
                {event.affected_sectors.map((s) => (
                  <span key={`sec-${s}`} className={styles.tag}>
                    {s}
                  </span>
                ))}
                {event.affected_themes.map((t) => (
                  <span key={`thm-${t}`} className={styles.tag}>
                    {t}
                  </span>
                ))}
              </div>
            ) : null}
          </li>
        );
      })}
    </ol>
  );
}
