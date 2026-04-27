import type {
  OutcomeHorizon,
  SessionAnalyticsResponse,
  TrackKind,
} from "../api/types";
import { formatDecimal } from "../format/decimal";
import styles from "./AnalyticsMatrix.module.css";

interface AnalyticsMatrixProps {
  data: SessionAnalyticsResponse;
}

export default function AnalyticsMatrix({ data }: AnalyticsMatrixProps) {
  if (data.cells.length === 0) {
    return <p className={styles.empty}>No outcomes yet for this session.</p>;
  }

  const lookup = new Map<string, (typeof data.cells)[number]>();
  for (const c of data.cells) lookup.set(`${c.track_kind}|${c.horizon}`, c);
  const cell = (track: TrackKind, h: OutcomeHorizon) =>
    lookup.get(`${track}|${h}`);

  return (
    <table className={styles.table} aria-label="Outcome analytics">
      <thead>
        <tr>
          <th scope="col">Track</th>
          {data.horizons.map((h) => (
            <th key={h} scope="col">
              {h}
            </th>
          ))}
        </tr>
      </thead>
      <tbody>
        {data.tracks.map((track) => (
          <tr key={track}>
            <th scope="row" className={styles.trackCell}>
              {track}
            </th>
            {data.horizons.map((h) => {
              const c = cell(track, h);
              if (!c)
                return (
                  <td key={h} className={styles.empty}>
                    —
                  </td>
                );
              return (
                <td key={h} className={styles.cell}>
                  <strong>{formatPct(c.mean_pnl_pct)}</strong>
                  <span className={styles.meta}>n={c.outcome_count}</span>
                </td>
              );
            })}
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function formatPct(p: string | null): string {
  if (p === null) return "—";
  return `${formatDecimal(p, "en-US", { maximumFractionDigits: 2 })}%`;
}
