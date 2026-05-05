import type {
  ExecutionReviewStage,
  ExecutionReviewSummary,
  OrderBasketPreview,
} from "../api/types";
import {
  EXECUTION_ACCOUNT_MODE_LABEL,
  EXECUTION_REVIEW_STAGE_STATUS_LABEL,
} from "../i18n";
import { labelOrToken, labelOrderSide } from "../i18n/formatters";
import styles from "./ExecutionReviewPanel.module.css";

interface Props {
  review: ExecutionReviewSummary | null;
}

function StatusPill({ status }: { status: ExecutionReviewStage["status"] }) {
  return (
    <span className={`${styles.stageStatus} ${styles[status] ?? ""}`}>
      {EXECUTION_REVIEW_STAGE_STATUS_LABEL[status]}
    </span>
  );
}

function BasketPreviewBlock({ basket }: { basket: OrderBasketPreview | null }) {
  if (!basket) return null;
  return (
    <div aria-label="바스켓 미리보기" role="group">
      <div className={styles.basketHeader}>
        <strong>바스켓 미리보기</strong>
        <span>
          {labelOrToken(EXECUTION_ACCOUNT_MODE_LABEL, basket.account_mode)} ·{" "}
          {basket.lines.length}건
        </span>
      </div>
      <ul className={styles.basketLines}>
        {basket.lines.map((line, idx) => (
          <li className={styles.basketLine} key={`${line.symbol}-${idx}`}>
            <span>
              <strong>{line.symbol}</strong>
              <span> · {line.market}</span>
            </span>
            <span>{labelOrderSide(line.side)}</span>
            <span>{line.quantity ?? "—"}</span>
            <span>{line.limit_price ?? "—"}</span>
            <span>
              {line.guard.approval_required ? "승인 필요" : "—"}
            </span>
          </li>
        ))}
      </ul>
      {basket.basket_warnings.length > 0 ? (
        <ul className={styles.warnings} aria-label="바스켓 경고">
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
      aria-label="실행 리뷰"
      className={styles.panel}
    >
      <div className={styles.header}>
        <div>
          <h2 className={styles.headerTitle}>실행 리뷰</h2>
          <p>
            장전 실행 준비 상태의 읽기 전용 단계 뷰입니다. 이 페이지는 주문을
            제출하지 않습니다.
          </p>
        </div>
        <span className={styles.statusBadge}>실행 비활성화</span>
      </div>

      <div className={styles.guardrail} role="note">
        <p>
          <strong>자문 / 읽기 전용.</strong> 실주문 실행 없음. 모의 실행은
          이후 명시적인 운영자 승인이 필요합니다.
        </p>
      </div>

      <ul className={styles.stages} aria-label="실행 리뷰 단계">
        {review.stages.map((stage) => (
          <li className={styles.stage} key={stage.stage_id}>
            <strong>{stage.label}</strong>
            <StatusPill status={stage.status} />
            <span>{stage.summary}</span>
            {stage.warnings.length > 0 ? (
              <ul
                className={styles.warnings}
                aria-label={`${stage.label} 경고`}
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
        <ul className={styles.warnings} aria-label="실행 차단 사유">
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
