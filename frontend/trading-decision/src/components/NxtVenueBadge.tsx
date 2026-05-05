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
    const badgeLabel = "NXT 검토 필요";
    return (
      <span
        aria-label={`NXT 거래소: ${badgeLabel}`}
        className={`${styles.badge} ${styles.review}`}
      >
        {badgeLabel}
      </span>
    );
  }
  if (nxtEligible === false) {
    const badgeLabel = "비-NXT (국내 브로커)";
    return (
      <span
        aria-label={`NXT 거래소: ${badgeLabel}`}
        className={`${styles.badge} ${styles.nonNxt}`}
      >
        {badgeLabel}
      </span>
    );
  }
  if (nxtEligible === null) {
    const badgeLabel = "NXT 자격 알 수 없음";
    return (
      <span
        aria-label={`NXT 거래소: ${badgeLabel}`}
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
    const badgeLabel = "NXT 실행 가능";
    return (
      <span
        aria-label={`NXT 거래소: ${badgeLabel}`}
        className={`${styles.badge} ${styles.actionable}`}
      >
        {badgeLabel}
      </span>
    );
  }
  const badgeLabel = "NXT 실행 불가";
  return (
    <span
      aria-label={`NXT 거래소: ${badgeLabel}`}
      className={`${styles.badge} ${styles.notActionable}`}
    >
      {badgeLabel}
    </span>
  );
}
