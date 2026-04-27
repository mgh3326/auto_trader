import type { OutcomeDetail, OutcomeHorizon, TrackKind } from "../api/types";
import { formatDateTime } from "../format/datetime";
import { formatDecimal } from "../format/decimal";
import styles from "./OutcomesPanel.module.css";

const TRACKS: TrackKind[] = [
  "accepted_live",
  "accepted_paper",
  "rejected_counterfactual",
  "analyst_alternative",
  "user_alternative",
];
const HORIZONS: OutcomeHorizon[] = ["1h", "4h", "1d", "3d", "7d", "final"];

interface OutcomesPanelProps {
  outcomes: OutcomeDetail[];
}

export default function OutcomesPanel({ outcomes }: OutcomesPanelProps) {
  if (outcomes.length === 0) {
    return <p className={styles.empty}>No outcome marks yet.</p>;
  }

  const cell = (track: TrackKind, horizon: OutcomeHorizon) =>
    outcomes.find((o) => o.track_kind === track && o.horizon === horizon);

  return (
    <table className={styles.table} aria-label="Outcome marks">
      <thead>
        <tr>
          <th scope="col">Track</th>
          {HORIZONS.map((h) => (
            <th key={h} scope="col">
              {h}
            </th>
          ))}
        </tr>
      </thead>
      <tbody>
        {TRACKS.map((track) => (
          <tr key={track}>
            <th scope="row" className={styles.trackCell}>
              {track}
            </th>
            {HORIZONS.map((h) => {
              const o = cell(track, h);
              if (!o) {
                return (
                  <td key={h} className={styles.empty}>
                    —
                  </td>
                );
              }
              return (
                <td key={h} className={styles.cell}>
                  <span title={tooltip(o)}>{formatPct(o.pnl_pct)}</span>
                </td>
              );
            })}
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function formatPct(pct: string | null | undefined): string {
  if (pct === null || pct === undefined) return "—";
  const n = Number(pct);
  if (!Number.isFinite(n)) return pct;
  return `${formatDecimal(pct, "en-US", { maximumFractionDigits: 2 })}%`;
}

function tooltip(o: OutcomeDetail): string {
  return [
    `price_at_mark: ${formatDecimal(o.price_at_mark)}`,
    o.pnl_amount ? `pnl_amount: ${formatDecimal(o.pnl_amount)}` : null,
    `marked_at: ${formatDateTime(o.marked_at)}`,
  ]
    .filter(Boolean)
    .join(" · ");
}
