import { useCallback, useEffect, useRef, useState } from "react";

import {
  ApprovalProcessingError,
  ApprovalTerminalError,
  mutateOrderProposalApproval,
} from "../api/orderProposalApproval";
import type { ApprovalAction } from "../types/orderProposalApproval";
import { fetchTraderApproval, fetchTraderApprovals } from "./api";
import { fmtNum, fmtPct, formatCountdown, remainingSeconds } from "./format";
import type {
  TraderApprovalDetailResponse,
  TraderApprovalInboxResponse,
  TraderApprovalItem,
} from "./types";

interface RowResult {
  reason: string;
  message: string | null;
}

interface RowUi {
  pending: boolean;
  result: RowResult | null;
  confirmToken: string | null;
  detail: TraderApprovalDetailResponse | null;
  /** The last detail re-read failed (e.g. 404 once the proposal is gone). */
  detailFailed: boolean;
}

interface RetainedRow {
  item: TraderApprovalItem;
  receivedAt: number;
}

const EMPTY_ROW: RowUi = {
  pending: false,
  result: null,
  confirmToken: null,
  detail: null,
  detailFailed: false,
};

const CONFIRMABLE_RUNG_STATES = new Set(["pending_approval", "needs_reconfirm"]);

/**
 * True only while the server-side detail is exactly the in-flight loss-cut
 * confirmation step: the first click published the confirmation card
 * (reported as not_human_card), nothing approved or leased it yet, it has not
 * expired, a rung still awaits the decision and both approval gates are on.
 * Denied (terminal), finished (approved_at / commit lease), expired, gated-off
 * or missing proposals all read false, so the second-click button hides.
 */
function awaitingLossCutConfirmation(detail: TraderApprovalDetailResponse | null): boolean {
  if (detail === null) return false;
  const d = detail.item;
  return (
    detail.actions_enabled &&
    detail.loss_cut_actions_enabled &&
    d.card_kind === "loss_cut_confirmation" &&
    d.block_reason === "not_human_card" &&
    d.approved_at === null &&
    !d.commit_lease_active &&
    d.expires_in_seconds !== null &&
    d.expires_in_seconds > 0 &&
    d.rungs.some((rung) => CONFIRMABLE_RUNG_STATES.has(rung.state))
  );
}

const RUNG_STATE_LABEL: Record<string, string> = {
  acked: "접수",
  resting: "접수",
  unverified: "확인 불가",
  rejected: "거부",
};

function toErrorMessage(err: unknown): string {
  return err instanceof Error ? err.message : String(err);
}

export function ApprovalInboxPanel() {
  const [inbox, setInbox] = useState<TraderApprovalInboxResponse | null>(null);
  const [inboxReceivedAt, setInboxReceivedAt] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [rowUi, setRowUi] = useState<Record<string, RowUi>>({});
  const [retained, setRetained] = useState<Record<string, RetainedRow>>({});
  const pendingRef = useRef(new Set<string>());
  const rowUiRef = useRef(rowUi);
  rowUiRef.current = rowUi;
  const [, setTick] = useState(0);

  const patchRow = useCallback((id: string, patch: Partial<RowUi>) => {
    setRowUi((prev) => ({ ...prev, [id]: { ...EMPTY_ROW, ...prev[id], ...patch } }));
  }, []);

  const refreshDetail = useCallback(
    async (id: string) => {
      try {
        const detail = await fetchTraderApproval(id);
        patchRow(id, { detail, detailFailed: false });
      } catch {
        // A failed re-read keeps the last known broker state on the row but
        // withdraws any pending loss-cut confirmation button.
        patchRow(id, { detailFailed: true });
      }
    },
    [patchRow],
  );

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const data = await fetchTraderApprovals();
      setInbox(data);
      setInboxReceivedAt(Date.now());
      // A row the server lists again as actionable (e.g. a republished
      // reconfirm card with a fresh nonce) gets its buttons back. Rows with a
      // request in flight or a pending loss-cut confirmation keep their state.
      const relisted = new Set(data.items.map((i) => i.proposal_id));
      setRowUi((prev) => {
        const next: Record<string, RowUi> = {};
        for (const [id, ui] of Object.entries(prev)) {
          const keep =
            !relisted.has(id) || ui.pending || pendingRef.current.has(id) || ui.confirmToken !== null;
          if (keep) next[id] = ui;
        }
        return next;
      });
      // A row holding a first-click token re-reads its detail so a proposal
      // denied, finished or expired elsewhere drops its confirmation button.
      for (const [id, ui] of Object.entries(rowUiRef.current)) {
        if (ui.confirmToken !== null) void refreshDetail(id);
      }
    } catch (err) {
      setError(toErrorMessage(err));
    } finally {
      setLoading(false);
    }
  }, [refreshDetail]);

  useEffect(() => {
    void load();
  }, [load]);

  useEffect(() => {
    const timer = setInterval(() => setTick((t) => t + 1), 1000);
    return () => clearInterval(timer);
  }, []);

  const runAction = useCallback(
    async (item: TraderApprovalItem, action: ApprovalAction, token?: string) => {
      const id = item.proposal_id;
      if (pendingRef.current.has(id)) return;
      pendingRef.current.add(id);
      patchRow(id, { pending: true });
      let result: RowResult;
      let confirmToken: string | null = null;
      try {
        const res = await mutateOrderProposalApproval(id, action, token);
        result = { reason: res.reason, message: null };
        if (res.reason === "loss_cut_confirmation_required" && res.confirmation_token) {
          confirmToken = res.confirmation_token;
        }
      } catch (err) {
        if (err instanceof ApprovalProcessingError) {
          result = { reason: "processing", message: null };
        } else if (err instanceof ApprovalTerminalError) {
          result = { reason: "terminal", message: err.message };
        } else {
          result = { reason: "error", message: toErrorMessage(err) };
        }
      }
      let detail: TraderApprovalDetailResponse | null = null;
      try {
        detail = await fetchTraderApproval(id);
      } catch {
        detail = null;
      }
      patchRow(id, { pending: false, result, confirmToken, detail, detailFailed: detail === null });
      pendingRef.current.delete(id);
      setRetained((prev) =>
        prev[id] ? prev : { ...prev, [id]: { item, receivedAt: inboxReceivedAt } },
      );
    },
    [patchRow, inboxReceivedAt],
  );

  const now = Date.now();
  const items = inbox?.items ?? [];
  const listed = new Set(items.map((i) => i.proposal_id));
  const rows: RetainedRow[] = [
    ...items.map((item) => ({ item, receivedAt: inboxReceivedAt })),
    ...Object.values(retained).filter((r) => !listed.has(r.item.proposal_id)),
  ];
  const actionsEnabled = inbox?.actions_enabled === true;
  const lossCutEnabled = inbox?.loss_cut_actions_enabled === true;

  return (
    <section className="trader-panel" data-testid="panel-approval-inbox">
      <header className="trader-panel-head">
        <h2>승인 대기{inbox ? ` (${inbox.count})` : ""}</h2>
        <button type="button" onClick={() => void load()} disabled={loading}>
          새로고침
        </button>
      </header>
      {error ? <p className="trader-error">조회 실패: {error}</p> : null}
      {inbox && rows.length === 0 ? (
        <p className="trader-dim">대기 중인 승인 요청 없음</p>
      ) : null}
      {!inbox && !error && loading ? <p className="trader-dim">불러오는 중…</p> : null}
      {rows.map(({ item, receivedAt }) => {
        const id = item.proposal_id;
        const ui = rowUi[id] ?? EMPTY_ROW;
        const remaining = remainingSeconds(item.expires_in_seconds, receivedAt, now);
        const expired = remaining !== null && remaining <= 0;
        const acted = ui.result !== null;
        const showBase = !acted && actionsEnabled && item.actionable && !expired;
        const showApprove = showBase && (!item.requires_two_step || lossCutEnabled);
        const showDeny = showBase;
        const showConfirm =
          acted &&
          ui.confirmToken !== null &&
          actionsEnabled &&
          lossCutEnabled &&
          !expired &&
          !ui.detailFailed &&
          awaitingLossCutConfirmation(ui.detail);
        return (
          <article className="trader-approval-row" data-testid={`approval-row-${id}`} key={id}>
            <header className="trader-approval-head">
              <strong>{item.symbol}</strong>
              <span>
                {item.account_mode}
                {item.broker_account_id ? ` · ${item.broker_account_id}` : ""}
              </span>
              <span>{item.side}</span>
              {item.requires_two_step ? (
                <span className="trader-chip trader-chip-degraded">2단계</span>
              ) : null}
              {item.tier ? <span className="trader-chip">{item.tier}</span> : null}
            </header>
            <p>
              {item.action} · {item.order_type} · 수량{" "}
              <span data-testid={`qty-${id}`}>{fmtNum(item.total_quantity)}</span> · 괴리{" "}
              {fmtPct(item.distance_pct)}
            </p>
            <ul className="trader-approval-rungs">
              {item.rungs.map((rung) => (
                <li key={rung.rung_index}>
                  {rung.side} {fmtNum(rung.quantity)} x{" "}
                  {rung.limit_price !== null ? fmtNum(rung.limit_price) : "-"}
                  {rung.distance_pct !== null ? ` · ${fmtPct(rung.distance_pct)}` : ""}
                </li>
              ))}
            </ul>
            {item.caveats.length > 0 ? (
              <p className="trader-dim">{item.caveats.join(" · ")}</p>
            ) : null}
            <p className="trader-dim">
              남은 시간 <span data-testid={`expires-${id}`}>{formatCountdown(remaining)}</span>
            </p>
            {showApprove || showDeny || showConfirm ? (
              <div className="trader-approval-actions">
                {showApprove ? (
                  <button
                    type="button"
                    data-testid={`approve-${id}`}
                    disabled={ui.pending}
                    onClick={() => void runAction(item, "approve")}
                  >
                    {item.requires_two_step ? "손절 승인" : "승인"}
                  </button>
                ) : null}
                {showDeny ? (
                  <button
                    type="button"
                    data-testid={`deny-${id}`}
                    disabled={ui.pending}
                    onClick={() => void runAction(item, "deny")}
                  >
                    기각
                  </button>
                ) : null}
                {showConfirm ? (
                  <button
                    type="button"
                    data-testid={`loss-cut-confirm-${id}`}
                    disabled={ui.pending}
                    onClick={() => void runAction(item, "loss-cut-confirm", ui.confirmToken ?? undefined)}
                  >
                    손절 최종 확인
                  </button>
                ) : null}
              </div>
            ) : null}
            {ui.result !== null ? (
              <p data-testid={`approval-result-${id}`} data-reason={ui.result.reason}>
                {ui.result.reason}
                {ui.result.message ? ` — ${ui.result.message}` : ""}
              </p>
            ) : null}
            {ui.detail !== null ? (
              <ul className="trader-approval-rungs">
                {ui.detail.item.rungs.map((rung) => (
                  <li
                    key={rung.rung_index}
                    data-testid={`broker-state-${id}-${rung.rung_index}`}
                    data-state={rung.state}
                  >
                    {RUNG_STATE_LABEL[rung.state] ?? rung.state}
                    {rung.broker_order_id !== null ? ` · ${rung.broker_order_id}` : ""}
                  </li>
                ))}
              </ul>
            ) : null}
            {acted ? (
              <button
                type="button"
                className="trader-approval-state-refresh"
                onClick={() => void refreshDetail(id)}
              >
                상태 새로고침
              </button>
            ) : null}
          </article>
        );
      })}
    </section>
  );
}
