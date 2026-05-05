import React from "react";
import type { CommitteeResearchDebate as ResearchDebateType } from "../api/types";

interface Props {
  researchDebate: ResearchDebateType | null;
}

export const CommitteeResearchDebate: React.FC<Props> = ({ researchDebate }) => {
  if (!researchDebate) return null;

  const { bull_case, bear_case, summary } = researchDebate;
  const hasContent = bull_case.length > 0 || bear_case.length > 0 || summary;
  if (!hasContent) return null;

  return (
    <div className="committee-research-debate">
      <h3>리서치 토론</h3>
      <div className="debate-cols">
        <div className="bull-col">
          <h4 className="bull">상승(Bull) 근거</h4>
          {bull_case.length === 0 ? (
            <p className="empty">아직 상승 근거가 없습니다.</p>
          ) : (
            <ul>
              {bull_case.map((claim, i) => (
                <li key={`bull-${i}`}>
                  <span className={`weight weight-${claim.weight}`}>
                    {claim.weight}
                  </span>{" "}
                  <span className="src">[{claim.source}]</span> {claim.text}
                </li>
              ))}
            </ul>
          )}
        </div>
        <div className="bear-col">
          <h4 className="bear">하락(Bear) 근거</h4>
          {bear_case.length === 0 ? (
            <p className="empty">아직 하락 근거가 없습니다.</p>
          ) : (
            <ul>
              {bear_case.map((claim, i) => (
                <li key={`bear-${i}`}>
                  <span className={`weight weight-${claim.weight}`}>
                    {claim.weight}
                  </span>{" "}
                  <span className="src">[{claim.source}]</span> {claim.text}
                </li>
              ))}
            </ul>
          )}
        </div>
      </div>
      {summary && <p className="summary">{summary}</p>}
      <style>{`
        .committee-research-debate {
          padding: 16px;
          background: #fdfdfe;
          border: 1px solid #dee2e6;
          border-radius: 4px;
          margin-bottom: 16px;
        }
        .debate-cols {
          display: grid;
          grid-template-columns: 1fr 1fr;
          gap: 16px;
        }
        .bull { color: #28a745; }
        .bear { color: #dc3545; }
        .empty { color: #888; font-style: italic; }
        .weight {
          display: inline-block;
          padding: 1px 6px;
          border-radius: 3px;
          font-size: 0.75em;
          font-weight: 600;
        }
        .weight-low { background: #f0f0f0; color: #555; }
        .weight-medium { background: #fff3cd; color: #856404; }
        .weight-high { background: #f8d7da; color: #721c24; }
        .src { color: #6c757d; font-size: 0.85em; }
        .summary {
          margin-top: 12px;
          padding-top: 8px;
          border-top: 1px dashed #dee2e6;
          color: #495057;
        }
      `}</style>
    </div>
  );
};
