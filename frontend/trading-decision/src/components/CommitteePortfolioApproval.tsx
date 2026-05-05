import React from "react";
import type { CommitteePortfolioApproval as PortfolioApprovalType } from "../api/types";
import { formatDateTime } from "../format/datetime";

interface Props {
  portfolioApproval: PortfolioApprovalType | null;
}

const VERDICT_LABEL: Record<string, string> = {
  approved: "승인됨",
  vetoed: "거부됨",
  modified: "수정 승인",
  pending: "대기 중",
};

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
      <h3>포트폴리오 승인</h3>
      <div className="approval-status">
        결정: <strong style={{ color: getStatusColor(portfolioApproval.verdict) }}>
          {VERDICT_LABEL[portfolioApproval.verdict] ?? portfolioApproval.verdict.toUpperCase()}
        </strong>
      </div>
      {portfolioApproval.notes && (
        <div className="approval-notes">
          <p>{portfolioApproval.notes}</p>
        </div>
      )}
      {portfolioApproval.approved_at && (
        <div className="approved-at">
          승인 일시: {formatDateTime(portfolioApproval.approved_at)}
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
