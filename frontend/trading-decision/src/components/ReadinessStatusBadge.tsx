import type { PreopenNewsReadinessStatus } from "../api/types";
import styles from "./ReadinessStatusBadge.module.css";

const LABELS: Record<PreopenNewsReadinessStatus, string> = {
  ready: "Ready",
  stale: "Stale",
  unavailable: "Unavailable",
};

export interface ReadinessStatusBadgeProps {
  status: PreopenNewsReadinessStatus;
}

export default function ReadinessStatusBadge({
  status,
}: ReadinessStatusBadgeProps) {
  return (
    <span
      className={`${styles.badge} ${styles[status]}`}
      data-status={status}
      role="status"
    >
      {LABELS[status]}
    </span>
  );
}
