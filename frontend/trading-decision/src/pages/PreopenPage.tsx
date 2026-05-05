import { useCallback, useEffect, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import ErrorView from "../components/ErrorView";
import LoadingView from "../components/LoadingView";
import MarketNewsBriefingSection from "../components/MarketNewsBriefingSection";
import NewsReadinessSection from "../components/NewsReadinessSection";
import ExecutionReviewPanel from "../components/ExecutionReviewPanel";
import { ApiError } from "../api/client";
import {
  createDecisionFromResearchRun,
  getLatestPreopen,
} from "../api/preopen";
import type {
  PreopenBriefingArtifact,
  PreopenLatestResponse,
  PreopenPaperApprovalBridge,
  PreopenQaEvaluatorSummary,
} from "../api/types";
import { formatDateTime } from "../format/datetime";
import {
  ARTIFACT_READINESS_LABEL,
  ARTIFACT_STATUS_LABEL,
  CANDIDATE_KIND_LABEL,
  COMMON,
  NXT_CLASSIFICATION_LABEL,
  PAPER_APPROVAL_CANDIDATE_STATUS_LABEL,
  PAPER_APPROVAL_STATUS_LABEL,
  QA_CHECK_STATUS_LABEL,
  QA_CONFIDENCE_LABEL,
  QA_GRADE_LABEL,
  QA_SEVERITY_LABEL,
  QA_STATUS_LABEL,
  RECONCILIATION_STATUS_LABEL,
  SIDE_LABEL,
  VENUE_LABEL,
} from "../i18n";
import {
  labelOperatorToken,
  labelOrToken,
  labelOrderSide,
  labelYesNo,
} from "../i18n/formatters";
import styles from "./PreopenPage.module.css";

type State =
  | { status: "loading" }
  | { status: "error"; message: string }
  | { status: "success"; data: PreopenLatestResponse };

function formatArtifactVersion(version: string): string {
  return version.startsWith("mvp.") ? version.slice(4) : version;
}

function labelArtifactCta(
  cta: PreopenBriefingArtifact["cta"] | null,
): string {
  if (!cta) return "의사결정 세션 생성";
  if (cta.state === "create_available") return "의사결정 세션 생성";
  if (cta.state === "linked_session_exists") return "의사결정 세션 열기";
  if (cta.state === "unavailable") return "의사결정 세션 생성 불가";
  return "의사결정 세션 생성";
}

function PreopenBriefingArtifactSection({
  artifact,
}: {
  artifact: PreopenBriefingArtifact | null;
}) {
  if (!artifact) return null;

  return (
    <section
      aria-label="장전 브리핑 산출물"
      className={styles.artifactSection}
    >
      <div className={styles.artifactHeader}>
        <div>
          <h2>장전 브리핑</h2>
          <p className={styles.meta}>
            {artifact.artifact_type} {formatArtifactVersion(artifact.artifact_version)}
          </p>
        </div>
        <span className={styles.artifactStatus}>
          산출물 {labelOrToken(ARTIFACT_STATUS_LABEL, artifact.status)}
        </span>
      </div>

      {artifact.market_summary ? <p>{artifact.market_summary}</p> : null}
      {artifact.news_summary ? <p>뉴스 요약: {artifact.news_summary}</p> : null}

      {artifact.risk_notes.length > 0 ? (
        <ul aria-label="장전 산출물 리스크 노트" className={styles.warnings}>
          {artifact.risk_notes.map((note) => (
            <li className={styles.warningChip} key={note}>
              {labelOperatorToken(note)}
            </li>
          ))}
        </ul>
      ) : null}

      {artifact.readiness.length > 0 ? (
        <div className={styles.artifactGrid}>
          {artifact.readiness.map((item) => (
            <div className={styles.artifactCard} key={item.key}>
              <strong>{labelOperatorToken(item.key)}</strong>
              <span>{labelOrToken(ARTIFACT_READINESS_LABEL, item.status)}</span>
            </div>
          ))}
        </div>
      ) : null}

      {artifact.sections.length > 0 ? (
        <div className={styles.artifactGrid}>
          {artifact.sections.map((section) => (
            <div className={styles.artifactCard} key={section.section_id}>
              <strong>{section.title}</strong>
              <span>
                {labelOrToken(ARTIFACT_STATUS_LABEL, section.status)} · {section.item_count}
              </span>
              {section.summary ? <small>{section.summary}</small> : null}
            </div>
          ))}
        </div>
      ) : null}
    </section>
  );
}

function getQaReasonLabel(qa: PreopenQaEvaluatorSummary, reason: string) {
  const matchedCheck = qa.checks.find(
    (check) => check.id === reason || check.details?.reason === reason,
  );
  return matchedCheck?.summary ?? labelOperatorToken(reason);
}

function PreopenQaEvaluatorPanel({
  qa,
}: {
  qa: PreopenQaEvaluatorSummary | null;
}) {
  if (!qa) return null;
  const scoreLabel = qa.overall.score === null ? "—" : String(qa.overall.score);
  return (
    <section aria-label="장전 QA 평가기" className={styles.qaSection}>
      <div className={styles.artifactHeader}>
        <div>
          <h2>QA 평가기</h2>
          <p className={styles.meta}>
            {qa.source} · {labelOrToken(QA_GRADE_LABEL, qa.overall.grade)} · 신뢰도 {labelOrToken(QA_CONFIDENCE_LABEL, qa.overall.confidence)}
          </p>
        </div>
        <span className={styles.artifactStatus}>
          QA {labelOrToken(QA_STATUS_LABEL, qa.status)}
        </span>
      </div>
      <p>종합 점수: {scoreLabel}</p>
      {qa.blocking_reasons.length > 0 ? (
        <ul aria-label="QA 차단 사유" className={styles.warnings}>
          {qa.blocking_reasons.map((reason) => (
            <li className={styles.warningChip} key={reason}>
              {getQaReasonLabel(qa, reason)}
            </li>
          ))}
        </ul>
      ) : null}
      {qa.warnings.length > 0 ? (
        <ul aria-label="QA 경고" className={styles.warnings}>
          {qa.warnings.map((warning) => (
            <li className={styles.warningChip} key={warning}>
              {getQaReasonLabel(qa, warning)}
            </li>
          ))}
        </ul>
      ) : null}
      <div className={styles.artifactGrid}>
        {qa.checks.map((check) => (
          <div className={styles.artifactCard} key={check.id}>
            <strong>{check.label}</strong>
            <span>
              {labelOrToken(QA_CHECK_STATUS_LABEL, check.status)} · {labelOrToken(QA_SEVERITY_LABEL, check.severity)}
            </span>
            <small>{check.summary}</small>
          </div>
        ))}
      </div>
    </section>
  );
}

function formatVenueLabel(venue: string | null, symbol: string | null): string {
  const venueLabel = labelOrToken(VENUE_LABEL, venue);
  return [venueLabel, symbol].filter(Boolean).join(" ");
}

type PreviewPayloadLike = {
  side?: unknown;
  type?: unknown;
  notional?: unknown;
  limit_price?: unknown;
  time_in_force?: unknown;
};

function formatPreviewPayload(payload: PreviewPayloadLike | null): string | null {
  if (!payload) return null;
  const side = typeof payload.side === "string" ? labelOrderSide(payload.side) : null;
  const type = typeof payload.type === "string" ? payload.type : null;
  const notional = typeof payload.notional === "string" ? payload.notional : null;
  const limitPrice =
    typeof payload.limit_price === "string" ? payload.limit_price : null;
  const tif =
    typeof payload.time_in_force === "string" ? payload.time_in_force : null;
  const parts = [side, type].filter(Boolean).join(" ");
  const price = limitPrice ? ` @ ${limitPrice}` : "";
  const suffix = tif ? ` ${tif.toUpperCase()}` : "";
  const order = [parts, notional ? `$${notional}` : null]
    .filter(Boolean)
    .join(" · ");
  return `${order}${price}${suffix}`;
}

function PreopenPaperApprovalBridgeSection({
  bridge,
}: {
  bridge: PreopenPaperApprovalBridge | null;
}) {
  if (!bridge) return null;
  const statusLabel = labelOrToken(PAPER_APPROVAL_STATUS_LABEL, bridge.status);

  return (
    <section
      aria-label="모의 승인 프리뷰"
      className={styles.paperApprovalSection}
    >
      <div className={styles.artifactHeader}>
        <div>
          <h2>모의 승인 프리뷰</h2>
          <p className={styles.meta}>
            {bridge.source} · {bridge.market_scope?.toUpperCase() ?? "시장 알 수 없음"} ·{" "}
            {bridge.eligible_count}개 대상 / {bridge.candidate_count}개 후보
          </p>
        </div>
        <span className={styles.artifactStatus}>프리뷰 {statusLabel}</span>
      </div>

      <div className={styles.paperApprovalSafety} role="note">
        자문 전용 프리뷰입니다. 이 화면에서 실행할 수 없습니다. Alpaca Paper 제출 전에 트레이더의 명시적 승인이 필요합니다. 이 카드는 모의 주문을 제출하거나 취소하지 않습니다.
      </div>

      {bridge.blocking_reasons.length > 0 ? (
        <ul aria-label="모의 승인 차단 사유" className={styles.warnings}>
          {bridge.blocking_reasons.map((reason) => (
            <li className={styles.warningChip} key={reason}>
              {labelOperatorToken(reason)}
            </li>
          ))}
        </ul>
      ) : null}
      {bridge.warnings.length > 0 || bridge.unsupported_reasons.length > 0 ? (
        <ul aria-label="모의 승인 경고" className={styles.warnings}>
          {[...bridge.warnings, ...bridge.unsupported_reasons].map((warning) => (
            <li className={styles.warningChip} key={warning}>
              {labelOperatorToken(warning)}
            </li>
          ))}
        </ul>
      ) : null}

      {bridge.candidates.length > 0 ? (
        <div className={styles.paperApprovalCandidates}>
          {bridge.candidates.map((candidate) => {
            const previewPayload = formatPreviewPayload(candidate.preview_payload);
            return (
              <article
                className={styles.paperApprovalCandidate}
                key={candidate.candidate_uuid}
              >
                <div className={styles.paperApprovalCandidateHeader}>
                  <strong>{candidate.symbol}</strong>
                  <span>{labelOrToken(PAPER_APPROVAL_CANDIDATE_STATUS_LABEL, candidate.status)}</span>
                </div>
                <dl className={styles.provenanceList}>
                  <div>
                    <dt>시그널 소스</dt>
                    <dd>
                      {formatVenueLabel(
                        candidate.signal_venue,
                        candidate.signal_symbol ?? candidate.symbol,
                      )}
                    </dd>
                  </div>
                  <div>
                    <dt>실행 거래소</dt>
                    <dd>
                      {formatVenueLabel(
                        candidate.execution_venue,
                        candidate.execution_symbol,
                      )}
                    </dd>
                  </div>
                  <div>
                    <dt>자산 분류</dt>
                    <dd>{labelOperatorToken(candidate.execution_asset_class)}</dd>
                  </div>
                  <div>
                    <dt>워크플로우</dt>
                    <dd>{labelOperatorToken(candidate.workflow_stage)}</dd>
                  </div>
                </dl>
                {candidate.purpose ? (
                  <p>목적: {labelOperatorToken(candidate.purpose)}</p>
                ) : null}
                {previewPayload ? <p>프리뷰 페이로드: {previewPayload}</p> : null}
                {candidate.approval_copy.length > 0 ? (
                  <ul
                    aria-label={`${candidate.symbol} 승인 문구`}
                    className={styles.approvalCopy}
                  >
                    {candidate.approval_copy.map((copy) => (
                      <li key={copy}>{copy}</li>
                    ))}
                  </ul>
                ) : null}
                {candidate.warnings.length > 0 ? (
                  <ul
                    aria-label={`${candidate.symbol} 모의 승인 경고`}
                    className={styles.warnings}
                  >
                    {candidate.warnings.map((warning) => (
                      <li className={styles.warningChip} key={warning}>
                        {labelOperatorToken(warning)}
                      </li>
                    ))}
                  </ul>
                ) : null}
              </article>
            );
          })}
        </div>
      ) : (
        <p>현재 사용 가능한 모의 승인 프리뷰 후보가 없습니다.</p>
      )}
    </section>
  );
}

export default function PreopenPage() {
  const navigate = useNavigate();
  const [state, setState] = useState<State>({ status: "loading" });
  const [version, setVersion] = useState(0);
  const [creating, setCreating] = useState(false);
  const [createError, setCreateError] = useState<string | null>(null);
  const [confirmPending, setConfirmPending] = useState(false);

  const refetch = useCallback(() => setVersion((v) => v + 1), []);

  useEffect(() => {
    setState({ status: "loading" });
    const controller = new AbortController();
    getLatestPreopen("kr")
      .then((data) => {
        if (!controller.signal.aborted) {
          setState({ status: "success", data });
        }
      })
      .catch((err: unknown) => {
        if (controller.signal.aborted) return;
        if (err instanceof ApiError && err.status === 401) {
          window.location.assign(
            `/login?next=${encodeURIComponent(window.location.pathname)}`,
          );
          return;
        }
        setState({
          status: "error",
          message:
            err instanceof ApiError ? err.detail : COMMON.somethingWentWrong,
        });
      });
    return () => controller.abort();
  }, [version]);

  const handleCreate = useCallback(async (runUuid: string) => {
    if (confirmPending) {
      setConfirmPending(false);
      setCreating(true);
      setCreateError(null);
      try {
        const result = await createDecisionFromResearchRun({ runUuid });
        navigate(`/sessions/${result.session_uuid}`);
      } catch (err: unknown) {
        setCreateError(
          err instanceof ApiError
            ? err.detail
            : "의사결정 세션 생성에 실패했습니다.",
        );
      } finally {
        setCreating(false);
      }
    } else {
      setConfirmPending(true);
    }
  }, [confirmPending, navigate]);

  if (state.status === "loading") return <LoadingView />;
  if (state.status === "error") {
    return (
      <main className={styles.page}>
        <ErrorView message={state.message} onRetry={refetch} />
      </main>
    );
  }

  const { data } = state;
  const artifactCta = data.briefing_artifact?.cta ?? null;
  const linkedSessionUuid =
    artifactCta?.state === "linked_session_exists"
      ? artifactCta.linked_session_uuid
      : data.linked_sessions[0]?.session_uuid;
  const createRunUuid =
    artifactCta?.state === "create_available"
      ? artifactCta.run_uuid
      : data.run_uuid;

  if (!data.has_run) {
    return (
      <main className={styles.page}>
        <h1>장전 브리핑</h1>
        <div className={styles.banner} role="status">
          <strong>사용 가능한 장전 리서치 실행 결과가 없습니다</strong>
          {data.advisory_skipped_reason ? (
            <p>사유: {data.advisory_skipped_reason}</p>
          ) : null}
        </div>
        <PreopenBriefingArtifactSection artifact={data.briefing_artifact} />
        <PreopenQaEvaluatorPanel qa={data.qa_evaluator} />
        <PreopenPaperApprovalBridgeSection bridge={data.paper_approval_bridge} />
        <ExecutionReviewPanel review={data.execution_review} />
      </main>
    );
  }

  return (
    <main className={styles.page}>
      <div className={styles.header}>
        <h1>장전 브리핑</h1>
        <div className={styles.meta}>
          생성 시각: {formatDateTime(data.generated_at)}
          {data.strategy_name ? ` · ${data.strategy_name}` : ""}
          {data.source_profile ? ` · ${data.source_profile}` : ""}
          {data.market_scope ? ` · ${data.market_scope.toUpperCase()}` : ""}
          {` · 자문 ${data.advisory_used ? "사용됨" : "사용되지 않음"}`}
        </div>
        {data.market_brief && typeof data.market_brief.summary === "string" ? (
          <p>{data.market_brief.summary}</p>
        ) : null}
      </div>

      {data.advisory_skipped_reason ? (
        <div className={styles.banner} role="status">
          자문 알림: {data.advisory_skipped_reason}
        </div>
      ) : null}

      {data.source_warnings.length > 0 ? (
        <ul aria-label="소스 경고" className={styles.warnings}>
          {data.source_warnings.map((w, i) => (
            <li className={styles.warningChip} key={i}>
              {w}
            </li>
          ))}
        </ul>
      ) : null}

      <PreopenBriefingArtifactSection artifact={data.briefing_artifact} />
      <PreopenQaEvaluatorPanel qa={data.qa_evaluator} />
      <PreopenPaperApprovalBridgeSection bridge={data.paper_approval_bridge} />
      <ExecutionReviewPanel review={data.execution_review} />
      <NewsReadinessSection news={data.news} preview={data.news_preview} />
      <MarketNewsBriefingSection briefing={data.market_news_briefing} />

      {data.candidates.length > 0 ? (
        <section className={styles.section}>
          <h2>후보 ({data.candidate_count}건)</h2>
          <table className={styles.table}>
            <thead>
              <tr>
                <th>심볼</th>
                <th>방향</th>
                <th>종류</th>
                <th>신뢰도</th>
                <th>가격</th>
                <th>수량</th>
                <th>근거</th>
              </tr>
            </thead>
            <tbody>
              {data.candidates.map((c) => (
                <tr key={c.candidate_uuid}>
                  <td>{c.symbol}</td>
                  <td>
                    <span
                      className={
                        c.side === "buy"
                          ? styles.sideBuy
                          : c.side === "sell"
                            ? styles.sideSell
                            : styles.sideNone
                      }
                    >
                      {labelOrToken(SIDE_LABEL, c.side)}
                    </span>
                  </td>
                  <td>{labelOrToken(CANDIDATE_KIND_LABEL, c.candidate_kind)}</td>
                  <td>{c.confidence !== null ? `${c.confidence}%` : "—"}</td>
                  <td>
                    {c.proposed_price !== null
                      ? `${c.proposed_price} ${c.currency ?? ""}`
                      : "—"}
                  </td>
                  <td>{c.proposed_qty !== null ? c.proposed_qty : "—"}</td>
                  <td className={styles.rationale} title={c.rationale ?? ""}>
                    {c.rationale ?? "—"}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </section>
      ) : null}

      {data.reconciliations.length > 0 ? (
        <section className={styles.section}>
          <h2>대기 조정 ({data.reconciliation_count}건)</h2>
          <table className={styles.table}>
            <thead>
              <tr>
                <th>심볼</th>
                <th>분류</th>
                <th>NXT 분류</th>
                <th>실행 가능</th>
                <th>괴리 %</th>
                <th>요약</th>
              </tr>
            </thead>
            <tbody>
              {data.reconciliations.map((r, i) => (
                <tr key={i}>
                  <td>{r.symbol}</td>
                  <td>{labelOrToken(RECONCILIATION_STATUS_LABEL, r.classification)}</td>
                  <td>{labelOrToken(NXT_CLASSIFICATION_LABEL, r.nxt_classification)}</td>
                  <td>
                    {labelYesNo(r.nxt_actionable)}
                  </td>
                  <td>{r.gap_pct !== null ? `${r.gap_pct}%` : "—"}</td>
                  <td>{r.summary ?? "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </section>
      ) : null}

      {data.linked_sessions.length > 0 ? (
        <section className={styles.section}>
          <h2>연결된 의사결정 세션</h2>
          <ul className={styles.linkedSessions}>
            {data.linked_sessions.map((s) => (
              <li className={styles.linkedSessionItem} key={String(s.session_uuid)}>
                <Link to={`/sessions/${s.session_uuid}`}>
                  {String(s.session_uuid)}
                </Link>
                <span>{s.status}</span>
                <span>{formatDateTime(s.created_at)}</span>
              </li>
            ))}
          </ul>
        </section>
      ) : null}

      <div className={styles.ctaRow}>
        <Link className="btn" to="/news-radar">
          Open news radar
        </Link>
        {linkedSessionUuid ? (
          <Link
            className="btn"
            to={`/sessions/${linkedSessionUuid}`}
          >
            세션 열기
          </Link>
        ) : null}
        {!linkedSessionUuid ? (
          <button
            className="btn"
            disabled={creating || !createRunUuid || artifactCta?.state === "unavailable"}
            onClick={() => createRunUuid && handleCreate(String(createRunUuid))}
            type="button"
          >
            {confirmPending
              ? "의사결정 세션을 생성하시겠습니까?"
              : creating
                ? "생성 중…"
                : labelArtifactCta(artifactCta)}
          </button>
        ) : null}
        {confirmPending ? (
          <button
            className="btn"
            onClick={() => setConfirmPending(false)}
            type="button"
          >
            {COMMON.cancel}
          </button>
        ) : null}
        {createError ? (
          <span className={styles.inlineError} role="alert">
            {createError}
          </span>
        ) : null}
      </div>

      {data.notes ? <p>{data.notes}</p> : null}
    </main>
  );
}
