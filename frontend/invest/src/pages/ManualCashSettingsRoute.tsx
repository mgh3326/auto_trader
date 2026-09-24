// /invest/settings/manual-cash — operator-entered parking balances (#671).
//
// The saved total is user_settings.manual_cash, the parking term of the
// deployment-cap denominator (app/services/deployment_cap.py). Values come
// from operator input only: rows start from the operator's own last saved
// breakdown, never from a broker balance or an estimate.
import { useCallback, useEffect, useMemo, useState, type FormEvent } from "react";
import {
  fetchManualCash,
  ManualCashSaveError,
  saveManualCash,
  type ManualCashResponse,
  type ManualCashView,
} from "../api/manualCash";
import { DesktopShell } from "../desktop/DesktopShell";
import { Button, Card, Pill } from "../ds";
import { formatRelativeTime } from "../format/relativeTime";
import { useViewport } from "../hooks/useViewport";
import {
  changeRatio,
  formatKrwAmount,
  parseKrwInput,
  requiresLargeChangeConfirm,
} from "../manualCashRules";
import { MobileShell } from "../mobile/MobileShell";
import "../styles/funding.css";
import "../styles/manualCash.css";

interface Row {
  id: number;
  name: string;
  amountText: string;
}

type SaveState =
  | { kind: "idle" }
  | { kind: "saving" }
  | { kind: "saved" }
  | { kind: "error"; message: string }
  | { kind: "stale_form" };

let nextRowId = 1;
function newRow(name = "", amountText = ""): Row {
  nextRowId += 1;
  return { id: nextRowId, name, amountText };
}

function rowsFrom(view: ManualCashView): Row[] {
  if (view.accounts.length) return view.accounts.map((a) => newRow(a.name, String(a.amount)));
  return [newRow()];
}

function formatTime(value: string | null): string {
  if (!value) return "—";
  const parsed = new Date(value);
  return Number.isNaN(parsed.valueOf()) ? value : parsed.toLocaleString("ko-KR");
}

function formatPct(ratio: number): string {
  return `${(ratio * 100).toFixed(1)}%`;
}

function currentAmount(view: ManualCashView): number | null {
  return view.present && view.amount_valid ? view.amount : null;
}

function StaleStatus({ view }: { view: ManualCashView }) {
  const hours = view.stale_after_hours;
  if (!view.present) {
    return (
      <div className="manual-cash-banner manual-cash-banner--danger" role="alert" data-testid="manual-cash-missing">
        <strong>저장된 값이 없습니다.</strong> deployment_cap 파킹 항은 0으로 계산됩니다
        (<code>absent_treated_as_zero</code>).
      </div>
    );
  }
  if (!view.amount_valid) {
    return (
      <div className="manual-cash-banner manual-cash-banner--danger" role="alert" data-testid="manual-cash-invalid">
        <strong>저장된 금액을 읽을 수 없습니다.</strong> 가용자금 합계에서 제외되고 deployment_cap 파킹 항은 0입니다.
        새로 입력해 저장하세요.
      </div>
    );
  }
  if (view.stale) {
    return (
      <div className="manual-cash-banner manual-cash-banner--danger" role="alert" data-testid="manual-cash-stale">
        <strong>stale — 현재 0으로 반영 중.</strong> 마지막 저장({formatTime(view.updated_at)})이 {hours}시간을
        넘었습니다. 가용자금 합계(<code>total_orderable_krw</code>)에서 제외되고 deployment_cap 파킹 항은 0입니다
        (<code>stale_treated_as_zero</code>). 잔고를 확인해 다시 저장하면 즉시 반영됩니다.
      </div>
    );
  }
  return (
    <div className="manual-cash-banner" role="status" data-testid="manual-cash-fresh">
      <strong>반영 중.</strong> {formatTime(view.stale_at)}에 stale이 됩니다 — 저장 후 {hours}시간(3일)이 지나면
      가용자금 합계에서 빠지고 deployment_cap 파킹 항이 0이 됩니다.
      {view.amount === 0 ? " (현재 값이 0원이라 파킹 항은 이미 0입니다.)" : null}
    </div>
  );
}

function ConfirmDialog({
  current,
  next,
  onConfirm,
  onCancel,
  busy,
}: {
  current: number | null;
  next: number;
  onConfirm: () => void;
  onCancel: () => void;
  busy: boolean;
}) {
  const ratio = changeRatio(current, next);
  return (
    <div className="manual-cash-dialog__backdrop">
      <div
        className="manual-cash-dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby="manual-cash-confirm-title"
        data-testid="manual-cash-confirm-dialog"
        onKeyDown={(event) => {
          if (event.key === "Escape") onCancel();
        }}
      >
        <h2 id="manual-cash-confirm-title">큰 변경을 확인하세요</h2>
        <p>
          파킹 합계가 50%를 넘게 바뀝니다. 0이 하나 더 붙거나 빠진 오타가 아닌지 확인하세요. 이 값은
          deployment_cap(동적 매수 한도)의 분모에 들어갑니다.
        </p>
        <dl className="manual-cash-dialog__numbers">
          <div>
            <dt>현재 저장값</dt>
            <dd data-testid="manual-cash-confirm-current">{current === null ? "없음" : formatKrwAmount(current)}</dd>
          </div>
          <div>
            <dt>새 합계</dt>
            <dd data-testid="manual-cash-confirm-next">{formatKrwAmount(next)}</dd>
          </div>
          <div>
            <dt>변화율</dt>
            <dd data-testid="manual-cash-confirm-ratio">
              {ratio === null ? "기준값 없음" : `${next >= (current ?? 0) ? "+" : "−"}${formatPct(ratio)}`}
            </dd>
          </div>
        </dl>
        <div className="manual-cash-dialog__actions">
          <Button type="button" variant="secondary" onClick={onCancel} disabled={busy}>
            취소 · 다시 확인
          </Button>
          <Button
            autoFocus
            type="button"
            variant="danger"
            onClick={onConfirm}
            disabled={busy}
            data-testid="manual-cash-confirm-save"
          >
            {formatKrwAmount(next)}로 저장
          </Button>
        </div>
      </div>
    </div>
  );
}

export function ManualCashSettingsContent({
  data,
  onSaved,
}: {
  data: ManualCashResponse;
  onSaved: (next: ManualCashResponse) => void;
}) {
  const view = data.manual_cash;
  const limits = data.limits;
  const canEdit = data.can_edit;
  const [rows, setRows] = useState<Row[]>(() => rowsFrom(view));
  const [saveState, setSaveState] = useState<SaveState>({ kind: "idle" });
  const [confirmOpen, setConfirmOpen] = useState(false);

  useEffect(() => {
    setRows(rowsFrom(view));
  }, [view]);

  const parsed = useMemo(
    () =>
      rows.map((row) => ({
        row,
        nameError: row.name.trim() ? (row.name.trim().length > limits.name_max_len ? `이름은 ${limits.name_max_len}자 이하` : null) : "이름을 입력하세요",
        amount: parseKrwInput(row.amountText, limits.max_amount_krw),
      })),
    [rows, limits.max_amount_krw, limits.name_max_len],
  );
  const allValid = parsed.every((p) => p.nameError === null && p.amount.ok);
  const total = parsed.reduce((sum, p) => sum + (p.amount.ok ? p.amount.value : 0), 0);
  const totalOverBound = total > limits.max_amount_krw;
  const canSave = canEdit && allValid && !totalOverBound && rows.length > 0 && saveState.kind !== "saving";
  const current = currentAmount(view);

  function update(id: number, patch: Partial<Row>) {
    setRows((prev) => prev.map((row) => (row.id === id ? { ...row, ...patch } : row)));
    if (saveState.kind === "saved" || saveState.kind === "error") setSaveState({ kind: "idle" });
  }

  async function persist(confirmLargeChange: boolean) {
    setSaveState({ kind: "saving" });
    try {
      const next = await saveManualCash({
        accounts: parsed.map((p) => ({ name: p.row.name.trim(), amount: p.amount.ok ? p.amount.value : -1 })),
        expected_updated_at: view.present ? view.updated_at : null,
        confirm_large_change: confirmLargeChange,
      });
      setConfirmOpen(false);
      setSaveState({ kind: "saved" });
      onSaved(next);
    } catch (caught) {
      if (caught instanceof ManualCashSaveError && caught.error === "confirm_required") {
        setConfirmOpen(true);
        setSaveState({ kind: "idle" });
        return;
      }
      setConfirmOpen(false);
      if (caught instanceof ManualCashSaveError && caught.error === "stale_form") {
        setSaveState({ kind: "stale_form" });
        return;
      }
      setSaveState({ kind: "error", message: caught instanceof Error ? caught.message : "저장 실패" });
    }
  }

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!canSave) return;
    if (requiresLargeChangeConfirm(current, total)) {
      setConfirmOpen(true);
      return;
    }
    void persist(false);
  }

  return (
    <div className="funding-page manual-cash-page">
      <header className="funding-page__header">
        <div>
          <div className="funding-eyebrow">SETTINGS · MANUAL CASH</div>
          <h1>파킹 현금 (manual_cash)</h1>
          <p>
            API로 조회되지 않는 파킹 계좌 잔고를 운영자가 직접 입력합니다. 저장된 합계는 가용자금 합계와
            deployment_cap(동적 매수 한도) 분모의 파킹 항으로 쓰입니다. 자동 추정·잔고 불러오기는 없습니다.
          </p>
        </div>
      </header>

      <Card>
        <dl className="funding-metrics" data-testid="manual-cash-current">
          <div className="funding-metric">
            <dt>현재 저장값</dt>
            <dd data-testid="manual-cash-current-amount">
              {current === null ? (view.present ? "읽을 수 없음" : "없음") : formatKrwAmount(current)}
            </dd>
          </div>
          <div className="funding-metric">
            <dt>마지막 저장</dt>
            <dd data-testid="manual-cash-updated-at">
              {formatTime(view.updated_at)}
              {view.updated_at ? ` (${formatRelativeTime(view.updated_at) ?? "—"})` : ""}
            </dd>
          </div>
          <div className="funding-metric">
            <dt>출처</dt>
            <dd data-testid="manual-cash-source">
              {view.source === "operator_confirmed" ? (
                <Pill tone="accent" size="sm">운영자 확인</Pill>
              ) : view.present ? (
                <Pill tone="warn" size="sm">출처 표시 없음 (MCP 등)</Pill>
              ) : (
                "—"
              )}
            </dd>
          </div>
        </dl>
        <StaleStatus view={view} />
        {view.present && view.amount_valid && view.accounts.length === 0 ? (
          <p className="funding-muted manual-cash-note" data-testid="manual-cash-no-breakdown">
            현재 값에는 계좌별 내역이 없습니다(합계만 저장됨). 아래에 계좌별 금액을 입력해 저장하세요.
          </p>
        ) : null}
      </Card>

      <Card>
        <form className="manual-cash-form" onSubmit={submit} data-testid="manual-cash-form" noValidate>
          <div className="funding-section-head">
            <div>
              <h2>파킹 계좌</h2>
              <p>
                계좌마다 이름과 금액(원, 0 이상 정수)을 입력합니다. 계좌당·합계 상한{" "}
                {formatKrwAmount(limits.max_amount_krw)}. 천 단위 쉼표는 표시용입니다.
              </p>
            </div>
          </div>
          <ul className="manual-cash-rows">
            {parsed.map(({ row, nameError, amount }, index) => (
              <li key={row.id} className="manual-cash-row" data-testid="manual-cash-row">
                <label>
                  <span>계좌 이름</span>
                  <input
                    value={row.name}
                    onChange={(event) => update(row.id, { name: event.target.value })}
                    placeholder="예: 토스 파킹"
                    maxLength={limits.name_max_len + 10}
                    disabled={!canEdit}
                    aria-invalid={nameError !== null}
                    aria-label={`계좌 ${index + 1} 이름`}
                  />
                  {nameError && row.name !== "" ? <small role="alert">{nameError}</small> : null}
                </label>
                <label>
                  <span>금액 (원)</span>
                  <input
                    value={row.amountText}
                    onChange={(event) => update(row.id, { amountText: event.target.value })}
                    inputMode="numeric"
                    placeholder="0"
                    disabled={!canEdit}
                    aria-invalid={!amount.ok}
                    aria-label={`계좌 ${index + 1} 금액`}
                  />
                  {amount.ok ? (
                    <small className="manual-cash-row__preview">{formatKrwAmount(amount.value)}</small>
                  ) : row.amountText !== "" ? (
                    <small role="alert">{amount.error}</small>
                  ) : null}
                </label>
                <Button
                  type="button"
                  variant="ghost"
                  size="sm"
                  onClick={() => setRows((prev) => prev.filter((r) => r.id !== row.id))}
                  disabled={!canEdit || rows.length <= 1}
                  aria-label={`계좌 ${index + 1} 삭제`}
                >
                  삭제
                </Button>
              </li>
            ))}
          </ul>
          <Button
            type="button"
            variant="secondary"
            size="sm"
            onClick={() => setRows((prev) => [...prev, newRow()])}
            disabled={!canEdit || rows.length >= limits.max_accounts}
          >
            + 계좌 추가
          </Button>

          <div className="manual-cash-total" data-testid="manual-cash-total">
            <span>합계</span>
            <strong>{allValid ? formatKrwAmount(total) : "—"}</strong>
            {allValid && current !== null && current > 0 ? (
              <small>
                현재 대비 {total >= current ? "+" : "−"}
                {formatPct(changeRatio(current, total) ?? 0)}
              </small>
            ) : null}
          </div>
          {totalOverBound ? (
            <p role="alert" className="manual-cash-error">합계가 상한 {formatKrwAmount(limits.max_amount_krw)}을 넘습니다.</p>
          ) : null}

          {canEdit ? (
            <Button type="submit" disabled={!canSave} data-testid="manual-cash-save">
              {saveState.kind === "saving" ? "저장 중…" : "운영자 확인 후 저장"}
            </Button>
          ) : (
            <p className="funding-muted">관리자만 저장할 수 있습니다.</p>
          )}
          {saveState.kind === "saved" ? <p role="status" data-testid="manual-cash-saved">저장했습니다. 가용자금·deployment_cap에 바로 반영됩니다.</p> : null}
          {saveState.kind === "error" ? <p role="alert" className="manual-cash-error">저장하지 못했습니다: {saveState.message}</p> : null}
          {saveState.kind === "stale_form" ? (
            <p role="alert" className="manual-cash-error" data-testid="manual-cash-stale-form">
              다른 곳(MCP 등)에서 값이 바뀌었습니다. 새로고침해 최신 값을 확인한 뒤 다시 저장하세요.
            </p>
          ) : null}
        </form>
      </Card>

      {confirmOpen ? (
        <ConfirmDialog
          current={current}
          next={total}
          busy={saveState.kind === "saving"}
          onCancel={() => setConfirmOpen(false)}
          onConfirm={() => void persist(true)}
        />
      ) : null}
    </div>
  );
}

function ManualCashSettingsLoader() {
  const [data, setData] = useState<ManualCashResponse | null>(null);
  const [error, setError] = useState(false);

  const load = useCallback(async (signal?: AbortSignal) => {
    setError(false);
    try {
      setData(await fetchManualCash(signal));
    } catch (caught) {
      if (!(caught instanceof DOMException && caught.name === "AbortError")) setError(true);
    }
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    void load(controller.signal);
    return () => controller.abort();
  }, [load]);

  if (error) return <div className="funding-load-state" role="alert">manual_cash 설정을 불러오지 못했습니다.</div>;
  if (!data) return <div className="funding-load-state" role="status">불러오는 중…</div>;
  return <ManualCashSettingsContent data={data} onSaved={setData} />;
}

export function ManualCashSettingsRoute() {
  const viewport = useViewport();
  const content = <ManualCashSettingsLoader />;
  return viewport === "mobile" ? (
    <MobileShell title="파킹 현금 설정">
      <div className="funding-mobile-wrap">{content}</div>
    </MobileShell>
  ) : (
    <DesktopShell center={content} />
  );
}
