import { Fragment } from "react";
import { EventRow } from "./EventRow";
import type { CalendarClusterVM } from "./vm";

export function ClusterEventRows({ clusters }: { clusters: CalendarClusterVM[] }) {
  return (
    <>
      {clusters.map((cluster) => (
        <Fragment key={cluster.id}>
          {cluster.topEvents.map((event) => (
            <EventRow key={event.id} ev={event} />
          ))}
          {cluster.count > cluster.topEvents.length ? <ClusterOverflow cluster={cluster} /> : null}
        </Fragment>
      ))}
    </>
  );
}

function ClusterOverflow({ cluster }: { cluster: CalendarClusterVM }) {
  const overflowCount = cluster.count - cluster.topEvents.length;
  const baseLabel = cluster.title.replace(/\s+\d+건$/, "");
  return (
    <div
      className="calendar-cluster-overflow"
      data-testid="calendar-cluster-overflow"
      data-cluster-id={cluster.id}
    >
      {baseLabel} · 그 외 {overflowCount}건
    </div>
  );
}
