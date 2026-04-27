import type { SessionStatus, UserResponseValue } from "../api/types";
import styles from "./StatusBadge.module.css";

interface StatusBadgeProps {
  value: SessionStatus | UserResponseValue;
}

export default function StatusBadge({ value }: StatusBadgeProps) {
  return <span className={`${styles.badge} ${styles[value]}`}>{value}</span>;
}
