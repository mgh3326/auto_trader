import type { SessionStatus, UserResponseValue } from "../api/types";
import { SESSION_STATUS_LABEL, USER_RESPONSE_LABEL } from "../i18n";
import styles from "./StatusBadge.module.css";

interface StatusBadgeProps {
  value: SessionStatus | UserResponseValue;
}

function labelFor(value: SessionStatus | UserResponseValue): string {
  if (value in SESSION_STATUS_LABEL) {
    return SESSION_STATUS_LABEL[value as SessionStatus];
  }
  return USER_RESPONSE_LABEL[value as UserResponseValue];
}

export default function StatusBadge({ value }: StatusBadgeProps) {
  return (
    <span className={`${styles.badge} ${styles[value]}`}>{labelFor(value)}</span>
  );
}
