import styles from "./MarketBriefPanel.module.css";

interface MarketBriefPanelProps {
  brief: Record<string, unknown> | null;
  notes: string | null;
}

export default function MarketBriefPanel({ brief, notes }: MarketBriefPanelProps) {
  if (brief === null && notes === null) return null;
  return (
    <details className={styles.panel}>
      <summary>Market brief</summary>
      {notes ? <p className={styles.notes}>{notes}</p> : null}
      {brief ? <pre>{JSON.stringify(brief, null, 2)}</pre> : null}
    </details>
  );
}
