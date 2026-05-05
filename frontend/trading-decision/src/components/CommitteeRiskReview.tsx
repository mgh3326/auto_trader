import React from "react";
import type { CommitteeRiskReview as RiskReviewType } from "../api/types";

interface Props {
  riskReview: RiskReviewType | null;
}

export const CommitteeRiskReview: React.FC<Props> = ({ riskReview }) => {
  if (!riskReview) return null;

  const getStatusColor = (verdict: string) => {
    switch (verdict) {
      case "approved": return "#28a745";
      case "vetoed": return "#dc3545";
      case "flagged": return "#ffc107";
      default: return "#6c757d";
    }
  };

  return (
    <div className="committee-risk-review">
      <h3>Risk Review</h3>
      <div className="risk-status">
        Verdict: <strong style={{ color: getStatusColor(riskReview.verdict) }}>
          {riskReview.verdict.toUpperCase()}
        </strong>
      </div>
      {riskReview.notes && (
        <div className="risk-notes">
          <p>{riskReview.notes}</p>
        </div>
      )}
      {riskReview.reviewed_at && (
        <div className="reviewed-at">
          Reviewed at: {new Date(riskReview.reviewed_at).toLocaleString()}
        </div>
      )}
      <style>{`
        .committee-risk-review {
          padding: 16px;
          background: #fdfdfe;
          border: 1px solid #dee2e6;
          border-radius: 4px;
          margin-bottom: 16px;
        }
        .risk-status {
          margin-bottom: 8px;
          font-size: 1.1em;
        }
        .risk-notes {
          color: #495057;
          font-style: italic;
          margin-bottom: 8px;
        }
        .reviewed-at {
          font-size: 0.8em;
          color: #6c757d;
        }
      `}</style>
    </div>
  );
};
