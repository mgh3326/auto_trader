// /invest/settings/protected-positions — #728 operator-only declarations.
import { useCallback, useEffect, useMemo, useRef, useState, type FormEvent } from "react";
import {
  fetchProtectedPositions,
  ProtectedPositionSaveError,
  saveProtectedPosition,
  type ProtectedPositionView,
  type ProtectedPositionsResponse,
  type ProtectionPreview,
} from "../api/protectedPositions";
import { DesktopShell } from "../desktop/DesktopShell";
import { Button, Card, Pill } from "../ds";
import { useViewport } from "../hooks/useViewport";
import { MobileShell } from "../mobile/MobileShell";
import "../styles/funding.css";
import "../styles/protectedPositions.css";

interface Draft {
  quantity: string;
  reason: string;
  reconfirm: boolean;
}

interface Confirming {
  position: ProtectedPositionView;
  draft: Draft;
  preview: ProtectionPreview;
}

type SaveState =
  | { kind: "idle" }
  | { kind: "saving" }
  | { kind: "saved" }
  | { kind: "stale" }
  | { kind: "error"; message: string };

function rowKey(position: Pick<ProtectedPositionView, "account_scope" | "market" | "symbol">): string {
  return `${position.account_scope}:${position.market}:${position.symbol}`;
}

function newIdempotencyKey(): string {
  if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") return crypto.randomUUID();
  return `protected-position-${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

function decimalDisplay(value: string | null): string {
  return value == null ? "검증 필요" : value;
}

function stateLabel(state: ProtectedPositionView["state"]): string {
  return {
    unprotected: "미보호",
    covered: "보호 정상",
    encroached: "보호 침범",
    shortfall: "보유 부족",
    unverified: "검증 불가",
  }[state];
}

function warningState(state: ProtectedPositionView["state"]): boolean {
  return state === "encroached" || state === "shortfall" || state === "unverified";
}

function isDecreaseOrRelease(preview: ProtectionPreview): boolean {
  return compareNonNegativeDecimalStrings(
    preview.after_protected_quantity,
    preview.before_protected_quantity,
  ) < 0;
}

function compareNonNegativeDecimalStrings(left: string, right: string): number {
  const normalize = (value: string): [string, string] => {
    const [whole = "0", fraction = ""] = value.trim().split(".", 2);
    const normalizedWhole = whole.replace(/^0+(?=\d)/, "") || "0";
    return [normalizedWhole, fraction.replace(/0+$/, "")];
  };
  const [leftWhole, leftFraction] = normalize(left);
  const [rightWhole, rightFraction] = normalize(right);
  if (leftWhole.length !== rightWhole.length) {
    return leftWhole.length < rightWhole.length ? -1 : 1;
  }
  if (leftWhole !== rightWhole) return leftWhole < rightWhole ? -1 : 1;
  const width = Math.max(leftFraction.length, rightFraction.length);
  const paddedLeft = leftFraction.padEnd(width, "0");
  const paddedRight = rightFraction.padEnd(width, "0");
  if (paddedLeft === paddedRight) return 0;
  return paddedLeft < paddedRight ? -1 : 1;
}

function initialDrafts(data: ProtectedPositionsResponse): Record<string, Draft> {
  return Object.fromEntries(
    data.positions.map((position) => [
      rowKey(position),
      { quantity: position.protected_quantity, reason: "", reconfirm: false },
    ]),
  );
}

function ConfirmDialog({
  confirming,
  saving,
  onCancel,
  onConfirm,
}: {
  confirming: Confirming;
  saving: boolean;
  onCancel: () => void;
  onConfirm: (confirmSymbol: string) => void;
}) {
  const [confirmSymbol, setConfirmSymbol] = useState("");
  const decrease = isDecreaseOrRelease(confirming.preview);
  const dialogRef = useRef<HTMLDivElement>(null);
  const [opener] = useState(() => document.activeElement instanceof HTMLElement ? document.activeElement : null);
  useEffect(() => () => opener?.focus(), [opener]);
  const beforeTactical = confirming.preview.before_headroom;
  const afterTactical = confirming.preview.headroom;
  const canConfirm = !decrease || confirmSymbol.trim().toUpperCase() === confirming.position.symbol.toUpperCase();

  return (
    <div className="protected-position-dialog__backdrop">
      <div
        ref={dialogRef}
        className="protected-position-dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby="protected-position-confirm-title"
        data-testid="protected-position-confirm-dialog"
      >
        <h2 id="protected-position-confirm-title">보호 수량 변경을 확인하세요</h2>
        <p className="protected-position-dialog__tactical" data-testid="tactical-sellable-change">
          전술 매도 가능 수량이 {beforeTactical} → {afterTactical} 로 바뀝니다
        </p>
        <dl>
          <div><dt>보호 수량</dt><dd>{confirming.preview.before_protected_quantity} → {confirming.preview.after_protected_quantity}</dd></div>
          <div><dt>신선 보유 H</dt><dd>{confirming.preview.broker_held}</dd></div>
          <div><dt>신선 주문가능 S</dt><dd>{confirming.preview.broker_sellable}</dd></div>
        </dl>
        {decrease ? (
          <label className="protected-position-dialog__symbol-confirm">
            <span>감소 또는 해제를 확인하려면 종목코드 {confirming.position.symbol}을 정확히 다시 입력하세요</span>
            <input
              value={confirmSymbol}
              onChange={(event) => setConfirmSymbol(event.target.value)}
              aria-label="감소 보호 종목코드 재입력"
              autoComplete="off"
            />
          </label>
        ) : null}
        <div className="protected-position-dialog__actions">
          <Button autoFocus type="button" variant="secondary" onClick={onCancel} disabled={saving}>
            취소 · 다시 확인
          </Button>
          <Button
            type="button"
            variant={decrease ? "danger" : "primary"}
            onClick={() => onConfirm(confirmSymbol)}
            disabled={saving || !canConfirm}
            data-testid="protected-position-confirm-save"
          >
            {saving ? "저장 중…" : "변경 확정"}
          </Button>
        </div>
      </div>
    </div>
  );
}

export function ProtectedPositionsSettingsContent({
  data,
  onReload,
}: {
  data: ProtectedPositionsResponse;
  onReload: () => Promise<void>;
}) {
  const [drafts, setDrafts] = useState<Record<string, Draft>>(() => initialDrafts(data));
  const [confirming, setConfirming] = useState<Confirming | null>(null);
  const [saveState, setSaveState] = useState<Record<string, SaveState>>({});

  useEffect(() => setDrafts(initialDrafts(data)), [data]);
  const anyNonEnforcing = useMemo(
    () => data.positions.some((position) => position.mode === "off" || position.mode === "shadow"),
    [data.positions],
  );

  function patchDraft(position: ProtectedPositionView, patch: Partial<Draft>) {
    const key = rowKey(position);
    setDrafts((previous) => ({ ...previous, [key]: { ...previous[key]!, ...patch } }));
    setSaveState((previous) => ({ ...previous, [key]: { kind: "idle" } }));
  }

  async function requestPreview(position: ProtectedPositionView, draft: Draft) {
    const key = rowKey(position);
    setSaveState((previous) => ({ ...previous, [key]: { kind: "saving" } }));
    try {
      await saveProtectedPosition(position, {
        protected_quantity: draft.quantity,
        reason: draft.reason,
        expected_revision: position.revision,
        idempotency_key: newIdempotencyKey(),
        confirm_protection_change: false,
        reconfirm: draft.reconfirm,
      });
    } catch (caught) {
      if (caught instanceof ProtectedPositionSaveError && caught.error === "confirm_required" && caught.preview) {
        setConfirming({ position, draft, preview: caught.preview });
        setSaveState((previous) => ({ ...previous, [key]: { kind: "idle" } }));
        return;
      }
      if (caught instanceof ProtectedPositionSaveError && caught.error === "stale_form") {
        setSaveState((previous) => ({ ...previous, [key]: { kind: "stale" } }));
        return;
      }
      setSaveState((previous) => ({
        ...previous,
        [key]: { kind: "error", message: caught instanceof Error ? caught.message : "미리보기를 만들지 못했습니다" },
      }));
    }
  }

  async function confirmSave(confirmSymbol: string) {
    if (!confirming) return;
    const { position, draft } = confirming;
    const key = rowKey(position);
    setSaveState((previous) => ({ ...previous, [key]: { kind: "saving" } }));
    try {
      await saveProtectedPosition(position, {
        protected_quantity: draft.quantity,
        reason: draft.reason,
        expected_revision: position.revision,
        idempotency_key: newIdempotencyKey(),
        confirm_protection_change: true,
        confirm_symbol: confirmSymbol || undefined,
        reconfirm: draft.reconfirm,
      });
      setConfirming(null);
      setSaveState((previous) => ({ ...previous, [key]: { kind: "saved" } }));
      await onReload();
    } catch (caught) {
      setConfirming(null);
      setSaveState((previous) => ({
        ...previous,
        [key]: {
          kind: caught instanceof ProtectedPositionSaveError && caught.error === "stale_form" ? "stale" : "error",
          ...(caught instanceof ProtectedPositionSaveError && caught.error === "stale_form"
            ? {}
            : { message: caught instanceof Error ? caught.message : "저장하지 못했습니다" }),
        } as SaveState,
      }));
    }
  }

  return (
    <div className="funding-page protected-positions-page">
      <header className="funding-page__header">
        <div>
          <div className="funding-eyebrow">SETTINGS · LONG-TERM PROTECTION</div>
          <h1>장기 보호 수량</h1>
          <p>보유하고 남길 장기 수량 P를 기록합니다. 이 화면은 브로커 주문을 보내지 않으며, 변경 때마다 신선한 보유 H와 주문가능 S를 다시 확인합니다.</p>
        </div>
      </header>
      {anyNonEnforcing ? (
        <div className="protected-position-banner" role="status" data-testid="protection-not-enforced-banner">
          <strong>보호 미강제 — 표시·기록만</strong> 현재 행 중 하나 이상이 off 또는 shadow 모드입니다. 화면의 전술 수량은 주문 전송 권한이 아닙니다.
        </div>
      ) : null}
      {!data.can_edit ? <p className="protected-position-readonly" role="status">읽기 전용입니다. 관리자만 보호 수량을 변경할 수 있습니다.</p> : null}
      <div className="protected-position-list">
        {data.positions.map((position) => {
          const key = rowKey(position);
          const draft = drafts[key] ?? { quantity: position.protected_quantity, reason: "", reconfirm: false };
          const state = saveState[key] ?? { kind: "idle" };
          return (
            <Card key={key}>
              <article className="protected-position-row" data-testid="protected-position-row">
                <header>
                  <div>
                    <h2>{position.name || position.symbol}</h2>
                    <p>{position.account_scope} · {position.market} · {position.symbol}</p>
                  </div>
                  <div className="protected-position-pills">
                    <Pill tone={warningState(position.state) ? "warn" : "accent"} size="sm">{stateLabel(position.state)}</Pill>
                    <Pill tone={position.mode === "enforce" ? "accent" : "paper"} size="sm">{position.mode}</Pill>
                  </div>
                </header>
                {(position.mode === "off" || position.mode === "shadow") ? (
                  <div className="protected-position-row__banner" role="status">보호 미강제 — 표시·기록만</div>
                ) : null}
                <dl className="protected-position-metrics">
                  <div><dt>보호 P</dt><dd>{position.protected_quantity}</dd></div>
                  <div><dt>보유 H</dt><dd>{decimalDisplay(position.broker_held)}</dd></div>
                  <div><dt>주문가능 S</dt><dd>{decimalDisplay(position.broker_sellable)}</dd></div>
                  <div><dt>전술 매도 가능</dt><dd>{decimalDisplay(position.headroom)}</dd></div>
                </dl>
                {position.read_error ? <p className="protected-position-error" role="alert">브로커 확인 불가: 보호 0으로 표시하지 않았습니다. 다시 시도하세요.</p> : null}
                {position.latest_revision ? <p className="protected-position-revision">최근 개정 {position.latest_revision.revision} · {position.latest_revision.actor ?? `사용자 ${position.latest_revision.actor_user_id}`} · {position.latest_revision.recorded_at} · {position.latest_revision.reason}</p> : null}
                <a href={position.history_url} className="protected-position-history">개정 이력 보기</a>
                {data.can_edit ? (
                  <form
                    className="protected-position-form"
                    onSubmit={(event: FormEvent<HTMLFormElement>) => {
                      event.preventDefault();
                      void requestPreview(position, draft);
                    }}
                    noValidate
                  >
                    <label>
                      <span>보호 수량 P</span>
                      <input value={draft.quantity} onChange={(event) => patchDraft(position, { quantity: event.target.value })} inputMode="decimal" aria-label={`${position.symbol} 보호 수량`} disabled={state.kind === "saving"} required />
                    </label>
                    <label>
                      <span>변경 사유</span>
                      <input value={draft.reason} onChange={(event) => patchDraft(position, { reason: event.target.value })} aria-label={`${position.symbol} 변경 사유`} disabled={state.kind === "saving"} required />
                    </label>
                    <label className="protected-position-reconfirm">
                      <input type="checkbox" checked={draft.reconfirm} onChange={(event) => patchDraft(position, { reconfirm: event.target.checked })} />
                      같은 수량을 새 증거로 재확인
                    </label>
                    <Button type="submit" disabled={state.kind === "saving"} data-testid="protected-position-preview">변경 미리보기</Button>
                  </form>
                ) : null}
                {state.kind === "saved" ? <p role="status">저장했습니다. 최신 보호 상태를 다시 불러왔습니다.</p> : null}
                {state.kind === "stale" ? (
                  <p className="protected-position-error" role="alert">
                    다른 변경으로 양식이 오래되었습니다. 최신 상태를 새로고침한 뒤 다시 시도하세요. {" "}
                    <Button type="button" variant="secondary" size="sm" onClick={() => void onReload()}>최신 상태 다시 불러오기</Button>
                  </p>
                ) : null}
                {state.kind === "error" ? (
                  <p className="protected-position-error" role="alert">
                    {state.message} <Button type="button" variant="secondary" size="sm" onClick={() => void onReload()}>다시 시도</Button>
                  </p>
                ) : null}
              </article>
            </Card>
          );
        })}
      </div>
      {confirming ? <ConfirmDialog confirming={confirming} saving={(saveState[rowKey(confirming.position)] ?? { kind: "idle" }).kind === "saving"} onCancel={() => setConfirming(null)} onConfirm={(symbol) => void confirmSave(symbol)} /> : null}
    </div>
  );
}

function ProtectedPositionsSettingsLoader() {
  const [data, setData] = useState<ProtectedPositionsResponse | null>(null);
  const [error, setError] = useState(false);
  const load = useCallback(async (signal?: AbortSignal) => {
    setError(false);
    try {
      setData(await fetchProtectedPositions(signal));
    } catch (caught) {
      if (!(caught instanceof DOMException && caught.name === "AbortError")) setError(true);
    }
  }, []);
  useEffect(() => {
    const controller = new AbortController();
    void load(controller.signal);
    return () => controller.abort();
  }, [load]);
  if (error) return <div className="funding-load-state" role="alert">보호 설정을 불러오지 못했습니다. <Button variant="secondary" size="sm" onClick={() => void load()}>다시 시도</Button></div>;
  if (!data) return <div className="funding-load-state" role="status">보호 설정을 불러오는 중…</div>;
  return <ProtectedPositionsSettingsContent data={data} onReload={() => load()} />;
}

export function ProtectedPositionsSettingsRoute() {
  const viewport = useViewport();
  const content = <ProtectedPositionsSettingsLoader />;
  return viewport === "mobile" ? <MobileShell title="장기 보호 설정"><div className="funding-mobile-wrap">{content}</div></MobileShell> : <DesktopShell center={content} />;
}
