import { WARNING_TOKEN_LABEL } from "../i18n";
import { labelOperatorToken } from "../i18n/formatters";
import styles from "./WarningChips.module.css";

interface Props {
  tokens: string[];
}

const TOKEN_RE = /^[a-z][a-z0-9_]{0,63}$/;

function labelFor(token: string): string {
  return WARNING_TOKEN_LABEL[token] ?? labelOperatorToken(token);
}

export default function WarningChips({ tokens }: Props) {
  const safe = tokens.filter((t) => TOKEN_RE.test(t));
  if (safe.length === 0) return null;
  return (
    <ul aria-label="경고" className={styles.list}>
      {safe.map((token) => (
        <li
          aria-label={`경고: ${labelFor(token)}`}
          className={styles.chip}
          key={token}
        >
          {labelFor(token)}
        </li>
      ))}
    </ul>
  );
}
