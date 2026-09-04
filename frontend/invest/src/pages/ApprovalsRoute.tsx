import { useEffect, useRef, useState, type ReactNode } from "react";
import { Link, useParams } from "react-router-dom";
import {
  ApprovalProcessingError,
  ApprovalTerminalError,
  fetchOrderProposalApproval,
  fetchOrderProposalApprovals,
  mutateOrderProposalApproval,
} from "../api/orderProposalApproval";
import { DesktopShell } from "../desktop/DesktopShell";
import { useViewport } from "../hooks/useViewport";
import { MobileShell } from "../mobile/MobileShell";
import type {
  ApprovalAction,
  ApprovalMutationResult,
  OrderProposalApprovalCard,
} from "../types/orderProposalApproval";

function ApprovalFrame({ children }: { children: ReactNode }) {
  const viewport = useViewport();
  if (viewport === "mobile") {
    return <MobileShell title="주문 승인"><div style={{ padding: "14px 16px 24px" }}>{children}</div></MobileShell>;
  }
  return <DesktopShell center={children} />;
}

function Preview({ card }: { card: OrderProposalApprovalCard }) {
  return (
    <ul aria-label="주문 프리뷰" style={{ margin: 0, paddingLeft: 18 }}>
      {card.preview.map((preview, index) => (
        <li key={index}>
          #{index + 1} · 수량 {preview.quantity ?? "미상"} · 지정가 {preview.limit_price ?? "시장가"} · 예상 {preview.expected_amount ?? "미상"}
        </li>
      ))}
    </ul>
  );
}

function Card({ card }: { card: OrderProposalApprovalCard }) {
  return (
    <article
      data-testid={`approval-card-${card.proposal_id}`}
      style={{ border: "1px solid var(--border)", borderRadius: 14, padding: 16, background: "var(--surface)" }}
    >
      <div style={{ display: "flex", justifyContent: "space-between", gap: 12 }}>
        <div>
          <h2 style={{ margin: 0 }}>{card.symbol}</h2>
          <p style={{ margin: "4px 0", color: "var(--fg-2)" }}>
            {card.market} · {card.account_mode} · {card.side} · {card.action}
          </p>
        </div>
        <span>{card.status}</span>
      </div>
      <Preview card={card} />
      <p style={{ color: "var(--fg-2)" }}>유효 기한: {card.valid_until ?? "미기록"}</p>
      <Link to={`/approvals/${card.proposal_id}`}>상세 및 승인</Link>
    </article>
  );
}

function Result({ result }: { result: ApprovalMutationResult | null }) {
  if (!result) return null;
  return (
    <section role="status" aria-label="승인 처리 결과">
      처리 결과: {result.reason}
    </section>
  );
}

export function ApprovalsListRoute() {
  const [cards, setCards] = useState<OrderProposalApprovalCard[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    fetchOrderProposalApprovals(controller.signal)
      .then((response) => setCards(response.items))
      .catch((reason: unknown) => {
        if (!controller.signal.aborted) setError(String(reason));
      });
    return () => controller.abort();
  }, []);

  return (
    <ApprovalFrame>
      <header>
        <p>/invest · 주문 승인</p>
        <h1>대기 중인 주문 제안</h1>
        <p>카드 발행 당시의 프리뷰입니다. 승인 시 서버가 다시 검증합니다.</p>
      </header>
      {error ? <p role="alert">{error}</p> : null}
      {!cards && !error ? <p role="status">승인 카드를 불러오는 중입니다.</p> : null}
      {cards?.length === 0 ? <p>표시할 승인 카드가 없습니다.</p> : null}
      <div style={{ display: "grid", gap: 12 }}>{cards?.map((card) => <Card key={card.proposal_id} card={card} />)}</div>
    </ApprovalFrame>
  );
}

export function ApprovalDetailRoute() {
  const { proposalId = "" } = useParams();
  const [card, setCard] = useState<OrderProposalApprovalCard | null>(null);
  const [result, setResult] = useState<ApprovalMutationResult | null>(null);
  const [busy, setBusy] = useState(false);
  const [polling, setPolling] = useState(false);
  const [confirmationToken, setConfirmationToken] = useState<string | null>(null);
  const [terminal, setTerminal] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const inFlight = useRef(false);

  useEffect(() => {
    const controller = new AbortController();
    fetchOrderProposalApproval(proposalId, controller.signal)
      .then(setCard)
      .catch((reason: unknown) => {
        if (!controller.signal.aborted) setError(String(reason));
      });
    return () => controller.abort();
  }, [proposalId]);

  useEffect(() => {
    if (!polling) return;
    const timer = window.setInterval(() => {
      fetchOrderProposalApproval(proposalId)
        .then((next) => {
          setCard(next);
          if (next.status === "terminal") {
            setPolling(false);
            setResult((current) => current ?? { handled: true, reason: "terminal" });
          }
        })
        .catch((reason: unknown) => setError(String(reason)));
    }, 2_000);
    return () => window.clearInterval(timer);
  }, [polling, proposalId]);

  async function mutate(action: ApprovalAction) {
    if (inFlight.current) return;
    inFlight.current = true;
    setBusy(true);
    setError(null);
    try {
      const next = await mutateOrderProposalApproval(
        proposalId,
        action,
        confirmationToken ?? undefined,
      );
      setResult(next);
      if (next.confirmation_token) setConfirmationToken(next.confirmation_token);
    } catch (reason) {
      if (reason instanceof ApprovalProcessingError) {
        setPolling(true);
      } else if (reason instanceof ApprovalTerminalError) {
        setTerminal(true);
        setError(reason.message);
      } else {
        setError(String(reason));
      }
    } finally {
      setBusy(false);
      inFlight.current = false;
    }
  }

  const disabled = busy || polling || terminal || !card || card.status === "terminal";
  const needsLossCutConfirm = result?.reason === "loss_cut_confirmation_required";
  return (
    <ApprovalFrame>
      <Link to="/approvals">← 승인 목록</Link>
      {!card && !error ? <p role="status">승인 카드를 불러오는 중입니다.</p> : null}
      {error ? <p role="alert">{error}</p> : null}
      {polling ? <p role="status">처리 중입니다. 상태를 확인하고 있습니다.</p> : null}
      {card ? (
        <article>
          <header>
            <p>/invest · 주문 승인</p>
            <h1>{card.symbol} 주문 제안</h1>
            <p>{card.market} · {card.account_mode} · {card.side} · {card.action}</p>
          </header>
          <Preview card={card} />
          <p>유효 기한: {card.valid_until ?? "미기록"}</p>
          <p>approval hash: {card.approval_hash_present ? "발행됨" : "없음"}</p>
          <section aria-label="최근 rung 결과">
            <h2>최근 결과</h2>
            <ul>{card.recent_result.map((rung) => <li key={rung.rung_index}>#{rung.rung_index + 1}: {rung.state}</li>)}</ul>
          </section>
          <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
            {!needsLossCutConfirm ? <button type="button" disabled={disabled} onClick={() => mutate("approve")}>승인</button> : null}
            {!needsLossCutConfirm ? <button type="button" disabled={disabled} onClick={() => mutate("deny")}>거부</button> : null}
            {needsLossCutConfirm ? <button type="button" disabled={disabled} onClick={() => mutate("loss-cut-confirm")}>손절 승인 확정</button> : null}
          </div>
          <Result result={result} />
        </article>
      ) : null}
    </ApprovalFrame>
  );
}
