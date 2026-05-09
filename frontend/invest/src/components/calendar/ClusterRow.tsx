import type { CalendarClusterVM } from "./vm";
import { RegionBadge } from "./RegionBadge";

export function ClusterRow({ cluster }: { cluster: CalendarClusterVM }) {
  const preview = cluster.topEvents.map((event) => event.title).join(" · ");

  return (
    <article
      data-testid="calendar-cluster"
      data-cluster-id={cluster.id}
      data-event-type={cluster.type}
      data-region={cluster.region}
      style={{
        display: "grid",
        gridTemplateColumns: "44px minmax(0, 1fr) 76px 76px 76px",
        alignItems: "center",
        gap: 10,
        padding: "12px",
        borderRadius: 10,
        background: "var(--surface-2)",
      }}
    >
      <div
        style={{
          fontSize: 12,
          fontWeight: 700,
          color: "var(--fg-1)",
          fontFeatureSettings: '"tnum"',
        }}
      >
        {cluster.monthDay}
      </div>
      <div style={{ minWidth: 0, gridColumn: "2 / 6" }}>
        <div style={{ display: "flex", alignItems: "center", gap: 8, minWidth: 0 }}>
          <RegionBadge region={cluster.region} />
          <span
            style={{
              fontSize: 14,
              fontWeight: 700,
              color: "var(--fg)",
              overflow: "hidden",
              textOverflow: "ellipsis",
              whiteSpace: "nowrap",
              minWidth: 0,
            }}
          >
            {cluster.title}
          </span>
        </div>
        <div
          style={{
            fontSize: 12,
            color: "var(--fg-3)",
            marginTop: 4,
            overflow: "hidden",
            textOverflow: "ellipsis",
            whiteSpace: "nowrap",
          }}
        >
          {preview ? `${preview}${cluster.count > cluster.topEvents.length ? " 외" : ""}` : "상세 일정 묶음"}
        </div>
      </div>
    </article>
  );
}
