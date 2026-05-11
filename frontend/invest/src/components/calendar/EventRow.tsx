import type { CalendarEventVM } from "./vm";
import { formatKstTime } from "./vm";
import { RegionBadge } from "./RegionBadge";
import { OwnershipTag } from "./OwnershipTag";

export function EventRow({ ev }: { ev: CalendarEventVM }) {
  const showFallbackTime =
    ev.time == null && ev.actual == null && ev.forecast == null && ev.previous == null;
  const timeText = ev.time != null ? formatKstTime(ev.time) : (ev.released ? "발표 완료" : showFallbackTime ? formatKstTime(null) : "발표 예정");

  return (
    <article
      className="calendar-event-row"
      data-testid="calendar-event"
      data-event-id={ev.id}
      data-event-type={ev.type}
      data-relation={ev.own ?? "none"}
    >
      <div className="calendar-event-row__day">{ev.monthDay}</div>
      <div className="calendar-event-row__main">
        <div className="calendar-event-row__title-line">
          <RegionBadge region={ev.region} />
          <span className="calendar-event-row__title" title={ev.title}>{ev.title}</span>
          <OwnershipTag own={ev.own} />
        </div>
        <div
          className="calendar-event-row__time"
          data-released={ev.released ? "true" : "false"}
        >
          {timeText}
        </div>
      </div>
      <div className="calendar-event-row__num calendar-event-row__num--actual" data-released={ev.released ? "true" : "false"}>
        {ev.actual ?? ""}
      </div>
      <div className="calendar-event-row__num calendar-event-row__num--forecast">
        {ev.forecast ?? ""}
      </div>
      <div className="calendar-event-row__num calendar-event-row__num--previous">
        {ev.previous ?? ""}
      </div>
    </article>
  );
}
