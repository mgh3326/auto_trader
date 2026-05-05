import type { ReconciliationStatus } from "../api/reconciliation";
import { RECONCILIATION_STATUS_LABEL } from "../i18n";
import styles from "./ReconciliationBadge.module.css";

interface Props {
  value: ReconciliationStatus | null;
}

export default function ReconciliationBadge({ value }: Props) {
  if (value === null) return null;
  const label = RECONCILIATION_STATUS_LABEL[value];
  return (
    <span
      aria-label={`조정 상태: ${label}`}
      className={`${styles.badge} ${styles[value]}`}
    >
      {label}
    </span>
  );
}
