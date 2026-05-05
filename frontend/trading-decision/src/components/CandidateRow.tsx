// frontend/trading-decision/src/components/CandidateRow.tsx
import type { ScreenedCandidate } from "../api/types";
import { candidates as t } from "../i18n/ko";
import styles from "./CandidateRow.module.css";

function fmt(value: number | null | undefined, digits = 2): string {
  if (value === null || value === undefined) return "—";
  return Number(value).toFixed(digits);
}

export interface CandidateRowProps {
  candidate: ScreenedCandidate;
  onStartResearch: (candidate: ScreenedCandidate) => void;
  busy?: boolean;
}

export default function CandidateRow({
  candidate,
  onStartResearch,
  busy,
}: CandidateRowProps) {
  return (
    <tr className={styles.row}>
      <td className={styles.symbol}>
        <div>{candidate.symbol}</div>
        {candidate.name && <small>{candidate.name}</small>}
      </td>
      <td>{fmt(candidate.price, 0)}</td>
      <td className={(candidate.change_rate ?? 0) < 0 ? styles.loss : styles.gain}>
        {fmt(candidate.change_rate)}%
      </td>
      <td>{fmt(candidate.volume, 0)}</td>
      <td>{fmt(candidate.trade_amount_24h, 0)}</td>
      <td>{candidate.volume_ratio === null ? "—" : fmt(candidate.volume_ratio)}</td>
      <td>{fmt(candidate.rsi)}</td>
      <td>{fmt(candidate.market_cap, 0)}</td>
      <td>{candidate.is_held ? <span className={styles.heldBadge}>{t.held}</span> : t.notHeld}</td>
      <td>
        {candidate.data_warnings.length > 0 ? (
          <ul className={styles.warnList}>
            {candidate.data_warnings.map((w) => (
              <li key={w}>{w}</li>
            ))}
          </ul>
        ) : (
          "—"
        )}
      </td>
      <td className={styles.actions}>
        <button type="button" onClick={() => onStartResearch(candidate)} disabled={busy}>
          {t.startResearch}
        </button>
        <a href={`/trading/decisions/orders/preview?symbol=${encodeURIComponent(candidate.symbol)}`}>
          {t.linkOrderPreview}
        </a>
      </td>
    </tr>
  );
}
