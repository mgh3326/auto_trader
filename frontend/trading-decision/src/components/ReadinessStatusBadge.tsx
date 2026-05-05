import type { PreopenNewsReadinessStatus } from "../api/types";
import { NEWS_READINESS_LABEL } from "../i18n";
import styles from "./ReadinessStatusBadge.module.css";

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
      {NEWS_READINESS_LABEL[status]}
    </span>
  );
}
