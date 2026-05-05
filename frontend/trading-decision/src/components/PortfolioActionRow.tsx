// frontend/trading-decision/src/components/PortfolioActionRow.tsx
import { Link } from "react-router-dom";
import type { CandidateAction, PortfolioActionCandidate } from "../api/types";
import { portfolioActions as t } from "../i18n/ko";
import styles from "./PortfolioActionRow.module.css";

const ACTION_LABEL: Record<CandidateAction, string> = {
  sell: t.actionSell,
  trim: t.actionTrim,
  hold: t.actionHold,
  add: t.actionAdd,
  watch: t.actionWatch,
};

const REASON_LABEL: Record<string, string> = {
  overweight: t.reasonOverweight,
  underweight: t.reasonUnderweight,
  research_bullish: t.reasonResearchBullish,
  research_bearish: t.reasonResearchBearish,
  research_not_bullish: t.reasonResearchNotBullish,
  research_missing: t.reasonResearchMissing,
  near_resistance: t.reasonNearResistance,
  near_support: t.reasonNearSupport,
};

const MISSING_LABEL: Record<string, string> = {
  journal_missing: t.missingJournal,
  staked_quantity_unknown: t.missingStakedUnknown,
};

function fmtPct(value: number | null | undefined): string {
  if (value === null || value === undefined) return "—";
  return `${value.toFixed(2)}%`;
}

export interface PortfolioActionRowProps {
  candidate: PortfolioActionCandidate;
}

export default function PortfolioActionRow({ candidate }: PortfolioActionRowProps) {
  const researchHref = candidate.latest_research_session_id
    ? `/trading/decisions/research/sessions/${candidate.latest_research_session_id}/summary`
    : null;
  const orderPreviewHref = `/trading/decisions/orders/preview?symbol=${encodeURIComponent(candidate.symbol)}`;

  return (
    <tr className={styles.row}>
      <td className={styles.symbol}>
        <div>{candidate.symbol}</div>
        {candidate.name && <small>{candidate.name}</small>}
      </td>
      <td>{fmtPct(candidate.position_weight_pct)}</td>
      <td className={(candidate.profit_rate ?? 0) < 0 ? styles.loss : styles.gain}>
        {fmtPct(candidate.profit_rate)}
      </td>
      <td>
        <span className={`${styles.badge} ${styles[`action_${candidate.candidate_action}`]}`}>
          {ACTION_LABEL[candidate.candidate_action]}
          {candidate.suggested_trim_pct ? ` (${candidate.suggested_trim_pct}%)` : ""}
        </span>
      </td>
      <td>{candidate.summary_decision ?? "—"}</td>
      <td>{candidate.market_verdict ?? "—"}</td>
      <td>{fmtPct(candidate.nearest_support_pct)}</td>
      <td>{fmtPct(candidate.nearest_resistance_pct)}</td>
      <td>{candidate.journal_status}</td>
      <td>
        <ul className={styles.reasonList}>
          {candidate.reason_codes.map((code) => (
            <li key={code}>{REASON_LABEL[code] ?? code}</li>
          ))}
        </ul>
      </td>
      <td>
        {candidate.missing_context_codes.length > 0 ? (
          <ul className={styles.missingList}>
            {candidate.missing_context_codes.map((code) => (
              <li key={code}>{MISSING_LABEL[code] ?? code}</li>
            ))}
          </ul>
        ) : (
          "—"
        )}
      </td>
      <td className={styles.actions}>
        {researchHref && <Link to={researchHref}>{t.linkResearch}</Link>}
        <Link to={orderPreviewHref}>{t.linkOrderPreview}</Link>
      </td>
    </tr>
  );
}
