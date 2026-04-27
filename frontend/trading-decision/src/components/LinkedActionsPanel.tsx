import type { ActionDetail, CounterfactualDetail } from "../api/types";
import { formatDateTime } from "../format/datetime";
import { formatDecimal } from "../format/decimal";
import styles from "./LinkedActionsPanel.module.css";

interface LinkedActionsPanelProps {
  actions: ActionDetail[];
  counterfactuals: CounterfactualDetail[];
}

export default function LinkedActionsPanel({
  actions,
  counterfactuals,
}: LinkedActionsPanelProps) {
  if (actions.length === 0 && counterfactuals.length === 0) {
    return <p>No linked actions yet.</p>;
  }

  return (
    <section className={styles.panel} aria-label="Linked actions">
      {actions.length > 0 ? (
        <div className={styles.list}>
          {actions.map((action) => (
            <article className={styles.row} key={action.id}>
              <div>
                <strong>{action.action_kind}</strong>{" "}
                <span className={styles.meta}>
                  {action.external_source ?? "unknown"} ·{" "}
                  {formatDateTime(action.recorded_at)}
                </span>
              </div>
              <strong className={styles.externalId}>{externalId(action)}</strong>
              <details className={styles.payload}>
                <summary>Payload snapshot</summary>
                <pre>{JSON.stringify(action.payload_snapshot, null, 2)}</pre>
              </details>
            </article>
          ))}
        </div>
      ) : null}
      {counterfactuals.length > 0 ? (
        <div className={styles.list}>
          {counterfactuals.map((counterfactual) => (
            <article className={styles.row} key={counterfactual.id}>
              <strong>{counterfactual.track_kind}</strong>
              <p>
                Baseline {formatDecimal(counterfactual.baseline_price)} at{" "}
                {formatDateTime(counterfactual.baseline_at)}
              </p>
              {counterfactual.quantity ? (
                <p>Quantity {formatDecimal(counterfactual.quantity)}</p>
              ) : null}
              {counterfactual.notes ? <p>{counterfactual.notes}</p> : null}
            </article>
          ))}
        </div>
      ) : null}
    </section>
  );
}

function externalId(action: ActionDetail): string {
  if (action.action_kind === "live_order") {
    return action.external_order_id ?? "(no external id)";
  }
  if (action.action_kind === "paper_order") {
    return action.external_paper_id ?? "(no external id)";
  }
  if (action.action_kind === "watch_alert") {
    return action.external_watch_id ?? "(no external id)";
  }
  return "(no external id)";
}
