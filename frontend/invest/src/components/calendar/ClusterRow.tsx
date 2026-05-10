import type { CalendarClusterVM } from "./vm";
import { RegionBadge } from "./RegionBadge";

export function ClusterRow({ cluster }: { cluster: CalendarClusterVM }) {
  const previewText =
    cluster.topEvents.length > 0
      ? `${cluster.topEvents.map((e) => e.title).join(" · ")}${cluster.count > cluster.topEvents.length ? " 외" : ""}`
      : "상세 일정 묶음";

  return (
    <article
      className="calendar-cluster-row"
      data-testid="calendar-cluster"
      data-cluster-id={cluster.id}
      data-event-type={cluster.type}
      data-region={cluster.region}
    >
      <div className="calendar-cluster-row__day">{cluster.monthDay}</div>
      <div className="calendar-cluster-row__main">
        <div className="calendar-cluster-row__title-line">
          <RegionBadge region={cluster.region} />
          <span className="calendar-cluster-row__title" title={cluster.title}>
            {cluster.title}
          </span>
        </div>
        <div className="calendar-cluster-row__preview" title={previewText}>
          {previewText}
        </div>
      </div>
    </article>
  );
}
