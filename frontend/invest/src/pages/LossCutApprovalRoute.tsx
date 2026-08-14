import { useEffect, useMemo, useState, type ReactNode } from "react";
import { useParams } from "react-router-dom";
import {
  beginLossCutApproval,
  confirmLossCutApproval,
  fetchLossCutProposalEvidence,
  fetchLossCutSymbolEvidence,
} from "../api/lossCutApproval";
import type {
  LossCutBeginResponse,
  LossCutConfirmResponse,
  LossCutEvidenceField,
  LossCutEvidenceResponse,
} from "../types/lossCutApproval";

const STATUS_LABELS = {
  filled: "채워짐",
  stale: "오래됨",
  missing: "없음",
  unavailable: "기록 불가",
  source_error: "출처 오류",
} as const;

function EvidenceCard({ field }: { field: LossCutEvidenceField }) {
  return (
    <article className={`loss-cut-evidence-card is-${field.status}`}>
      <header>
        <h2>{field.label}</h2>
        <span>{STATUS_LABELS[field.status]}</span>
      </header>
      {field.value ? <pre>{JSON.stringify(field.value, null, 2)}</pre> : null}
      {field.reason ? <p className="loss-cut-evidence-reason">{field.reason}</p> : null}
      <dl>
        <div>
          <dt>출처</dt>
          <dd>{field.source ?? "미기록"}</dd>
        </div>
        <div>
          <dt>기준 시각</dt>
          <dd>{field.as_of ?? "미기록"}</dd>
        </div>
      </dl>
    </article>
  );
}

function EvidencePanel({ evidence }: { evidence: LossCutEvidenceResponse }) {
  const fields = [
    evidence.loss,
    evidence.reason,
    evidence.r931,
    evidence.consensus,
    evidence.watch,
  ];
  return (
    <>
      <section className="loss-cut-position-panel" aria-label="승인 대상 scope">
        <h2>승인 대상 계좌·수량</h2>
        {evidence.positions.length === 0 ? (
          <p>이 종목의 현재 account position을 확인하지 못했습니다.</p>
        ) : (
          evidence.positions.map((position) => (
            <dl key={`${position.account_ref}:${position.symbol}`}>
              <div>
                <dt>계좌</dt>
                <dd>{position.account_ref}</dd>
              </div>
              <div>
                <dt>종목</dt>
                <dd>{position.symbol}</dd>
              </div>
              <div>
                <dt>총수량 / 매도가능</dt>
                <dd>
                  {position.total_quantity} / {position.sellable_quantity ?? "확인 불가"}
                </dd>
              </div>
              <div>
                <dt>평단 / 현재가</dt>
                <dd>
                  {position.average_price ?? "확인 불가"} /{" "}
                  {position.current_price ?? "확인 불가"}
                </dd>
              </div>
            </dl>
          ))
        )}
      </section>
      <section className="loss-cut-evidence-grid" aria-label="손절 승인 증거 5필드">
        {fields.map((field) => (
          <EvidenceCard key={field.label} field={field} />
        ))}
      </section>
    </>
  );
}

function PageFrame({
  title,
  evidence,
  loading,
  error,
  children,
}: {
  title: string;
  evidence: LossCutEvidenceResponse | null;
  loading: boolean;
  error: string | null;
  children?: ReactNode;
}) {
  return (
    <main className="loss-cut-approval-page">
      <header className="loss-cut-approval-hero">
        <p className="loss-cut-eyebrow">/invest · 손절 승인</p>
        <h1>{title}</h1>
        <p>
          각 증거의 출처와 빈 상태를 확인하세요. 값이 없으면 빈칸을 숨기지 않습니다.
        </p>
      </header>
      {loading ? <p role="status">최신 증거를 확인하는 중입니다.</p> : null}
      {error ? <p role="alert" className="loss-cut-error">{error}</p> : null}
      {evidence ? <EvidencePanel evidence={evidence} /> : null}
      {children}
    </main>
  );
}

export function LossCutEvidenceRoute() {
  const { symbol = "" } = useParams();
  const [evidence, setEvidence] = useState<LossCutEvidenceResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    const controller = new AbortController();
    fetchLossCutSymbolEvidence(symbol, controller.signal)
      .then(setEvidence)
      .catch((reason: unknown) => {
        if (!controller.signal.aborted) setError(String(reason));
      });
    return () => controller.abort();
  }, [symbol]);
  return (
    <PageFrame
      title={`${symbol} 손절 증거`}
      evidence={evidence}
      loading={!evidence && !error}
      error={error}
    />
  );
}

export function LossCutApprovalRoute() {
  const { proposalId = "" } = useParams();
  const [evidence, setEvidence] = useState<LossCutEvidenceResponse | null>(null);
  const [begin, setBegin] = useState<LossCutBeginResponse | null>(null);
  const [confirmed, setConfirmed] = useState<LossCutConfirmResponse | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    fetchLossCutProposalEvidence(proposalId, controller.signal)
      .then(setEvidence)
      .catch((reason: unknown) => {
        if (!controller.signal.aborted) setError(String(reason));
      });
    return () => controller.abort();
  }, [proposalId]);

  const fingerprint = useMemo(
    () => begin?.fingerprint ?? confirmed?.fingerprint ?? evidence?.fingerprint,
    [begin, confirmed, evidence],
  );

  async function onBegin() {
    setBusy(true);
    setError(null);
    try {
      const result = await beginLossCutApproval(proposalId);
      setBegin(result);
      setEvidence(result.evidence);
    } catch (reason) {
      setError(String(reason));
    } finally {
      setBusy(false);
    }
  }

  async function onConfirm() {
    if (!begin) return;
    setBusy(true);
    setError(null);
    try {
      const result = await confirmLossCutApproval(proposalId, begin.ceremony_id);
      setConfirmed(result);
      setEvidence(result.evidence);
      setBegin(null);
    } catch (reason) {
      setError(String(reason));
    } finally {
      setBusy(false);
    }
  }

  return (
    <PageFrame
      title={`${evidence?.symbol ?? "손절"} 승인 검증`}
      evidence={evidence}
      loading={!evidence && !error}
      error={error}
    >
      {fingerprint ? (
        <section className="loss-cut-fingerprint" aria-label="승인 대상 fingerprint">
          <h2>고정된 대상 fingerprint</h2>
          <pre>{JSON.stringify(fingerprint, null, 2)}</pre>
        </section>
      ) : null}

      <section className="loss-cut-two-step" aria-label="2단계 확인">
        <h2>2단계 확인</h2>
        <p>
          B1은 승인 검증 이벤트까지만 기록합니다. 이 화면에서 주문을 제출하지 않습니다.
        </p>
        {!begin && !confirmed ? (
          <button
            type="button"
            disabled={busy || !evidence?.can_begin}
            onClick={onBegin}
          >
            1단계 · 현재 증거와 대상을 고정
          </button>
        ) : null}
        {begin ? (
          <div className="loss-cut-confirm-box">
            <p>
              만료: <time>{begin.expires_at}</time>
            </p>
            <p>위 계좌·종목·수량·가격 fingerprint가 의도와 같은지 다시 확인하세요.</p>
            <button type="button" disabled={busy} onClick={onConfirm}>
              2단계 · 손절 승인 검증 확정
            </button>
          </div>
        ) : null}
        {confirmed ? (
          <p role="status" className="loss-cut-success">
            승인 검증이 단일 사용으로 기록됐습니다. 주문 제출은 실행되지 않았습니다.
          </p>
        ) : null}
      </section>
    </PageFrame>
  );
}
