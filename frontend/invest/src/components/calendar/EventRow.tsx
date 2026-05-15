import type { CalendarEventVM } from "./vm";
import type { ImpactTag } from "../../types/calendar";
import { formatKstTime } from "./vm";
import { RegionBadge } from "./RegionBadge";
import { OwnershipTag } from "./OwnershipTag";

const IMPACT_TAG_LABEL: Record<ImpactTag, string> = {
  fx: "FX",
  rates: "금리",
  inflation: "물가",
  jobs: "고용",
  central_bank: "중앙은행",
};

function ImpactTags({ tags }: { tags: ImpactTag[] | undefined }) {
  if (!tags || tags.length === 0) return null;
  return (
    <div className="calendar-event-row__impact-tags" data-testid="calendar-event-impact-tags">
      {tags.map((tag) => (
        <span key={tag} className="calendar-event-row__impact-tag" data-impact-tag={tag}>
          {IMPACT_TAG_LABEL[tag] ?? tag}
        </span>
      ))}
    </div>
  );
}

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
        <ImpactTags tags={ev.impactTags} />
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
