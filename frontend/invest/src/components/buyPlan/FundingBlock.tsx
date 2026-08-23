// 자금 대조 블록 — §144차 요구사항 ②.
//
// This is the block the operator acts on ("돈을 옮겨두지"), so it leads with
// the verdict and explains it underneath.
//
// Rows are one per (broker, currency), never per currency alone: KIS KRW
// cannot fill an Upbit order, and totalling them let an empty Upbit account
// read as 리저브 충분 off KIS cash (verify-r1 B1). Requirements whose owning
// account could not be resolved get their own block and suspend any verdict
// they could break.
import { Card, Pill } from "../../ds";
import type {
  BuyPlanFunding,
  ScopeReconciliation,
  UnattributedRequirement,
} from "../../types/buyPlan";
import { exact, money } from "./format";
import { FUNDING_BROKER_LABEL, FUNDING_VERDICT_LABEL } from "./labels";

function verdictTone(row: ScopeReconciliation): "gain" | "warn" | "paper" {
  if (row.verdict === "sufficient") return "gain";
  if (row.verdict === "shortfall") return "warn";
  return "paper";
}

function verdictLine(row: ScopeReconciliation): { text: string; title?: string } {
  if (row.verdict === "shortfall") {
    // With an incomplete requirement side the deficit is still proven, but it
    // is a floor — more may be missing. Saying so is the difference between a
    // number the operator can act on and one they can rely on (verify-r2
    // SHOULD-4).
    const prefix = row.requirements_complete ? "" : "최소 ";
    return {
      text: `${prefix}${money(row.shortfall, row.currency)} 입금 필요`,
      title: exact(row.shortfall),
    };
  }
  if (row.verdict === "sufficient") return { text: "리저브 충분" };
  if (row.broker === "unattributed") {
    return { text: "어느 계좌에서 나갈지 확정하지 못해 대조할 수 없습니다" };
  }
  if (row.available_cash === null) {
    return { text: "가용 현금을 확정하지 못해 대조를 보류했습니다" };
  }
  if (row.unattributed_same_currency !== "0") {
    return { text: "귀속 미확정 소요가 있어 대조를 보류했습니다" };
  }
  return { text: "소요액을 확정하지 못해 대조를 보류했습니다" };
}

function BreakdownRow({
  label,
  value,
  title,
}: Readonly<{ label: string; value: string; title?: string }>) {
  return (
    <div style={{ display: "flex", justifyContent: "space-between", gap: 12 }}>
      <span style={{ color: "var(--fg-2)" }}>{label}</span>
      <span style={{ fontVariantNumeric: "tabular-nums" }} title={title}>
        {value}
      </span>
    </div>
  );
}

function ReasonList({ reasons }: Readonly<{ reasons: string[] }>) {
  if (reasons.length === 0) return null;
  return (
    <ul
      style={{
        margin: 0,
        paddingLeft: 16,
        fontSize: 11,
        color: "var(--fg-2)",
        display: "grid",
        gap: 2,
      }}
    >
      {reasons.map((reason) => (
        <li key={reason}>{reason}</li>
      ))}
    </ul>
  );
}

function ScopeCard({ row }: Readonly<{ row: ScopeReconciliation }>) {
  const verdict = verdictLine(row);
  return (
    <Card soft style={{ padding: 16, display: "grid", gap: 10 }}>
      <div style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
        <strong style={{ fontSize: 15 }}>
          {row.broker === "unattributed"
            ? `목적지 미확정 · ${row.currency}`
            : `${FUNDING_BROKER_LABEL[row.broker]} · ${row.currency}`}
        </strong>
        <Pill tone={verdictTone(row)} size="sm">
          {FUNDING_VERDICT_LABEL[row.verdict]}
        </Pill>
        {!row.requirements_complete && (
          <Pill tone="warn" size="sm">소요액 불완전</Pill>
        )}
      </div>
      <div style={{ fontSize: 18, fontWeight: 700 }} title={verdict.title}>
        {verdict.text}
      </div>
      {row.verdict === "shortfall" && !row.requirements_complete && (
        <p style={{ margin: 0, fontSize: 11, color: "var(--warn)" }}>
          소요액 일부를 확인하지 못했습니다 — 이 금액은 하한이며 실제로는 더 필요할
          수 있습니다.
        </p>
      )}
      <div style={{ display: "grid", gap: 4, fontSize: 12 }}>
        <BreakdownRow
          label="가용 현금 (이 계좌)"
          value={money(row.available_cash, row.currency)}
          title={exact(row.available_cash)}
        />
        <BreakdownRow
          label="이 계좌 소요 합계"
          value={money(row.required_total, row.currency)}
          title={exact(row.required_total)}
        />
        <div style={{ height: 1, background: "var(--border)", margin: "2px 0" }} />
        <BreakdownRow
          label="· 물타기 A(k)"
          value={money(row.required_averaging_adds, row.currency)}
          title={exact(row.required_averaging_adds)}
        />
        <BreakdownRow
          label="· 지지 그물 (워치형)"
          value={money(row.required_support_net, row.currency)}
          title={exact(row.required_support_net)}
        />
        <BreakdownRow
          label="· 그 밖의 활성 매수 워치"
          value={money(row.required_active_watches, row.currency)}
          title={exact(row.required_active_watches)}
        />
        {row.unattributed_same_currency !== "0" && (
          <BreakdownRow
            label="· 계좌 미확정 (최악의 경우 여기로)"
            value={money(row.unattributed_same_currency, row.currency)}
            title={exact(row.unattributed_same_currency)}
          />
        )}
      </div>
      <ReasonList reasons={row.incomplete_reasons} />
      <ReasonList reasons={row.notes} />
    </Card>
  );
}

function UnattributedBlock({
  rows,
}: Readonly<{ rows: UnattributedRequirement[] }>) {
  if (rows.length === 0) return null;
  return (
    <Card style={{ padding: 16, display: "grid", gap: 8 }}>
      <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
        <Pill tone="warn" size="sm">계좌 미확정</Pill>
        <strong style={{ fontSize: 13 }}>
          어느 계좌에서 나갈 돈인지 확정하지 못한 소요액
        </strong>
      </div>
      <p style={{ margin: 0, fontSize: 11, color: "var(--fg-2)" }}>
        합계에서 빼지 않았습니다. 이 돈은 어느 브로커로도 갈 수 있으므로, 같은 통화의
        모든 계좌 판정을 보류시키고 위에 별도의 &quot;목적지 미확정&quot; 행으로도
        올립니다.
      </p>
      <div style={{ display: "grid", gap: 4, fontSize: 12 }}>
        {rows.map((row) => (
          <div
            key={`${row.kind}:${row.label}`}
            style={{ display: "flex", justifyContent: "space-between", gap: 12 }}
          >
            <span title={row.reason}>{row.label}</span>
            <span
              style={{ fontVariantNumeric: "tabular-nums" }}
              title={exact(row.amount)}
            >
              {money(row.amount, row.currency)}
            </span>
          </div>
        ))}
      </div>
    </Card>
  );
}

export function FundingBlock({ funding }: Readonly<{ funding: BuyPlanFunding }>) {
  const included = funding.accounts.filter((a) => a.included_in_reserve);
  return (
    <section style={{ display: "grid", gap: 12 }}>
      <h2 style={{ margin: 0, fontSize: 16 }}>자금 대조</h2>
      {funding.source_warnings.length > 0 && (
        <Card soft style={{ padding: 12 }}>
          <div style={{ fontSize: 12, fontWeight: 700, marginBottom: 4 }}>
            일부 소스가 불완전합니다 — 아래 숫자는 그만큼 덜 본 값입니다
          </div>
          <ReasonList reasons={funding.source_warnings} />
        </Card>
      )}
      <div
        style={{
          display: "grid",
          gap: 12,
          gridTemplateColumns: "repeat(auto-fit, minmax(280px, 1fr))",
        }}
      >
        {funding.scopes.map((row) => (
          <ScopeCard key={row.scope_key} row={row} />
        ))}
      </div>
      <UnattributedBlock rows={funding.unattributed} />
      {included.length > 0 && (
        <details>
          <summary style={{ cursor: "pointer", fontSize: 12, color: "var(--fg-2)" }}>
            계좌별 가용 현금 ({included.length})
          </summary>
          <div style={{ display: "grid", gap: 4, marginTop: 8, fontSize: 12 }}>
            {included.map((account) => (
              <div
                key={`${account.account_id}:${account.currency}`}
                style={{ display: "flex", justifyContent: "space-between", gap: 12 }}
              >
                <span>
                  {account.display_name}{" "}
                  <span style={{ color: "var(--fg-2)" }}>
                    ({account.source} · {account.available_cash_source})
                  </span>
                </span>
                <span
                  style={{ fontVariantNumeric: "tabular-nums" }}
                  title={exact(account.available_cash) ?? "확인 불가"}
                >
                  {money(account.available_cash, account.currency)}
                </span>
              </div>
            ))}
          </div>
        </details>
      )}
    </section>
  );
}
