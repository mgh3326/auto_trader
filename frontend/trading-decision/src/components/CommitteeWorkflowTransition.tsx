import React from "react";
import type { CommitteeAccountMode, WorkflowStatus } from "../api/types";

interface Props {
  currentStatus: WorkflowStatus | null;
  accountMode?: CommitteeAccountMode | null;
  isUpdating: boolean;
  onTransition: (next: WorkflowStatus) => void;
}

const SIMULATION_MODES: ReadonlySet<CommitteeAccountMode> = new Set([
  "kis_mock",
  "alpaca_paper",
]);

// ROB-107: For KIS mock / Alpaca paper sessions, the Portfolio Manager step
// is auto-approved by simulation policy, so the workflow advances through
// `auto_approved` before reaching `preview_ready`. For non-simulation modes,
// auto-approval is out of scope and the step is skipped.
function nextStatusFor(
  current: WorkflowStatus,
  accountMode: CommitteeAccountMode | null | undefined,
): WorkflowStatus | null {
  switch (current) {
    case "created":
      return "evidence_generating";
    case "evidence_generating":
      return "evidence_ready";
    case "evidence_ready":
      return "debate_ready";
    case "debate_ready":
      return "trader_draft_ready";
    case "trader_draft_ready":
      return "risk_review_ready";
    case "risk_review_ready":
      return accountMode && SIMULATION_MODES.has(accountMode)
        ? "auto_approved"
        : "preview_ready";
    case "auto_approved":
      return "preview_ready";
    case "preview_ready":
      return "journal_ready";
    case "journal_ready":
      return "completed";
    default:
      return null;
  }
}

export const CommitteeWorkflowTransition: React.FC<Props> = ({
  currentStatus,
  accountMode,
  isUpdating,
  onTransition,
}) => {
  if (!currentStatus) return null;

  const nextStatus = nextStatusFor(currentStatus, accountMode);

  if (!nextStatus) return null;

  return (
    <div className="committee-workflow-transition">
      <button
        onClick={() => onTransition(nextStatus)}
        disabled={isUpdating}
        className="transition-button"
      >
        {isUpdating ? "Updating..." : `Advance to ${nextStatus.replace(/_/g, " ")}`}
      </button>
      <style>{`
        .committee-workflow-transition {
          padding: 16px;
          background: #f8f9fa;
          border-top: 1px solid #dee2e6;
          display: flex;
          justify-content: center;
        }
        .transition-button {
          padding: 8px 16px;
          background: #007bff;
          color: white;
          border: none;
          border-radius: 4px;
          cursor: pointer;
        }
        .transition-button:disabled {
          background: #6c757d;
          cursor: not-allowed;
        }
      `}</style>
    </div>
  );
};
