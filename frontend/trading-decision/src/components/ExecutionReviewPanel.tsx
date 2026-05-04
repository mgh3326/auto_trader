import type {
  ExecutionReviewStage,
  ExecutionReviewSummary,
  OrderBasketPreview,
} from "../api/types";
import styles from "./ExecutionReviewPanel.module.css";

interface Props {
  review: ExecutionReviewSummary | null;
}

function StatusPill({ status }: { status: ExecutionReviewStage["status"] }) {
  return (
    <span className={`${styles.stageStatus} ${styles[status] ?? ""}`}>
      {status}
    </span>
  );
}

function BasketPreviewBlock({ basket }: { basket: OrderBasketPreview | null }) {
  if (!basket) return null;
  return (
    <div aria-label="Basket preview" role="group">
      <div className={styles.basketHeader}>
        <strong>Basket preview</strong>
        <span>
          {basket.account_mode} · {basket.lines.length} lines
        </span>
      </div>
      <ul className={styles.basketLines}>
        {basket.lines.map((line, idx) => (
          <li className={styles.basketLine} key={`${line.symbol}-${idx}`}>
            <span>
              <strong>{line.symbol}</strong>
              <span> · {line.market}</span>
            </span>
            <span>{line.side}</span>
            <span>{line.quantity ?? "—"}</span>
            <span>{line.limit_price ?? "—"}</span>
            <span>
              {line.guard.approval_required ? "Approval required" : "—"}
            </span>
          </li>
        ))}
      </ul>
      {basket.basket_warnings.length > 0 ? (
        <ul className={styles.warnings} aria-label="Basket warnings">
          {basket.basket_warnings.map((w) => (
            <li className={styles.warningChip} key={w}>
              {w}
            </li>
          ))}
        </ul>
      ) : null}
    </div>
  );
}

export default function ExecutionReviewPanel({ review }: Props) {
  if (!review) return null;

  return (
    <section
      aria-label="Execution review"
      className={styles.panel}
    >
      <div className={styles.header}>
        <div>
          <h2 className={styles.headerTitle}>Execution review</h2>
          <p>
            Read-only stage view of preopen execution readiness. This page does
            not submit orders.
          </p>
        </div>
        <span className={styles.statusBadge}>Execution disabled</span>
      </div>

      <div className={styles.guardrail} role="note">
        <p>
          <strong>Advisory / read-only.</strong> No live execution. Mock
          execution requires later explicit operator approval.
        </p>
      </div>

      <ul className={styles.stages} aria-label="Execution review stages">
        {review.stages.map((stage) => (
          <li className={styles.stage} key={stage.stage_id}>
            <strong>{stage.label}</strong>
            <StatusPill status={stage.status} />
            <span>{stage.summary}</span>
            {stage.warnings.length > 0 ? (
              <ul
                className={styles.warnings}
                aria-label={`${stage.label} warnings`}
              >
                {stage.warnings.map((w) => (
                  <li className={styles.warningChip} key={w}>
                    {w}
                  </li>
                ))}
              </ul>
            ) : null}
          </li>
        ))}
      </ul>

      <BasketPreviewBlock basket={review.basket_preview} />

      {review.blocking_reasons.length > 0 ? (
        <ul className={styles.warnings} aria-label="Execution blocking reasons">
          {review.blocking_reasons.map((reason) => (
            <li className={styles.warningChip} key={reason}>
              {reason}
            </li>
          ))}
        </ul>
      ) : null}
    </section>
  );
}
