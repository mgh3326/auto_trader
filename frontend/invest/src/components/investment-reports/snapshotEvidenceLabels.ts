// ROB-275 — Shared Korean labels for snapshot evidence components.
//
// Centralised so a new BundleItemRole or SnapshotFreshnessStatus value
// only needs to be reflected in one file.

import type {
  BundleItemRole,
  SnapshotFreshnessStatus,
} from "../../types/investmentReports";

export const ROLE_LABELS: Record<BundleItemRole, string> = {
  required: "필수",
  optional: "선택",
  fallback: "대체",
  conflict_evidence: "충돌 증거",
};

export const FRESHNESS_LABELS: Record<SnapshotFreshnessStatus, string> = {
  fresh: "신선",
  soft_stale: "일부 지연",
  partial: "부분",
  hard_stale: "오래됨",
  unavailable: "확인 불가",
  failed: "실패",
};
