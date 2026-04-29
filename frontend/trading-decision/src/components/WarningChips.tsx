import styles from "./WarningChips.module.css";

interface Props {
  tokens: string[];
}

const FRIENDLY: Record<string, string> = {
  missing_quote: "Quote missing",
  stale_quote: "Quote stale",
  missing_orderbook: "Orderbook missing",
  missing_support_resistance: "Support / resistance unavailable",
  missing_kr_universe: "KR universe row missing",
  non_nxt_venue: "Non-NXT venue",
  unknown_venue: "Unknown venue",
  unknown_side: "Unknown side",
};

const TOKEN_RE = /^[a-z][a-z0-9_]{0,63}$/;

export default function WarningChips({ tokens }: Props) {
  const safe = tokens.filter((t) => TOKEN_RE.test(t));
  if (safe.length === 0) return null;
  return (
    <ul aria-label="Warnings" className={styles.list}>
      {safe.map((token) => (
        <li
          aria-label={`Warning: ${FRIENDLY[token] ?? token}`}
          className={styles.chip}
          key={token}
        >
          {FRIENDLY[token] ?? token}
        </li>
      ))}
    </ul>
  );
}
