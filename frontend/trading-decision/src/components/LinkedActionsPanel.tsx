import type { ActionDetail, CounterfactualDetail } from "../api/types";
import { formatDateTime } from "../format/datetime";
import { formatDecimal } from "../format/decimal";
import { ACTION_KIND_LABEL, COMMON, TRACK_KIND_LABEL } from "../i18n";
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
    return <p>연결된 액션이 없습니다.</p>;
  }

  return (
    <section className={styles.panel} aria-label="연결된 액션">
      {actions.length > 0 ? (
        <div className={styles.list}>
          {actions.map((action) => (
            <article className={styles.row} key={action.id}>
              <div>
                <strong>{ACTION_KIND_LABEL[action.action_kind]}</strong>{" "}
                <span className={styles.meta}>
                  {action.external_source ?? COMMON.unknown} ·{" "}
                  {formatDateTime(action.recorded_at)}
                </span>
              </div>
              <strong className={styles.externalId}>{externalId(action)}</strong>
              <details className={styles.payload}>
                <summary>페이로드 스냅샷</summary>
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
              <strong>{TRACK_KIND_LABEL[counterfactual.track_kind]}</strong>
              <p>
                기준가 {formatDecimal(counterfactual.baseline_price)} (시각:{" "}
                {formatDateTime(counterfactual.baseline_at)})
              </p>
              {counterfactual.quantity ? (
                <p>수량 {formatDecimal(counterfactual.quantity)}</p>
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
  const fallback = "(외부 ID 없음)";
  if (action.action_kind === "live_order") {
    return action.external_order_id ?? fallback;
  }
  if (action.action_kind === "paper_order") {
    return action.external_paper_id ?? fallback;
  }
  if (action.action_kind === "watch_alert") {
    return action.external_watch_id ?? fallback;
  }
  return fallback;
}
