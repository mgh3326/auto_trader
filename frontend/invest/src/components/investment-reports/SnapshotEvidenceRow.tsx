// ROB-275 — One row in the report's snapshot evidence list.
//
// Pure render — clicking the row notifies the parent (which decides
// whether to mount a drawer / fetch the payload). The row itself never
// triggers a network call.

import type { JSX } from "react";

import type {
  ReportSnapshotBundleItem,
  SnapshotFreshnessStatus,
} from "../../types/investmentReports";

const FRESHNESS_LABELS: Record<SnapshotFreshnessStatus, string> = {
  fresh: "신선",
  soft_stale: "일부 지연",
  partial: "부분",
  hard_stale: "오래됨",
  unavailable: "확인 불가",
  failed: "실패",
};

export interface SnapshotEvidenceRowProps {
  item: ReportSnapshotBundleItem;
  selected: boolean;
  onSelect: (snapshotUuid: string) => void;
}

export function SnapshotEvidenceRow({
  item,
  selected,
  onSelect,
}: SnapshotEvidenceRowProps): JSX.Element {
  const sizeLabel =
    item.payloadSizeBytes == null
      ? null
      : item.payloadSizeBytes < 1024
        ? `${item.payloadSizeBytes} B`
        : `${(item.payloadSizeBytes / 1024).toFixed(1)} KB`;

  return (
    <button
      type="button"
      data-testid={`snapshot-evidence-row-${item.snapshotUuid}`}
      onClick={() => onSelect(item.snapshotUuid)}
      aria-expanded={selected}
      style={{
        textAlign: "left",
        display: "grid",
        gap: 4,
        padding: selected ? "9px" : "10px",
        border: selected
          ? "2px solid var(--fg-2)"
          : "1px solid var(--border)",
        borderRadius: 10,
        background: selected ? "var(--surface-2)" : "transparent",
        color: "var(--fg-1)",
        cursor: "pointer",
        fontFamily: "inherit",
      }}
    >
      <div
        style={{
          display: "flex",
          justifyContent: "space-between",
          gap: 10,
          alignItems: "baseline",
          flexWrap: "wrap",
        }}
      >
        <span style={{ fontSize: 14, fontWeight: 700 }}>
          {item.snapshotKind}
          {item.symbol ? ` · ${item.symbol}` : ""}
        </span>
        <span style={{ fontSize: 12, color: "var(--fg-3)" }}>
          {FRESHNESS_LABELS[item.freshnessStatus] ?? item.freshnessStatus}
        </span>
      </div>
      <div
        style={{
          display: "flex",
          gap: 6,
          color: "var(--fg-3)",
          fontSize: 12,
          flexWrap: "wrap",
        }}
      >
        <span>{item.sourceKind}</span>
        <span>·</span>
        <span>{new Date(item.asOf).toLocaleString("ko-KR")}</span>
        {sizeLabel ? (
          <>
            <span>·</span>
            <span>{sizeLabel}</span>
          </>
        ) : null}
        {item.sourceUri ? (
          <>
            <span>·</span>
            <span style={{ wordBreak: "break-all" }}>{item.sourceUri}</span>
          </>
        ) : null}
      </div>
    </button>
  );
}
