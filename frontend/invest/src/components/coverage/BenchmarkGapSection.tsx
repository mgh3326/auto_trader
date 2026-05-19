import { Card } from "../../ds";
import type {
  BenchmarkGapMatrixResponse,
  BenchmarkGapPriority,
  BenchmarkGapRow,
  CoverageProductStatus,
} from "../../types/benchmarkGap";

const PRIORITY_COLOR: Record<BenchmarkGapPriority, string> = {
  P0: "#dc2626",
  P1: "#d97706",
  P2: "#ca8a04",
  P3: "#64748b",
};

const STATUS_LABEL: Record<CoverageProductStatus, string> = {
  covered: "수급됨",
  partial: "부분",
  stale: "오래됨",
  missing: "없음",
  candidate_unwired: "후보 · 미연결",
  benchmark_only: "벤치마크만",
  intentionally_excluded: "의도적 제외",
  unsupported: "미지원",
  blocked_by_auth_or_policy: "차단 (auth/정책)",
};

const STATUS_COLOR: Record<CoverageProductStatus, string> = {
  covered: "#16a34a",
  partial: "#ca8a04",
  stale: "#d97706",
  missing: "#dc2626",
  candidate_unwired: "#7c3aed",
  benchmark_only: "#64748b",
  intentionally_excluded: "#475569",
  unsupported: "#94a3b8",
  blocked_by_auth_or_policy: "#b91c1c",
};

function StatusPill({ status }: { status: CoverageProductStatus }) {
  return (
    <span
      style={{
        display: "inline-flex",
        alignItems: "center",
        borderRadius: 999,
        padding: "3px 8px",
        fontSize: 12,
        fontWeight: 800,
        color: "white",
        background: STATUS_COLOR[status],
        whiteSpace: "nowrap",
      }}
    >
      {STATUS_LABEL[status]}
    </span>
  );
}

function PriorityChip({ priority }: { priority: BenchmarkGapPriority }) {
  return (
    <span
      style={{
        display: "inline-flex",
        alignItems: "center",
        borderRadius: 6,
        padding: "1px 6px",
        fontSize: 11,
        fontWeight: 900,
        color: "white",
        background: PRIORITY_COLOR[priority],
      }}
    >
      {priority}
    </span>
  );
}

function RowCard({ row }: { row: BenchmarkGapRow }) {
  return (
    <div
      style={{
        border: "1px solid var(--divider)",
        borderRadius: 10,
        padding: 12,
        display: "grid",
        gap: 6,
      }}
    >
      <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
        <PriorityChip priority={row.priority} />
        <strong style={{ fontSize: 14 }}>{row.benchmarkLabelKo}</strong>
        <StatusPill status={row.coverageStatus} />
      </div>
      <div style={{ color: "var(--fg-3)", fontSize: 12, fontFamily: "var(--font-mono)" }}>
        {row.benchmarkProvider} · {row.benchmarkSurface}
      </div>
      <div style={{ fontSize: 12 }}>
        <span style={{ color: "var(--fg-2)" }}>왜 필요:</span> {row.whyNeeded}
      </div>
      <div style={{ fontSize: 12 }}>
        <span style={{ color: "var(--fg-2)" }}>다음 액션:</span> {row.nextAction}
      </div>
      {(row.autoTraderApi || row.autoTraderReadModel || row.autoTraderTable) && (
        <div style={{ color: "var(--fg-3)", fontSize: 11, fontFamily: "var(--font-mono)" }}>
          auto_trader: {row.autoTraderApi ?? row.autoTraderReadModel ?? row.autoTraderTable}
        </div>
      )}
      {row.relatedLinearIssue && (
        <div style={{ color: "var(--fg-3)", fontSize: 11 }}>관련 이슈: {row.relatedLinearIssue}</div>
      )}
      {row.newIssueCandidate && (
        <div style={{ color: "#7c3aed", fontSize: 11, fontWeight: 700 }}>new_issue_candidate</div>
      )}
    </div>
  );
}

function rowsByProvider(
  rows: BenchmarkGapRow[],
  provider: BenchmarkGapRow["benchmarkProvider"] | BenchmarkGapRow["benchmarkProvider"][],
) {
  const providers = Array.isArray(provider) ? provider : [provider];
  return rows.filter((row) => providers.includes(row.benchmarkProvider));
}

export function BenchmarkGapSection({ data }: { data: BenchmarkGapMatrixResponse }) {
  const tossRows = rowsByProvider(data.rows, "toss");
  const naverRows = rowsByProvider(data.rows, "naver");
  const internalRows = rowsByProvider(data.rows, ["internal", "kis", "upbit", "news_ingestor"]);

  return (
    <div style={{ display: "grid", gap: 16 }}>
      <Card>
        <h2 style={{ margin: 0, fontSize: 20 }}>토스·네이버 대비 데이터 수급 현황</h2>
        <p style={{ margin: "6px 0 12px", color: "var(--fg-2)", fontSize: 13 }}>
          이 화면은 "무슨 데이터를 다음에 수급해야 하는가?" 를 보여주는 read-only 벤치마크 갭 매트릭스입니다.
          Toss/Naver 는 reference/candidate 신호이며 sourceOfTruth 가 아닙니다.
        </p>
        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(120px, 1fr))", gap: 8 }}>
          {Object.entries(data.summary.byStatus).map(([status, count]) => (
            <div
              key={status}
              style={{
                border: "1px solid var(--divider)",
                borderRadius: 8,
                padding: "6px 10px",
                display: "flex",
                justifyContent: "space-between",
                alignItems: "center",
                gap: 8,
              }}
            >
              <StatusPill status={status as CoverageProductStatus} />
              <strong>{count}</strong>
            </div>
          ))}
        </div>
      </Card>

      <Card>
        <h2 style={{ margin: 0, fontSize: 18 }}>다음 수급 후보</h2>
        <p style={{ margin: "4px 0 12px", color: "var(--fg-2)", fontSize: 12 }}>
          우선순위 P0 → P3 순. 이미 `covered` 인 surface 는 제외됩니다.
        </p>
        <div style={{ display: "grid", gap: 8 }}>
          {data.nextCandidates.length === 0 && (
            <span style={{ color: "var(--fg-3)" }}>현재 추가 수급 후보 없음</span>
          )}
          {data.nextCandidates.map((c) => (
            <div
              key={c.rowId}
              style={{
                display: "grid",
                gridTemplateColumns: "auto 1fr auto",
                gap: 10,
                alignItems: "center",
                padding: "8px 10px",
                border: "1px solid var(--divider)",
                borderRadius: 8,
              }}
            >
              <PriorityChip priority={c.priority} />
              <div>
                <div style={{ fontWeight: 800 }}>
                  {c.featureArea} · {c.benchmarkProvider}
                </div>
                <div style={{ fontSize: 12, color: "var(--fg-2)" }}>{c.gap}</div>
                <div style={{ fontSize: 11, color: "var(--fg-3)" }}>
                  현재 auto_trader: {c.currentAutoTrader ?? "(없음)"} · 다음 액션: {c.nextAction}
                  {c.relatedLinearIssue ? ` · 관련 ${c.relatedLinearIssue}` : ""}
                  {c.newIssueCandidate ? " · new_issue_candidate" : ""}
                </div>
              </div>
              <StatusPill status={c.currentStatus} />
            </div>
          ))}
        </div>
      </Card>

      <Card>
        <h2 style={{ margin: 0, fontSize: 18 }}>Toss benchmark</h2>
        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(260px, 1fr))", gap: 10, marginTop: 10 }}>
          {tossRows.map((row) => (
            <RowCard key={row.id} row={row} />
          ))}
        </div>
      </Card>

      <Card>
        <h2 style={{ margin: 0, fontSize: 18 }}>Naver benchmark</h2>
        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(260px, 1fr))", gap: 10, marginTop: 10 }}>
          {naverRows.map((row) => (
            <RowCard key={row.id} row={row} />
          ))}
        </div>
      </Card>

      <Card>
        <h2 style={{ margin: 0, fontSize: 18 }}>auto_trader 내부 / KIS</h2>
        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(260px, 1fr))", gap: 10, marginTop: 10 }}>
          {internalRows.map((row) => (
            <RowCard key={row.id} row={row} />
          ))}
        </div>
      </Card>

      <Card>
        <h2 style={{ margin: 0, fontSize: 14 }}>Source authority</h2>
        <ul style={{ margin: "6px 0 0", paddingLeft: 18, color: "var(--fg-2)", fontSize: 12 }}>
          {data.sourcePolicy.map((line) => (
            <li key={line}>{line}</li>
          ))}
        </ul>
      </Card>
    </div>
  );
}
