import React from "react";
import type { CommitteeArtifacts } from "../api/types";

interface Props {
  artifacts: CommitteeArtifacts | null;
}

export const CommitteeEvidenceArtifacts: React.FC<Props> = ({ artifacts }) => {
  if (!artifacts?.evidence) return null;

  const { evidence } = artifacts;

  return (
    <div className="committee-evidence-artifacts">
      <h3>Committee Evidence</h3>
      {evidence.technical_analysis && (
        <div className="evidence-item">
          <h4>Technical Analysis</h4>
          <p>{evidence.technical_analysis.summary}</p>
          {evidence.technical_analysis.confidence && (
            <span className="confidence">Confidence: {evidence.technical_analysis.confidence}%</span>
          )}
        </div>
      )}
      {evidence.news_analysis && (
        <div className="evidence-item">
          <h4>News Analysis</h4>
          <p>{evidence.news_analysis.summary}</p>
          {evidence.news_analysis.confidence && (
            <span className="confidence">Confidence: {evidence.news_analysis.confidence}%</span>
          )}
        </div>
      )}
      <style>{`
        .committee-evidence-artifacts {
          padding: 16px;
          background: #fff;
          border: 1px solid #dee2e6;
          border-radius: 4px;
          margin-bottom: 16px;
        }
        .evidence-item {
          margin-bottom: 12px;
        }
        .evidence-item h4 {
          margin: 0 0 4px 0;
        }
        .evidence-item p {
          margin: 0 0 4px 0;
          color: #495057;
        }
        .confidence {
          font-size: 0.85em;
          color: #6c757d;
        }
      `}</style>
    </div>
  );
};
