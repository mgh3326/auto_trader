import type { NxtClassification } from "../api/reconciliation";
import styles from "./NxtVenueBadge.module.css";

interface Props {
  marketScope: string | null;
  nxtClassification: NxtClassification | null;
  nxtEligible: boolean | null;
}

const ACTIONABLE: ReadonlyArray<NxtClassification> = [
  "buy_pending_actionable",
  "sell_pending_actionable",
  "buy_pending_at_support",
  "sell_pending_near_resistance",
];

export default function NxtVenueBadge({
  marketScope,
  nxtClassification,
  nxtEligible,
}: Props) {
  if (marketScope !== "kr") return null;

  if (nxtClassification === "data_mismatch_requires_review") {
    const badgeLabel = "NXT review needed";
    return (
      <span
        aria-label={`NXT venue: ${badgeLabel}`}
        className={`${styles.badge} ${styles.review}`}
      >
        {badgeLabel}
      </span>
    );
  }
  if (nxtEligible === false) {
    const badgeLabel = "Non-NXT (KR broker)";
    return (
      <span
        aria-label={`NXT venue: ${badgeLabel}`}
        className={`${styles.badge} ${styles.nonNxt}`}
      >
        {badgeLabel}
      </span>
    );
  }
  if (nxtEligible === null) {
    const badgeLabel = "NXT eligibility unknown";
    return (
      <span
        aria-label={`NXT venue: ${badgeLabel}`}
        className={`${styles.badge} ${styles.unknown}`}
      >
        {badgeLabel}
      </span>
    );
  }
  if (
    nxtClassification !== null &&
    ACTIONABLE.indexOf(nxtClassification) >= 0
  ) {
    const badgeLabel = "NXT actionable";
    return (
      <span
        aria-label={`NXT venue: ${badgeLabel}`}
        className={`${styles.badge} ${styles.actionable}`}
      >
        {badgeLabel}
      </span>
    );
  }
  const badgeLabel = "NXT not actionable";
  return (
    <span
      aria-label={`NXT venue: ${badgeLabel}`}
      className={`${styles.badge} ${styles.notActionable}`}
    >
      {badgeLabel}
    </span>
  );
}
