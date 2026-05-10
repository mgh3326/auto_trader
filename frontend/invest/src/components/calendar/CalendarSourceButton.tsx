import { useId, useState } from "react";
import type { CalendarSourceStatus } from "../../types/calendar";
import { sourceFriendlyLabel, sourceStaleStatusCopy } from "./vm";

export function CalendarSourceButton({ sources }: { sources: CalendarSourceStatus[] }) {
  const [open, setOpen] = useState(false);
  const popoverId = useId();

  return (
    <div className="calendar-source-button-wrap">
      <button
        type="button"
        data-testid="calendar-source-button"
        className="calendar-source-button"
        aria-expanded={open ? "true" : "false"}
        aria-controls={popoverId}
        onClick={() => setOpen((v) => !v)}
      >
        데이터 출처
      </button>
      {open && (
        <div
          id={popoverId}
          data-testid="calendar-source-popover"
          role="dialog"
          aria-label="데이터 출처"
          className="calendar-source-popover"
        >
          {sources.map((s) => {
            const stale = sourceStaleStatusCopy(s);
            return (
              <div
                key={`${s.source}-${s.category}-${s.market}`}
                data-testid="calendar-source-row"
                data-source={s.source}
                data-state={s.state}
                className="calendar-source-row"
              >
                <span className="calendar-source-row__label">{sourceFriendlyLabel(s.source)}</span>
                {stale != null && (
                  <span className="calendar-source-row__status">{stale}</span>
                )}
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}
