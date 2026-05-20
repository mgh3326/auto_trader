// ROB-274 — Proposal diff panel for non-`create` operations.
//
// Renders the operation badge, target reference, current/proposed state JSON
// blobs, and the per-field diff table. Returns null for `create` operations
// (no prior state to compare against).

import type {
  ProposalDiffEntry,
  ProposalTargetRef,
} from "../../types/investmentReports";

interface ProposalDiffPanelProps {
  operation: string;
  targetRef?: ProposalTargetRef | null;
  currentState?: Record<string, unknown> | null;
  proposedState?: Record<string, unknown> | null;
  diff?: ProposalDiffEntry[] | null;
}

function renderValue(value: unknown): string {
  if (value === null || value === undefined) return "—";
  if (typeof value === "object") return JSON.stringify(value);
  return String(value);
}

export function ProposalDiffPanel(props: ProposalDiffPanelProps) {
  const { operation, targetRef, currentState, proposedState, diff } = props;
  if (operation === "create") return null;
  return (
    <div
      className="proposal-diff-panel"
      data-operation={operation}
      style={{
        display: "grid",
        gap: 8,
        padding: 10,
        border: "1px solid var(--border)",
        borderRadius: 10,
        background: "var(--surface-2)",
        fontSize: 12,
      }}
    >
      <div
        className="proposal-diff-header"
        style={{
          display: "flex",
          gap: 8,
          alignItems: "baseline",
          flexWrap: "wrap",
        }}
      >
        <span
          className="proposal-op-badge"
          data-operation={operation}
          style={{
            fontWeight: 800,
            padding: "2px 8px",
            borderRadius: 8,
            background: "rgba(255,255,255,0.04)",
            color: "var(--fg-1)",
          }}
        >
          {operation}
        </span>
        {targetRef ? (
          <span
            className="proposal-target-ref"
            style={{ color: "var(--fg-3)" }}
          >
            {targetRef.type}
            {targetRef.id ? `:${targetRef.id}` : ""}
            {targetRef.status ? ` · ${targetRef.status}` : ""}
          </span>
        ) : null}
      </div>
      {currentState ? (
        <div className="proposal-current-state">
          <strong style={{ marginRight: 6 }}>current</strong>
          <pre
            style={{
              margin: 0,
              fontFamily: "var(--mono, monospace)",
              whiteSpace: "pre-wrap",
              wordBreak: "break-all",
            }}
          >
            {JSON.stringify(currentState, null, 2)}
          </pre>
        </div>
      ) : null}
      {proposedState ? (
        <div className="proposal-proposed-state">
          <strong style={{ marginRight: 6 }}>proposed</strong>
          <pre
            style={{
              margin: 0,
              fontFamily: "var(--mono, monospace)",
              whiteSpace: "pre-wrap",
              wordBreak: "break-all",
            }}
          >
            {JSON.stringify(proposedState, null, 2)}
          </pre>
        </div>
      ) : null}
      {diff && diff.length > 0 ? (
        <table
          className="proposal-diff-table"
          style={{
            width: "100%",
            borderCollapse: "collapse",
            fontFamily: "var(--mono, monospace)",
          }}
        >
          <thead>
            <tr>
              <th style={{ textAlign: "left", padding: "4px 6px" }}>field</th>
              <th style={{ textAlign: "left", padding: "4px 6px" }}>from</th>
              <th style={{ textAlign: "left", padding: "4px 6px" }}>to</th>
            </tr>
          </thead>
          <tbody>
            {diff.map((entry) => (
              <tr key={entry.field}>
                <td style={{ padding: "4px 6px" }}>{entry.field}</td>
                <td style={{ padding: "4px 6px" }}>{renderValue(entry.from)}</td>
                <td style={{ padding: "4px 6px" }}>{renderValue(entry.to)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : null}
    </div>
  );
}
