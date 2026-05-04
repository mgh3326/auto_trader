import React from "react";
import type { WorkflowStatus } from "../api/types";

interface Props {
  currentStatus: WorkflowStatus | null;
  isUpdating: boolean;
  onTransition: (next: WorkflowStatus) => void;
}

const NEXT_STEPS: Record<string, WorkflowStatus> = {
  created: "evidence_generating",
  evidence_generating: "evidence_ready",
  evidence_ready: "debate_ready",
  debate_ready: "trader_draft_ready",
  trader_draft_ready: "risk_review_ready",
  risk_review_ready: "preview_ready",
  preview_ready: "journal_ready",
  journal_ready: "completed",
};

export const CommitteeWorkflowTransition: React.FC<Props> = ({
  currentStatus,
  isUpdating,
  onTransition,
}) => {
  if (!currentStatus) return null;

  const nextStatus = NEXT_STEPS[currentStatus];

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
