import type { ReconciliationStatus } from "../api/reconciliation";
import styles from "./ReconciliationBadge.module.css";

interface Props {
  value: ReconciliationStatus | null;
}

const LABEL: Record<ReconciliationStatus, string> = {
  maintain: "Maintain",
  near_fill: "Near fill",
  too_far: "Too far",
  chasing_risk: "Chasing risk",
  data_mismatch: "Data mismatch",
  kr_pending_non_nxt: "KR broker only",
  unknown_venue: "Unknown venue",
  unknown: "Unknown",
};

export default function ReconciliationBadge({ value }: Props) {
  if (value === null) return null;
  const label = LABEL[value];
  return (
    <span
      aria-label={`Reconciliation status: ${label}`}
      className={`${styles.badge} ${styles[value]}`}
    >
      {label}
    </span>
  );
}
