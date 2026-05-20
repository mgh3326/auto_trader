// ROB-275 — Report snapshot evidence panel.
//
// Mounted under the existing report header on /invest/reports/:reportUuid.
// Renders the bundle summary + role-grouped item rows + separate
// `unavailable_sources` and `source_conflicts` sections (which are
// *report* observations, not bundle-linked snapshots — visually distinct).
//
// Click a row → drawer fetches that snapshot's payload via
// useSnapshotPayload. Initial render does NOT trigger payload fetches.

import { useEffect, useState, type JSX } from "react";

import { Card } from "../../ds";
import { useReportSnapshotBundle } from "../../hooks/useReportSnapshotBundle";
import { useSnapshotPayload } from "../../hooks/useSnapshotPayload";
import type {
  BundleItemRole,
  ReportSnapshotBundleItem,
} from "../../types/investmentReports";
import { SnapshotEvidenceRow } from "./SnapshotEvidenceRow";
import { SnapshotPayloadDrawer } from "./SnapshotPayloadDrawer";
import { ROLE_LABELS } from "./snapshotEvidenceLabels";

const ROLE_ORDER: readonly BundleItemRole[] = [
  "required",
  "optional",
  "fallback",
  "conflict_evidence",
];

function groupByRole(items: ReportSnapshotBundleItem[]) {
  const buckets: Record<BundleItemRole, ReportSnapshotBundleItem[]> = {
    required: [],
    optional: [],
    fallback: [],
    conflict_evidence: [],
  };
  for (const item of items) {
    buckets[item.role].push(item);
  }
  return buckets;
}

export interface ReportSnapshotEvidencePanelProps {
  reportUuid: string;
}

export function ReportSnapshotEvidencePanel({
  reportUuid,
}: ReportSnapshotEvidencePanelProps): JSX.Element | null {
  const { status, bundle, error, reload } = useReportSnapshotBundle(reportUuid);
  const [selectedSnapshotUuid, setSelectedSnapshotUuid] =
    useState<string | null>(null);

  // Reset selection whenever the report changes so a stale snapshot_uuid
  // from a previous report cannot leak into useSnapshotPayload.
  useEffect(() => {
    setSelectedSnapshotUuid(null);
  }, [reportUuid]);

  const payload = useSnapshotPayload(reportUuid, selectedSnapshotUuid);

  if (status === "loading") {
    return (
      <Card>
        <div
          data-testid="snapshot-evidence-panel-loading"
          style={{ color: "var(--fg-3)", fontSize: 13 }}
        >
          스냅샷 근거를 불러오는 중…
        </div>
      </Card>
    );
  }
  if (status === "error" || !bundle) {
    return (
      <Card>
        <div
          data-testid="snapshot-evidence-panel-error"
          style={{
            display: "flex",
            gap: 10,
            alignItems: "center",
            justifyContent: "space-between",
          }}
        >
          <span style={{ color: "var(--danger)", fontSize: 13 }}>
            스냅샷 근거를 불러오지 못했습니다.{error ? ` (${error})` : ""}
          </span>
          <button
            type="button"
            onClick={reload}
            style={{
              padding: "4px 10px",
              borderRadius: 8,
              border: "1px solid var(--border)",
              background: "transparent",
              color: "var(--fg-2)",
              cursor: "pointer",
              fontFamily: "inherit",
            }}
          >
            재시도
          </button>
        </div>
      </Card>
    );
  }

  if (bundle.legacyNoSnapshot) {
    return (
      <Card>
        <div
          data-testid="snapshot-evidence-panel-legacy"
          style={{ color: "var(--fg-3)", fontSize: 13 }}
        >
          이 리포트는 스냅샷 번들이 연결되어 있지 않습니다 (legacy).
        </div>
      </Card>
    );
  }

  const buckets = groupByRole(bundle.items);
  const summary = bundle.bundle;

  return (
    <Card>
      <div
        data-testid="snapshot-evidence-panel"
        style={{ display: "grid", gap: 12 }}
      >
        <div
          style={{
            display: "flex",
            justifyContent: "space-between",
            alignItems: "baseline",
            gap: 10,
            flexWrap: "wrap",
          }}
        >
          <h2 style={{ margin: 0, fontSize: 18 }}>스냅샷 근거</h2>
          {summary ? (
            <span style={{ color: "var(--fg-3)", fontSize: 12 }}>
              번들 {summary.status} · {summary.market}
              {summary.accountScope ? ` · ${summary.accountScope}` : ""} · policy{" "}
              {summary.policyVersion} ·{" "}
              {new Date(summary.asOf).toLocaleString("ko-KR")}
            </span>
          ) : null}
        </div>

        {summary ? (
          <div
            style={{
              fontSize: 12,
              color: "var(--fg-3)",
              wordBreak: "break-all",
            }}
          >
            번들 ID: {summary.bundleUuid}
          </div>
        ) : null}

        {ROLE_ORDER.map((role) =>
          buckets[role].length > 0 ? (
            <section
              key={role}
              data-testid={`snapshot-evidence-role-${role}`}
              style={{ display: "grid", gap: 8 }}
            >
              <h3 style={{ margin: 0, fontSize: 14 }}>
                {ROLE_LABELS[role]} ({buckets[role].length})
              </h3>
              {buckets[role].map((item) => (
                <div key={item.snapshotUuid} style={{ display: "grid" }}>
                  <SnapshotEvidenceRow
                    item={item}
                    selected={selectedSnapshotUuid === item.snapshotUuid}
                    onSelect={(snapshotUuid) =>
                      setSelectedSnapshotUuid((prev) =>
                        prev === snapshotUuid ? null : snapshotUuid,
                      )
                    }
                  />
                  {selectedSnapshotUuid === item.snapshotUuid ? (
                    <SnapshotPayloadDrawer
                      status={payload.status}
                      detail={payload.detail}
                      error={payload.error}
                      onClose={() => setSelectedSnapshotUuid(null)}
                    />
                  ) : null}
                </div>
              ))}
            </section>
          ) : null,
        )}

        {/* unavailable_sources and source_conflicts are *report*
            observations — NOT bundle-linked snapshots. Render them in
            distinct sections so they are not mistaken for evidence rows. */}
        {bundle.unavailableSources &&
        Object.keys(bundle.unavailableSources).length > 0 ? (
          <section
            data-testid="snapshot-evidence-unavailable-sources"
            style={{
              display: "grid",
              gap: 6,
              padding: 10,
              borderRadius: 10,
              border: "1px solid var(--warn, var(--border))",
              background: "var(--surface-2)",
            }}
          >
            <h3 style={{ margin: 0, fontSize: 14, color: "var(--warn)" }}>
              확인 불가 소스
            </h3>
            <pre
              style={{
                margin: 0,
                fontFamily: "var(--mono, monospace)",
                fontSize: 12,
                color: "var(--fg-2)",
                whiteSpace: "pre-wrap",
                wordBreak: "break-word",
              }}
            >
              {JSON.stringify(bundle.unavailableSources, null, 2)}
            </pre>
          </section>
        ) : null}

        {bundle.sourceConflicts &&
        Object.keys(bundle.sourceConflicts).length > 0 ? (
          <section
            data-testid="snapshot-evidence-source-conflicts"
            style={{
              display: "grid",
              gap: 6,
              padding: 10,
              borderRadius: 10,
              border: "1px solid var(--danger, var(--border))",
              background: "var(--surface-2)",
            }}
          >
            <h3 style={{ margin: 0, fontSize: 14, color: "var(--danger)" }}>
              소스 충돌
            </h3>
            <pre
              style={{
                margin: 0,
                fontFamily: "var(--mono, monospace)",
                fontSize: 12,
                color: "var(--fg-2)",
                whiteSpace: "pre-wrap",
                wordBreak: "break-word",
              }}
            >
              {JSON.stringify(bundle.sourceConflicts, null, 2)}
            </pre>
          </section>
        ) : null}
      </div>
    </Card>
  );
}
