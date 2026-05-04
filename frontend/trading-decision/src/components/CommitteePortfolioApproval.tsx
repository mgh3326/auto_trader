import React from "react";
import type { CommitteePortfolioApproval as PortfolioApprovalType } from "../api/types";

interface Props {
  portfolioApproval: PortfolioApprovalType | null;
}

export const CommitteePortfolioApproval: React.FC<Props> = ({ portfolioApproval }) => {
  if (!portfolioApproval) return null;

  const getStatusColor = (verdict: string) => {
    switch (verdict) {
      case "approved": return "#28a745";
      case "vetoed": return "#dc3545";
      case "modified": return "#007bff";
      default: return "#6c757d";
    }
  };

  return (
    <div className="committee-portfolio-approval">
      <h3>Portfolio Approval</h3>
      <div className="approval-status">
        Verdict: <strong style={{ color: getStatusColor(portfolioApproval.verdict) }}>
          {portfolioApproval.verdict.toUpperCase()}
        </strong>
      </div>
      {portfolioApproval.notes && (
        <div className="approval-notes">
          <p>{portfolioApproval.notes}</p>
        </div>
      )}
      {portfolioApproval.approved_at && (
        <div className="approved-at">
          Approved at: {new Date(portfolioApproval.approved_at).toLocaleString()}
        </div>
      )}
      <style>{`
        .committee-portfolio-approval {
          padding: 16px;
          background: #fdfefe;
          border: 1px solid #dee2e6;
          border-radius: 4px;
          margin-bottom: 16px;
        }
        .approval-status {
          margin-bottom: 8px;
          font-size: 1.1em;
        }
        .approval-notes {
          color: #495057;
          font-style: italic;
          margin-bottom: 8px;
        }
        .approved-at {
          font-size: 0.8em;
          color: #6c757d;
        }
      `}</style>
    </div>
  );
};
