import { useCallback, useEffect, useMemo, useState } from "react";

import {
  fetchProtectedPositions,
  ProtectedPositionSaveError,
  saveProtectedPosition,
  type ProtectedPositionsResponse,
  type ProtectedPositionView,
  type ProtectionPreview,
} from "../api/protectedPositions";

type Scope = "kis_live" | "toss_live" | "upbit_live";
type Market = "kr" | "us" | "crypto";

type SaveStatus =
  | { kind: "idle" }
  | { kind: "saving" }
  | { kind: "saved"; revision: number | null }
  | { kind: "stale" }
  | { kind: "error"; message: string };

interface PendingConfirm {
  key: { account_scope: string; market: string; symbol: string };
  quantity: string;
  reason: string;
  expectedRevision: number | null;
  preview: ProtectionPreview;
}

function newIdempotencyKey(): string {
  return (
    globalThis.crypto?.randomUUID?.() ??
    `pq-${Date.now()}-${Math.random().toString(16).slice(2)}`
  );
}

function savedRevision(res: unknown): number | null {
  const revision = (res as { position?: { revision?: unknown } } | null)?.position?.revision;
  return typeof revision === "number" ? revision : null;
}

export function ProtectedQuantityPanel() {
  const [data, setData] = useState<ProtectedPositionsResponse | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [scope, setScope] = useState<Scope>("kis_live");
  const [market, setMarket] = useState<Market>("kr");
  const [symbol, setSymbol] = useState("");
  const [quantity, setQuantity] = useState("");
  const [reason, setReason] = useState("");
  const [confirmSymbol, setConfirmSymbol] = useState("");
  const [pending, setPending] = useState<PendingConfirm | null>(null);
  const [status, setStatus] = useState<SaveStatus>({ kind: "idle" });

  const load = useCallback(async () => {
    try {
      setData(await fetchProtectedPositions());
      setLoadError(null);
    } catch (err) {
      setLoadError(err instanceof Error ? err.message : String(err));
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const matched = useMemo<ProtectedPositionView | null>(() => {
    const needle = symbol.trim().toUpperCase();
    if (!needle) return null;
    return (
      data?.positions.find(
        (p) =>
          p.account_scope === scope &&
          p.market === market &&
          p.symbol.toUpperCase() === needle,
      ) ?? null
    );
  }, [data, scope, market, symbol]);

  const saving = status.kind === "saving";
  const submitDisabled =
    !data?.can_edit || saving || !symbol.trim() || !quantity.trim() || !reason.trim();

  async function requestPreview() {
    const key = { account_scope: scope, market, symbol: symbol.trim() };
    const draft = { quantity: quantity.trim(), reason: reason.trim() };
    const expectedRevision = matched?.revision ?? null;
    setStatus({ kind: "saving" });
    setPending(null);
    try {
      const res = await saveProtectedPosition(key, {
        protected_quantity: draft.quantity,
        reason: draft.reason,
        expected_revision: expectedRevision,
        idempotency_key: newIdempotencyKey(),
        confirm_protection_change: false,
      });
      setStatus({ kind: "saved", revision: savedRevision(res) });
      await load();
    } catch (err) {
      if (
        err instanceof ProtectedPositionSaveError &&
        err.error === "confirm_required" &&
        err.preview
      ) {
        setPending({
          key,
          quantity: draft.quantity,
          reason: draft.reason,
          expectedRevision,
          preview: err.preview,
        });
        setStatus({ kind: "idle" });
        return;
      }
      if (err instanceof ProtectedPositionSaveError && err.error === "stale_form") {
        setStatus({ kind: "stale" });
        return;
      }
      setStatus({
        kind: "error",
        message: err instanceof Error ? err.message : "미리보기를 만들지 못했습니다",
      });
    }
  }

  async function confirmSave() {
    if (!pending || saving) return;
    setStatus({ kind: "saving" });
    try {
      const res = await saveProtectedPosition(pending.key, {
        protected_quantity: pending.quantity,
        reason: pending.reason,
        expected_revision: pending.expectedRevision,
        idempotency_key: newIdempotencyKey(),
        confirm_protection_change: true,
        confirm_symbol: confirmSymbol.trim() || undefined,
      });
      setPending(null);
      setConfirmSymbol("");
      setStatus({ kind: "saved", revision: savedRevision(res) });
      await load();
    } catch (err) {
      setPending(null);
      if (err instanceof ProtectedPositionSaveError && err.error === "stale_form") {
        setStatus({ kind: "stale" });
        return;
      }
      setStatus({
        kind: "error",
        message: err instanceof Error ? err.message : "저장하지 못했습니다",
      });
    }
  }

  return (
    <section className="trader-panel" data-testid="panel-protected-quantity">
      <header className="trader-panel-head">
        <h2>보호 수량</h2>
      </header>
      {loadError ? <p className="trader-error">조회 실패: {loadError}</p> : null}
      {data && !data.can_edit ? (
        <p className="trader-dim">읽기 전용입니다. 보호 수량 변경 권한이 없습니다.</p>
      ) : null}
      <div className="trader-pq-form">
        <label>
          계좌
          <select
            data-testid="pq-scope"
            value={scope}
            onChange={(e) => setScope(e.target.value as Scope)}
          >
            <option value="kis_live">kis_live</option>
            <option value="toss_live">toss_live</option>
            <option value="upbit_live">upbit_live</option>
          </select>
        </label>
        <label>
          시장
          <select
            data-testid="pq-market"
            value={market}
            onChange={(e) => setMarket(e.target.value as Market)}
          >
            <option value="kr">kr</option>
            <option value="us">us</option>
            <option value="crypto">crypto</option>
          </select>
        </label>
        <label>
          종목
          <input
            data-testid="pq-symbol"
            value={symbol}
            onChange={(e) => setSymbol(e.target.value)}
            autoComplete="off"
          />
        </label>
        <label>
          보호 수량
          <input
            data-testid="pq-quantity"
            value={quantity}
            onChange={(e) => setQuantity(e.target.value)}
            inputMode="decimal"
          />
        </label>
        <label>
          변경 사유
          <input
            data-testid="pq-reason"
            value={reason}
            onChange={(e) => setReason(e.target.value)}
          />
        </label>
      </div>
      <div className="trader-pq-current" data-testid="pq-current">
        {matched ? (
          <span>
            P {matched.protected_quantity} · H {matched.broker_held ?? "-"} · S{" "}
            {matched.broker_sellable ?? "-"} · {matched.mode} · rev {matched.revision ?? "-"}
            {matched.latest_revision ? ` · 기록 ${matched.latest_revision.recorded_at}` : ""}
          </span>
        ) : (
          <span>선언 없음</span>
        )}
      </div>
      <button
        type="button"
        data-testid="pq-submit"
        disabled={submitDisabled}
        onClick={() => void requestPreview()}
      >
        변경 미리보기
      </button>
      {pending ? (
        <div className="trader-pq-preview" data-testid="pq-preview">
          <p>
            P {pending.preview.before_protected_quantity} →{" "}
            {pending.preview.after_protected_quantity} · H {pending.preview.broker_held} · S{" "}
            {pending.preview.broker_sellable} · 전술 {pending.preview.before_headroom} →{" "}
            {pending.preview.headroom} · {pending.preview.state}
          </p>
          <label>
            종목코드 재입력
            <input
              data-testid="pq-confirm-symbol"
              value={confirmSymbol}
              onChange={(e) => setConfirmSymbol(e.target.value)}
              autoComplete="off"
            />
          </label>
          <button
            type="button"
            data-testid="pq-confirm"
            disabled={saving}
            onClick={() => void confirmSave()}
          >
            보호 수량 저장
          </button>
        </div>
      ) : null}
      {status.kind === "saved" ? (
        <p role="status">
          저장했습니다{status.revision !== null ? ` · 개정 ${status.revision}` : ""}
        </p>
      ) : null}
      {status.kind === "stale" ? (
        <p className="trader-error" role="alert">
          다른 변경으로 양식이 오래되었습니다. 목록을 새로 불러온 뒤 다시 시도하세요.{" "}
          <button type="button" onClick={() => void load()}>
            다시 불러오기
          </button>
        </p>
      ) : null}
      {status.kind === "error" ? (
        <p className="trader-error" role="alert">
          {status.message}
        </p>
      ) : null}
    </section>
  );
}
