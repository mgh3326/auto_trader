import { useState } from "react";
import type {
  OutcomeCreateRequest,
  ProposalDetail,
  ProposalRespondRequest,
} from "../api/types";
import { parseReconciliationPayload } from "../api/reconciliation";
import { formatDateTime } from "../format/datetime";
import { formatDecimal } from "../format/decimal";
import {
  COMMON,
  PROPOSAL_KIND_LABEL,
  SIDE_LABEL,
} from "../i18n";
import LinkedActionsPanel from "./LinkedActionsPanel";
import NxtVenueBadge from "./NxtVenueBadge";
import OriginalVsAdjustedSummary from "./OriginalVsAdjustedSummary";
import OutcomeMarkForm from "./OutcomeMarkForm";
import OutcomesPanel from "./OutcomesPanel";
import ProposalAdjustmentEditor from "./ProposalAdjustmentEditor";
import ProposalResponseControls from "./ProposalResponseControls";
import ReconciliationBadge from "./ReconciliationBadge";
import ReconciliationDecisionSupportPanel from "./ReconciliationDecisionSupportPanel";
import StatusBadge from "./StatusBadge";
import WarningChips from "./WarningChips";
import styles from "./ProposalRow.module.css";

interface ProposalRowProps {
  proposal: ProposalDetail;
  onRespond: (
    proposalUuid: string,
    body: ProposalRespondRequest,
  ) => Promise<{ ok: boolean; status?: number; detail?: string }>;
  onRecordOutcome: (
    proposalUuid: string,
    body: OutcomeCreateRequest,
  ) => Promise<{ ok: boolean; status?: number; detail?: string }>;
}

const valuePairs = [
  ["수량", "original_quantity", "user_quantity"],
  ["수량 비율(%)", "original_quantity_pct", "user_quantity_pct"],
  ["금액", "original_amount", "user_amount"],
  ["가격", "original_price", "user_price"],
  ["트리거 가격", "original_trigger_price", "user_trigger_price"],
  ["임계 비율(%)", "original_threshold_pct", "user_threshold_pct"],
] as const;

export default function ProposalRow({
  proposal,
  onRespond,
  onRecordOutcome,
}: ProposalRowProps) {
  const [adjustResponse, setAdjustResponse] = useState<
    "modify" | "partial_accept" | null
  >(null);
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [banner, setBanner] = useState<string | null>(null);
  const displayName = getDisplayName(proposal);
  const shouldShowSymbol = displayName !== proposal.symbol;

  const recon = parseReconciliationPayload(proposal.original_payload);
  const cryptoPaperWorkflow = proposal.original_payload.crypto_paper_workflow;
  const nonActionable =
    proposal.proposal_kind === "other" &&
    recon !== null &&
    recon.candidate_kind === "pending_order" &&
    (recon.reconciliation_status === "kr_pending_non_nxt" ||
      recon.nxt_classification === "non_nxt_pending_ignore_for_nxt" ||
      recon.nxt_classification === "data_mismatch_requires_review");

  async function respond(body: ProposalRespondRequest) {
    setIsSubmitting(true);
    setBanner(null);
    const result = await onRespond(proposal.proposal_uuid, body);
    setIsSubmitting(false);
    if (!result.ok) {
      const message =
        result.status === 409
          ? "세션이 보관되었습니다. 더 이상 응답할 수 없습니다."
          : (result.detail ?? COMMON.somethingWentWrong);
      setBanner(message);
      return { ok: false, detail: message };
    }
    setAdjustResponse(null);
    return { ok: true };
  }

  return (
    <article className={`${styles.row} ${nonActionable ? styles.nonActionable : ""}`}>
      <header className={styles.header}>
        <div className={styles.identity}>
          <h2 className={styles.name}>{displayName}</h2>
          {shouldShowSymbol ? (
            <span className={styles.symbol}>{proposal.symbol}</span>
          ) : null}
        </div>
        <span className={styles.chip}>{SIDE_LABEL[proposal.side]}</span>
        <span className={styles.chip}>{PROPOSAL_KIND_LABEL[proposal.proposal_kind]}</span>
        <StatusBadge value={proposal.user_response} />
        {recon ? (
          <>
            <ReconciliationBadge value={recon.reconciliation_status} />
            <NxtVenueBadge
              marketScope={inferMarketScope(proposal)}
              nxtClassification={recon.nxt_classification}
              nxtEligible={recon.nxt_eligible}
            />
          </>
        ) : null}
      </header>
      {banner ? (
        <div className={styles.banner} role="alert">
          {banner}
        </div>
      ) : null}
      <div className={styles.panels}>
        <section className={styles.panel}>
          <h3>원본</h3>
          <ValueList proposal={proposal} prefix="original" />
          {proposal.original_rationale ? (
            <p className={styles.rationale}>{proposal.original_rationale}</p>
          ) : null}
        </section>
        {proposal.user_response !== "pending" ? (
          <section className={styles.panel}>
            <h3>내 결정</h3>
            <p>
              <StatusBadge value={proposal.user_response} /> ·{" "}
              {formatDateTime(proposal.responded_at)}
            </p>
            <OriginalVsAdjustedSummary pairs={summaryPairs(proposal)} />
            {proposal.user_note ? <p>{proposal.user_note}</p> : null}
          </section>
        ) : null}
      </div>
      {cryptoPaperWorkflow?.approval_copy?.length ? (
        <section className={styles.cryptoPaperWorkflow}>
          <h3>암호화폐 모의 워크플로우</h3>
          <ul>
            {cryptoPaperWorkflow.approval_copy.map((line) => (
              <li key={line}>{line}</li>
            ))}
          </ul>
        </section>
      ) : null}
      {recon ? (
        <>
          <WarningChips tokens={recon.warnings} />
          <ReconciliationDecisionSupportPanel
            side={proposal.side}
            originalPrice={proposal.original_price}
            originalQuantity={proposal.original_quantity}
            payload={recon}
          />
          {nonActionable ? (
            <p className={styles.nonActionableAlert} role="alert">
              비-NXT 대기 주문 — 국내 브로커 라우팅 전용. 결정 전에 검토하세요. 이 행의 응답 기록은 브로커 주문을 제출하거나 취소하지 않습니다.
            </p>
          ) : null}
        </>
      ) : null}
      <ProposalResponseControls
        currentResponse={proposal.user_response}
        isSubmitting={isSubmitting}
        onOpenAdjust={setAdjustResponse}
        onSimpleResponse={(response) => void respond({ response })}
      />
      {!nonActionable ? (
        <p className={styles.safetyNote}>
          수락은 결정만 기록합니다. 실주문을 전송하지 않습니다.
        </p>
      ) : null}
      {adjustResponse ? (
        <ProposalAdjustmentEditor
          onCancel={() => setAdjustResponse(null)}
          onSubmit={respond}
          proposal={proposal}
          response={adjustResponse}
        />
      ) : null}
      <LinkedActionsPanel
        actions={proposal.actions}
        counterfactuals={proposal.counterfactuals}
      />
      <section className={styles.outcomes} aria-label="결과 마크">
        <OutcomesPanel outcomes={proposal.outcomes} />
        <details>
          <summary>결과 마크 기록</summary>
          <OutcomeMarkForm
            counterfactuals={proposal.counterfactuals}
            onSubmit={(body) => onRecordOutcome(proposal.proposal_uuid, body)}
          />
        </details>
      </section>
    </article>
  );
}

function inferMarketScope(proposal: ProposalDetail): string {
  if (proposal.instrument_type === "equity_kr") return "kr";
  if (proposal.instrument_type === "equity_us") return "us";
  if (proposal.instrument_type === "crypto") return "crypto";
  return "";
}

function ValueList({
  proposal,
  prefix,
}: {
  proposal: ProposalDetail;
  prefix: "original" | "user";
}) {
  const rows: Array<{ label: string; value: string }> = [];
  for (const [label, originalKey, userKey] of valuePairs) {
    const key = prefix === "original" ? originalKey : userKey;
    const value = proposal[key];
    if (value !== null) rows.push({ label, value });
  }

  return (
    <dl className={styles.values}>
      {rows.map((row) => (
        <div key={row.label}>
          <dt>{row.label}</dt>
          <dd>{formatProposalValue(row.label, row.value, proposal)}</dd>
        </div>
      ))}
    </dl>
  );
}

function getDisplayName(proposal: ProposalDetail) {
  const payloadName = proposal.original_payload?.name;
  if (typeof payloadName === "string" && payloadName.trim().length > 0) {
    return payloadName;
  }
  return proposal.symbol;
}

function isMissingSellAmount(
  label: string,
  value: string,
  proposal: ProposalDetail,
) {
  return (
    label === "금액" &&
    proposal.side === "sell" &&
    Number(value) === 0 &&
    proposal.original_price === null
  );
}

function formatProposalValue(
  label: string,
  value: string,
  proposal: ProposalDetail,
) {
  if (isMissingSellAmount(label, value, proposal)) {
    return "현재 시세 추정이 필요합니다";
  }
  return `${formatDecimal(value)}${
    label === "금액" && proposal.original_currency
      ? ` ${proposal.original_currency}`
      : ""
  }`;
}

function summaryPairs(proposal: ProposalDetail) {
  return valuePairs
    .filter(([, originalKey]) => proposal[originalKey] !== null)
    .map(([label, originalKey, userKey]) => ({
      label,
      original: formatDecimal(proposal[originalKey]),
      user: proposal[userKey] ? formatDecimal(proposal[userKey]) : null,
    }));
}
