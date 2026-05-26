// ROB-318 Phase 3 (PR-C) — deterministic report-diagnostics panel.
//
// Renders the report-level diagnostics persisted by PR-B
// (snapshot_report_diagnostics): a quality-grade badge, the why-no-action
// distinction (genuine hold vs data-insufficient vs stale-gated), and
// per-source data-sufficiency chips that surface the reason_code for each
// degraded source.
//
// Null-safe: returns ``null`` for legacy reports (no diagnostics) and when no
// sub-field carries anything worth showing. All narrative text is either a
// fixed Korean label or the backend-provided ``reason_ko`` — this component
// never infers a value.

import type { CSSProperties, JSX } from "react";

import type {
  DataSufficiencySource,
  SnapshotReportDiagnostics,
} from "../../types/investmentReports";
import { FRESHNESS_LABELS } from "./snapshotEvidenceLabels";
import {
  DIAGNOSTIC_KIND_LABELS,
  QUALITY_GRADE_LABELS,
  REASON_CODE_LABELS,
  WHY_NO_ACTION_LABELS,
} from "./reportDiagnosticsLabels";

// Grade → accent colour, reusing the global CSS variables the rest of the
// report header uses (var(--success/warn/danger/fg-3)).
const GRADE_COLORS: Record<string, string> = {
  high_confidence: "var(--success, #2e7d32)",
  informational_only: "var(--warn, #b8860b)",
  no_action: "var(--danger, #c0392b)",
};

const DEGRADED_STATUSES = new Set(["hard_stale", "unavailable", "failed"]);

function statusLabel(status: string | null | undefined): string {
  if (status && status in FRESHNESS_LABELS) {
    return FRESHNESS_LABELS[status as keyof typeof FRESHNESS_LABELS];
  }
  return status ?? "확인 불가";
}

export interface ReportDiagnosticsPanelProps {
  diagnostics: SnapshotReportDiagnostics | null | undefined;
}

export function ReportDiagnosticsPanel({
  diagnostics,
}: ReportDiagnosticsPanelProps): JSX.Element | null {
  if (diagnostics == null) return null;

  const quality = diagnostics.report_quality_summary ?? null;
  const why = diagnostics.why_no_action ?? null;
  const sufficiency = diagnostics.data_sufficiency_by_source ?? {};

  // Only surface degraded sources — fresh ones are noise on the chip row.
  const degraded: [string, DataSufficiencySource][] = Object.entries(
    sufficiency,
  ).filter(([, info]) => info?.status != null && DEGRADED_STATUSES.has(info.status));

  const hasQuality = quality?.grade != null;
  const hasWhy = why?.kind != null;
  if (!hasQuality && !hasWhy && degraded.length === 0) return null;

  const chipStyle: CSSProperties = {
    fontSize: 12,
    color: "var(--fg-3)",
    border: "1px solid var(--border, #ddd)",
    borderRadius: 6,
    padding: "2px 8px",
  };

  return (
    <div
      data-testid="report-diagnostics"
      style={{ display: "grid", gap: 8 }}
      aria-live="polite"
    >
      {hasQuality ? (
        <span
          data-testid="report-diagnostics-grade"
          style={{
            fontSize: 12,
            fontWeight: 800,
            color: GRADE_COLORS[quality!.grade] ?? "var(--fg-3)",
          }}
        >
          리포트 품질: {QUALITY_GRADE_LABELS[quality!.grade] ?? quality!.grade}
          {typeof quality!.fresh_coverage_pct === "number"
            ? ` · 신선도 ${quality!.fresh_coverage_pct}%`
            : null}
        </span>
      ) : null}

      {hasWhy ? (
        <div
          data-testid="report-diagnostics-why"
          data-kind={why!.kind}
          style={{ fontSize: 13, color: "var(--fg-2)", lineHeight: 1.6 }}
        >
          <strong style={{ marginRight: 6 }}>
            {WHY_NO_ACTION_LABELS[why!.kind] ?? why!.kind}
          </strong>
          {why!.reason_ko ? <span>{why!.reason_ko}</span> : null}
        </div>
      ) : null}

      {degraded.length > 0 ? (
        <ul
          aria-label="소스별 데이터 충분성"
          style={{
            listStyle: "none",
            margin: 0,
            padding: 0,
            display: "flex",
            gap: 6,
            flexWrap: "wrap",
          }}
        >
          {degraded.map(([kind, info]) => (
            <li
              key={kind}
              data-testid={`report-diagnostics-source-${kind}`}
              style={chipStyle}
            >
              {DIAGNOSTIC_KIND_LABELS[kind] ?? kind} · {statusLabel(info.status)}
              {info.reason_code
                ? ` (${REASON_CODE_LABELS[info.reason_code] ?? info.reason_code})`
                : ""}
            </li>
          ))}
        </ul>
      ) : null}
    </div>
  );
}
