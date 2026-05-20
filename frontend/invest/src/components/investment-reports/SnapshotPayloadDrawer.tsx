// ROB-275 — Snapshot payload viewer rendered next to the evidence row.
//
// Receives the already-loaded ReportSnapshotDetail; rendering is pure.
// The fetch is handled by useSnapshotPayload in the parent panel so this
// component never knows about the network.

import type { JSX } from "react";

import type { ReportSnapshotDetail } from "../../types/investmentReports";

const ROLE_LABELS: Record<string, string> = {
  required: "필수",
  optional: "선택",
  fallback: "대체",
  conflict_evidence: "충돌 증거",
};

const FRESHNESS_LABELS: Record<string, string> = {
  fresh: "신선",
  soft_stale: "일부 지연",
  partial: "부분",
  hard_stale: "오래됨",
  unavailable: "확인 불가",
  failed: "실패",
};

export interface SnapshotPayloadDrawerProps {
  status: "idle" | "loading" | "ready" | "error";
  detail: ReportSnapshotDetail | null;
  error: string | null;
  onClose: () => void;
}

export function SnapshotPayloadDrawer({
  status,
  detail,
  error,
  onClose,
}: SnapshotPayloadDrawerProps): JSX.Element {
  return (
    <div
      data-testid="snapshot-payload-drawer"
      style={{
        marginTop: 8,
        padding: 12,
        border: "1px solid var(--border)",
        borderRadius: 10,
        background: "var(--surface-2)",
        display: "grid",
        gap: 10,
      }}
    >
      <div
        style={{
          display: "flex",
          justifyContent: "space-between",
          alignItems: "baseline",
          gap: 10,
        }}
      >
        <strong style={{ fontSize: 14 }}>스냅샷 페이로드</strong>
        <button
          type="button"
          onClick={onClose}
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
          닫기
        </button>
      </div>

      {status === "loading" ? (
        <div style={{ color: "var(--fg-3)", fontSize: 13 }}>
          페이로드 불러오는 중…
        </div>
      ) : null}

      {status === "error" ? (
        <div style={{ color: "var(--danger)", fontSize: 13 }}>
          페이로드를 불러오지 못했습니다.{error ? ` (${error})` : ""}
        </div>
      ) : null}

      {status === "ready" && detail != null ? (
        <>
          <dl
            style={{
              display: "grid",
              gridTemplateColumns: "minmax(80px, max-content) 1fr",
              gap: "4px 12px",
              margin: 0,
              fontSize: 12,
              color: "var(--fg-3)",
            }}
          >
            <dt>역할</dt>
            <dd style={{ margin: 0 }}>{ROLE_LABELS[detail.role] ?? detail.role}</dd>
            <dt>종류</dt>
            <dd style={{ margin: 0 }}>{detail.snapshotKind}</dd>
            <dt>소스</dt>
            <dd style={{ margin: 0 }}>{detail.sourceKind}</dd>
            <dt>신선도</dt>
            <dd style={{ margin: 0 }}>
              {FRESHNESS_LABELS[detail.freshnessStatus] ?? detail.freshnessStatus}
            </dd>
            <dt>as_of</dt>
            <dd style={{ margin: 0 }}>
              {new Date(detail.asOf).toLocaleString("ko-KR")}
            </dd>
            {detail.sourceUri ? (
              <>
                <dt>출처 URI</dt>
                <dd style={{ margin: 0, wordBreak: "break-all" }}>
                  {detail.sourceUri}
                </dd>
              </>
            ) : null}
          </dl>
          <pre
            data-testid="snapshot-payload-json"
            style={{
              margin: 0,
              maxHeight: 320,
              overflow: "auto",
              fontFamily: "var(--mono, monospace)",
              fontSize: 12,
              background: "var(--surface-1)",
              padding: 10,
              borderRadius: 8,
              border: "1px solid var(--border)",
              color: "var(--fg-2)",
              whiteSpace: "pre-wrap",
              wordBreak: "break-word",
            }}
          >
            {JSON.stringify(detail.payloadJson, null, 2)}
          </pre>
        </>
      ) : null}
    </div>
  );
}
