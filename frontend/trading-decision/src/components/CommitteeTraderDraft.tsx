import React from "react";
import type {
  CommitteeTraderAction,
  CommitteeTraderDraft as TraderDraftType,
} from "../api/types";

interface Props {
  traderDraft: TraderDraftType[] | null;
}

const ACTION_LABEL: Record<CommitteeTraderAction, string> = {
  BUY: "매수",
  REBALANCE: "리밸런스",
  HOLD: "보유",
  WATCH: "관찰",
  TRIM: "축소",
  SELL: "매도",
  AVOID: "회피",
};

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
      <h3>트레이더 초안</h3>
      <p className="advisory">
        초안일 뿐이며, 실주문이 생성되지 않습니다.
      </p>
      <ul className="drafts">
        {traderDraft.map((draft, i) => (
          <li key={`${draft.symbol}-${i}`} className="draft">
            <div className="head">
              <span
                className="action"
                style={{ background: ACTION_COLORS[draft.action] }}
              >
                {ACTION_LABEL[draft.action] ?? draft.action}
              </span>
              <span className="symbol">{draft.symbol}</span>
              <span className={`confidence confidence-${draft.confidence}`}>
                {draft.confidence}
              </span>
            </div>
            <dl className="details">
              {draft.price_plan && (
                <>
                  <dt>가격 계획</dt>
                  <dd>{draft.price_plan}</dd>
                </>
              )}
              {draft.size_plan && (
                <>
                  <dt>수량 계획</dt>
                  <dd>{draft.size_plan}</dd>
                </>
              )}
              {draft.rationale && (
                <>
                  <dt>근거</dt>
                  <dd>{draft.rationale}</dd>
                </>
              )}
              {draft.invalidation_condition && (
                <>
                  <dt>무효화 조건</dt>
                  <dd>{draft.invalidation_condition}</dd>
                </>
              )}
              {draft.next_step_recommendation && (
                <>
                  <dt>다음 단계</dt>
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
