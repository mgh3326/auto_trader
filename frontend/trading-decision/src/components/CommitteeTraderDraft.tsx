import React from "react";
import type {
  CommitteeTraderAction,
  CommitteeTraderDraft as TraderDraftType,
} from "../api/types";

interface Props {
  traderDraft: TraderDraftType[] | null;
}

const ACTION_COLORS: Record<CommitteeTraderAction, string> = {
  BUY: "#28a745",
  REBALANCE: "#17a2b8",
  HOLD: "#6c757d",
  WATCH: "#6c757d",
  TRIM: "#fd7e14",
  SELL: "#dc3545",
  AVOID: "#dc3545",
};

export const CommitteeTraderDraft: React.FC<Props> = ({ traderDraft }) => {
  if (!traderDraft || traderDraft.length === 0) return null;

  return (
    <div className="committee-trader-draft">
      <h3>Trader Draft</h3>
      <p className="advisory">
        Draft only — no live order is created.
      </p>
      <ul className="drafts">
        {traderDraft.map((draft, i) => (
          <li key={`${draft.symbol}-${i}`} className="draft">
            <div className="head">
              <span
                className="action"
                style={{ background: ACTION_COLORS[draft.action] }}
              >
                {draft.action}
              </span>
              <span className="symbol">{draft.symbol}</span>
              <span className={`confidence confidence-${draft.confidence}`}>
                {draft.confidence}
              </span>
            </div>
            <dl className="details">
              {draft.price_plan && (
                <>
                  <dt>Price plan</dt>
                  <dd>{draft.price_plan}</dd>
                </>
              )}
              {draft.size_plan && (
                <>
                  <dt>Size plan</dt>
                  <dd>{draft.size_plan}</dd>
                </>
              )}
              {draft.rationale && (
                <>
                  <dt>Rationale</dt>
                  <dd>{draft.rationale}</dd>
                </>
              )}
              {draft.invalidation_condition && (
                <>
                  <dt>Invalidation</dt>
                  <dd>{draft.invalidation_condition}</dd>
                </>
              )}
              {draft.next_step_recommendation && (
                <>
                  <dt>Next step</dt>
                  <dd>{draft.next_step_recommendation}</dd>
                </>
              )}
            </dl>
          </li>
        ))}
      </ul>
      <style>{`
        .committee-trader-draft {
          padding: 16px;
          background: #fdfdfe;
          border: 1px solid #dee2e6;
          border-radius: 4px;
          margin-bottom: 16px;
        }
        .advisory {
          color: #6c757d;
          font-style: italic;
          font-size: 0.9em;
          margin: 0 0 12px 0;
        }
        .drafts {
          list-style: none;
          padding: 0;
          margin: 0;
          display: flex;
          flex-direction: column;
          gap: 12px;
        }
        .draft {
          padding: 8px;
          border: 1px dashed #ced4da;
          border-radius: 4px;
        }
        .head {
          display: flex;
          align-items: center;
          gap: 8px;
          margin-bottom: 8px;
        }
        .action {
          color: white;
          padding: 2px 10px;
          border-radius: 3px;
          font-weight: 700;
          font-size: 0.85em;
        }
        .symbol { font-weight: 600; font-size: 1.05em; }
        .confidence {
          padding: 1px 6px;
          border-radius: 3px;
          font-size: 0.75em;
          margin-left: auto;
        }
        .confidence-low { background: #f0f0f0; color: #555; }
        .confidence-medium { background: #fff3cd; color: #856404; }
        .confidence-high { background: #d4edda; color: #155724; }
        .details {
          margin: 0;
          display: grid;
          grid-template-columns: max-content 1fr;
          gap: 4px 12px;
        }
        .details dt {
          font-weight: 600;
          color: #495057;
        }
        .details dd {
          margin: 0;
          color: #212529;
        }
      `}</style>
    </div>
  );
};
