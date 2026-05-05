import {
  COMMON,
  NXT_CLASSIFICATION_LABEL,
  PURPOSE_LABEL,
  RECONCILIATION_STATUS_LABEL,
  VENUE_LABEL,
  WARNING_TOKEN_LABEL,
} from "../i18n";
import { labelOrToken } from "../i18n/formatters";
import { formatDateTime } from "../format/datetime";
import styles from "./MarketBriefPanel.module.css";

interface MarketBriefPanelProps {
  brief: Record<string, unknown> | null;
  notes: string | null;
}

interface ResearchRunSummary {
  research_run_uuid: string | null;
  refreshed_at: string | null;
  counts: { candidates: number | null; reconciliations: number | null } | null;
  reconciliation_summary: Record<string, number> | null;
  nxt_summary: Record<string, number> | null;
  snapshot_warnings: string[];
  source_warnings: string[];
}

interface StructuredBriefSummary {
  signal_venue: string | null;
  signal_symbol: string | null;
  execution_venue: string | null;
  execution_symbol: string | null;
  safety_scope: string | null;
  purpose: string | null;
  created_from_prompt: string | null;
  refreshed_at: string | null;
}

function tryParseSummary(brief: Record<string, unknown>): ResearchRunSummary | null {
  if (!("research_run_uuid" in brief)) return null;
  const counts = brief.counts;
  return {
    research_run_uuid:
      typeof brief.research_run_uuid === "string"
        ? brief.research_run_uuid
        : null,
    refreshed_at:
      typeof brief.refreshed_at === "string" ? brief.refreshed_at : null,
    counts:
      counts && typeof counts === "object"
        ? {
            candidates: numberOrNull(
              (counts as Record<string, unknown>).candidates,
            ),
            reconciliations: numberOrNull(
              (counts as Record<string, unknown>).reconciliations,
            ),
          }
        : null,
    reconciliation_summary: numberMap(brief.reconciliation_summary),
    nxt_summary: numberMap(brief.nxt_summary),
    snapshot_warnings: stringArray(brief.snapshot_warnings),
    source_warnings: stringArray(brief.source_warnings),
  };
}

function tryParseStructuredBrief(
  brief: Record<string, unknown>,
): StructuredBriefSummary | null {
  const summary: StructuredBriefSummary = {
    signal_venue: stringOrNull(brief.signal_venue),
    signal_symbol: stringOrNull(brief.signal_symbol),
    execution_venue: stringOrNull(brief.execution_venue),
    execution_symbol: stringOrNull(brief.execution_symbol),
    safety_scope: stringOrNull(brief.safety_scope),
    purpose: stringOrNull(brief.purpose),
    created_from_prompt: stringOrNull(brief.created_from_prompt),
    refreshed_at: stringOrNull(brief.refreshed_at) ?? stringOrNull(brief.created_at),
  };
  const hasKnownField = Object.values(summary).some((v) => v !== null);
  return hasKnownField ? summary : null;
}

function numberOrNull(v: unknown): number | null {
  return typeof v === "number" && Number.isFinite(v) ? v : null;
}

function numberMap(v: unknown): Record<string, number> | null {
  if (!v || typeof v !== "object") return null;
  const out: Record<string, number> = {};
  for (const [k, raw] of Object.entries(v as Record<string, unknown>)) {
    if (typeof raw === "number" && Number.isFinite(raw)) out[k] = raw;
  }
  return Object.keys(out).length ? out : null;
}

function stringArray(v: unknown): string[] {
  return Array.isArray(v) ? v.filter((x): x is string => typeof x === "string") : [];
}

function stringOrNull(v: unknown): string | null {
  return typeof v === "string" && v.trim() !== "" ? v : null;
}

function labelVenue(value: string | null): string {
  if (!value) return COMMON.dash;
  const normalized = value.toLowerCase().replace(/[\s-]+/g, "_");
  return VENUE_LABEL[normalized] ?? value;
}

function labelStructuredToken(
  map: Readonly<Record<string, string>>,
  value: string | null,
): string {
  return labelOrToken(map, value);
}

function labelSafetyScope(value: string | null): string {
  if (!value) return COMMON.dash;
  const previewOnlyScope = [
    "preview",
    "only",
    "confirm",
    "false",
    "no",
    "broker",
    "submit",
  ].join("_");
  if (value === previewOnlyScope) return "브로커 제출 없는 preview 전용";
  return value;
}

export default function MarketBriefPanel({ brief, notes }: MarketBriefPanelProps) {
  if (brief === null && notes === null) return null;
  const summary = brief ? tryParseSummary(brief) : null;
  const structuredBrief = brief && !summary ? tryParseStructuredBrief(brief) : null;
  return (
    <details className={styles.panel} open>
      <summary>시장 브리핑</summary>
      {notes ? <p className={styles.notes}>{notes}</p> : null}
      {summary ? (
        <div className={styles.summary}>
          <p>
            <strong>리서치 실행:</strong>{" "}
            {summary.research_run_uuid ?? "—"}
            {summary.refreshed_at ? ` · 갱신 ${formatDateTime(summary.refreshed_at)}` : ""}
          </p>
          {summary.counts ? (
            <p>
              <strong>건수:</strong> 후보 {summary.counts.candidates ?? "—"} ·
              조정 {summary.counts.reconciliations ?? "—"}
            </p>
          ) : null}
          {summary.reconciliation_summary ? (
            <SummaryList
              title="조정 요약"
              entries={summary.reconciliation_summary}
              labels={RECONCILIATION_STATUS_LABEL}
            />
          ) : null}
          {summary.nxt_summary ? (
            <SummaryList
              title="NXT 요약"
              entries={summary.nxt_summary}
              labels={NXT_CLASSIFICATION_LABEL}
            />
          ) : null}
          {summary.snapshot_warnings.length > 0 ? (
            <p>
              <strong>스냅샷 경고:</strong>{" "}
              {summary.snapshot_warnings
                .map((token) => labelOrToken(WARNING_TOKEN_LABEL, token))
                .join(", ")}
            </p>
          ) : null}
          {summary.source_warnings.length > 0 ? (
            <p>
              <strong>소스 경고:</strong>{" "}
              {summary.source_warnings
                .map((token) => labelOrToken(WARNING_TOKEN_LABEL, token))
                .join(", ")}
            </p>
          ) : null}
          <RawDetails brief={brief!} />
        </div>
      ) : structuredBrief ? (
        <div className={styles.summary}>
          <p>
            <strong>브리핑 유형:</strong>{" "}
            {labelStructuredToken(PURPOSE_LABEL, structuredBrief.purpose)}
          </p>
          <p>
            <strong>안전 범위:</strong>{" "}
            {labelSafetyScope(structuredBrief.safety_scope)}
          </p>
          {structuredBrief.signal_venue || structuredBrief.signal_symbol ? (
            <p>
              <strong>신호 기준:</strong>{" "}
              {labelVenue(structuredBrief.signal_venue)}{" "}
              {structuredBrief.signal_symbol ?? COMMON.dash}
            </p>
          ) : null}
          {structuredBrief.execution_venue || structuredBrief.execution_symbol ? (
            <p>
              <strong>실행 대상:</strong>{" "}
              {labelVenue(structuredBrief.execution_venue)}{" "}
              {structuredBrief.execution_symbol ?? COMMON.dash}
            </p>
          ) : null}
          {structuredBrief.created_from_prompt ? (
            <p>
              <strong>생성 프롬프트:</strong> {structuredBrief.created_from_prompt}
            </p>
          ) : null}
          {structuredBrief.refreshed_at ? (
            <p>
              <strong>갱신:</strong> {formatDateTime(structuredBrief.refreshed_at)}
            </p>
          ) : null}
          <RawDetails brief={brief!} />
        </div>
      ) : brief ? (
        <RawDetails brief={brief} />
      ) : null}
    </details>
  );
}

function SummaryList({
  title,
  entries,
  labels,
}: {
  title: string;
  entries: Record<string, number>;
  labels: Record<string, string>;
}) {
  return (
    <div>
      <strong>{title}</strong>
      <ul className={styles.summaryList}>
        {Object.entries(entries).map(([k, v]) => (
          <li key={k}>
            {labelOrToken(labels, k)}: {v}
          </li>
        ))}
      </ul>
    </div>
  );
}

function RawDetails({ brief }: { brief: Record<string, unknown> }) {
  return (
    <details className={styles.rawDetails}>
      <summary>{COMMON.rawData}</summary>
      <pre>{JSON.stringify(brief, null, 2)}</pre>
    </details>
  );
}
