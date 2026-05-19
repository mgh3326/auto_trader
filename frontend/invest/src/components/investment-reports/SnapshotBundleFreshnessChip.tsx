// ROB-269 Phase 4 — Snapshot bundle freshness provenance chip.
//
// Renders bundle-level + per-source freshness state on an investment report
// header. Returns ``null`` when ``freshnessSummary`` is null/undefined.
//
// Default-off behaviour is achieved by data-presence gating, NOT by a
// runtime UI flag: this component does NOT read
// ``ACTION_REPORT_BUNDLE_UI_ENABLED`` (which is Phase 4 scaffolding only,
// not wired). Until callers supply ``snapshot_freshness_summary`` on the
// IngestReportRequest the field stays JSON ``null`` on the response and
// this component renders nothing. The Phase 3 generation flag
// ``ACTION_REPORT_BUNDLE_BASED_GENERATION_ENABLED`` is also default off,
// so reports stay snapshot-less in practice until both Phase 3 generators
// and Phase 4 UI rollout are intentionally enabled.
//
// Korean copy is intentionally constant — labels are owned by this file so
// translation/style changes are localised.

import type { JSX } from "react";

import type {
  SnapshotFreshnessStatus,
  SnapshotFreshnessSummary,
  SnapshotKindFreshness,
} from "../../types/investmentReports";

const STATUS_LABELS: Record<SnapshotFreshnessStatus, string> = {
  fresh: "신선",
  soft_stale: "일부 지연",
  partial: "부분",
  hard_stale: "오래됨",
  failed: "실패",
  unavailable: "확인 불가",
};

// Korean-facing labels for per-kind chips. Keys match the snapshot_kind
// enum on the backend (app/schemas/investment_snapshots.py SnapshotKind).
const KIND_LABELS: Record<string, string> = {
  portfolio: "포지션",
  journal: "거래일지",
  watch_context: "감시",
  market: "시장",
  symbol: "종목",
  candidate_universe: "후보군",
  news: "뉴스",
  naver_remote_debug: "네이버",
  toss_remote_debug: "토스",
  browser_probe: "브라우저",
  invest_page: "인베스트 페이지",
  llm_input_frozen: "LLM 입력",
};

// Visual rank: same status across overall + per-kind. Critical kinds get
// surfaced first; optional kinds collapse to a single "기타" cluster when
// they don't reach a degraded state.
const CRITICAL_KIND_ORDER: readonly string[] = [
  "portfolio",
  "journal",
  "watch_context",
  "market",
];

function isKindFreshness(value: unknown): value is SnapshotKindFreshness {
  return (
    typeof value === "object" &&
    value !== null &&
    "status" in value &&
    typeof (value as SnapshotKindFreshness).status === "string"
  );
}

function isStatusString(value: unknown): value is SnapshotFreshnessStatus {
  return (
    typeof value === "string" &&
    Object.prototype.hasOwnProperty.call(STATUS_LABELS, value)
  );
}

function statusOf(
  summary: SnapshotFreshnessSummary,
  kind: string,
): SnapshotFreshnessStatus | null {
  const entry = summary[kind];
  if (isKindFreshness(entry)) return entry.status;
  if (isStatusString(entry)) return entry;
  return null;
}

export interface SnapshotBundleFreshnessChipProps {
  freshnessSummary: SnapshotFreshnessSummary | null | undefined;
}

export function SnapshotBundleFreshnessChip({
  freshnessSummary,
}: SnapshotBundleFreshnessChipProps): JSX.Element | null {
  if (freshnessSummary == null) return null;

  const overall = freshnessSummary.overall ?? null;
  const overallStatus = isStatusString(overall) ? overall : null;
  if (overallStatus == null) {
    // Defensive: the DB CHECK should have stopped a published row from
    // reaching here without ``overall``, but for draft rows or partial
    // backfills render a neutral "확인 불가" so the operator sees the gap.
    return (
      <div
        className="snapshot-bundle-freshness snapshot-bundle-freshness--unavailable"
        data-testid="snapshot-bundle-freshness"
        aria-live="polite"
      >
        <span className="snapshot-bundle-freshness__overall">
          스냅샷 {STATUS_LABELS.unavailable}
        </span>
      </div>
    );
  }

  const overallLabel = STATUS_LABELS[overallStatus];

  // Surface critical kinds that are degraded; optional kinds with degraded
  // status appear after; fresh kinds are hidden to keep the chip compact.
  const criticalEntries: { kind: string; status: SnapshotFreshnessStatus }[] = [];
  for (const kind of CRITICAL_KIND_ORDER) {
    const s = statusOf(freshnessSummary, kind);
    if (s != null && s !== "fresh") criticalEntries.push({ kind, status: s });
  }

  const optionalEntries: { kind: string; status: SnapshotFreshnessStatus }[] = [];
  for (const [kind, entry] of Object.entries(freshnessSummary)) {
    if (kind === "overall") continue;
    if (CRITICAL_KIND_ORDER.includes(kind)) continue;
    let s: SnapshotFreshnessStatus | null = null;
    if (isKindFreshness(entry)) s = entry.status;
    else if (isStatusString(entry)) s = entry;
    if (s != null && s !== "fresh") optionalEntries.push({ kind, status: s });
  }

  return (
    <div
      className={`snapshot-bundle-freshness snapshot-bundle-freshness--${overallStatus}`}
      data-testid="snapshot-bundle-freshness"
      aria-live="polite"
    >
      <span className="snapshot-bundle-freshness__overall">
        스냅샷 {overallLabel}
      </span>
      {criticalEntries.length === 0 && optionalEntries.length === 0 ? null : (
        <ul className="snapshot-bundle-freshness__kinds" aria-label="스냅샷 소스별 상태">
          {criticalEntries.map(({ kind, status }) => (
            <li
              key={kind}
              className={`snapshot-bundle-freshness__chip snapshot-bundle-freshness__chip--critical snapshot-bundle-freshness__chip--${status}`}
              data-testid={`snapshot-chip-${kind}`}
            >
              {KIND_LABELS[kind] ?? kind} {STATUS_LABELS[status]}
            </li>
          ))}
          {optionalEntries.map(({ kind, status }) => (
            <li
              key={kind}
              className={`snapshot-bundle-freshness__chip snapshot-bundle-freshness__chip--optional snapshot-bundle-freshness__chip--${status}`}
              data-testid={`snapshot-chip-${kind}`}
            >
              {KIND_LABELS[kind] ?? kind} {STATUS_LABELS[status]}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
