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
    return (
      <span className={`${styles.badge} ${styles.review}`}>
        NXT review needed
      </span>
    );
  }
  if (nxtEligible === false) {
    return (
      <span className={`${styles.badge} ${styles.nonNxt}`}>
        Non-NXT (KR broker)
      </span>
    );
  }
  if (nxtEligible === null) {
    return (
      <span className={`${styles.badge} ${styles.unknown}`}>
        NXT eligibility unknown
      </span>
    );
  }
  if (
    nxtClassification !== null &&
    ACTIONABLE.indexOf(nxtClassification) >= 0
  ) {
    return (
      <span className={`${styles.badge} ${styles.actionable}`}>
        NXT actionable
      </span>
    );
  }
  return (
    <span className={`${styles.badge} ${styles.notActionable}`}>
      NXT not actionable
    </span>
  );
}
