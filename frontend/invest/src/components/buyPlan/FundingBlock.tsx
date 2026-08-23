// 자금 대조 블록 — §144차 요구사항 ②.
//
// This is the block the operator actually acts on ("돈을 옮겨두지"), so the
// verdict line comes first and the breakdown explains it underneath.
import { Card, Pill } from "../../ds";
import type { BuyPlanFunding, CurrencyReconciliation } from "../../types/buyPlan";
import { money } from "./format";
import { FUNDING_VERDICT_LABEL } from "./labels";

function verdictTone(row: CurrencyReconciliation): "gain" | "warn" | "paper" {
  if (row.verdict === "sufficient") return "gain";
  if (row.verdict === "shortfall") return "warn";
  return "paper";
}

function verdictLine(row: CurrencyReconciliation): string {
  if (row.verdict === "shortfall") {
    return `${money(row.shortfall, row.currency)} 입금 필요`;
  }
  if (row.verdict === "sufficient") return "리저브 충분";
  return "가용 현금을 확정하지 못해 대조를 보류했습니다";
}

function BreakdownRow({
  label,
  value,
}: Readonly<{ label: string; value: string }>) {
  return (
    <div style={{ display: "flex", justifyContent: "space-between", gap: 12 }}>
      <span style={{ color: "var(--fg-2)" }}>{label}</span>
      <span style={{ fontVariantNumeric: "tabular-nums" }}>{value}</span>
    </div>
  );
}

function CurrencyCard({ row }: Readonly<{ row: CurrencyReconciliation }>) {
  return (
    <Card soft style={{ padding: 16, display: "grid", gap: 10 }}>
      <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
        <strong style={{ fontSize: 15 }}>{row.currency}</strong>
        <Pill tone={verdictTone(row)} size="sm">
          {FUNDING_VERDICT_LABEL[row.verdict]}
        </Pill>
      </div>
      <div style={{ fontSize: 18, fontWeight: 700 }}>{verdictLine(row)}</div>
      <div style={{ display: "grid", gap: 4, fontSize: 12 }}>
        <BreakdownRow
          label="가용 현금 (live 계좌)"
          value={money(row.available_cash, row.currency)}
        />
        <BreakdownRow
          label="트리거 소요 합계"
          value={money(row.required_total, row.currency)}
        />
        <div style={{ height: 1, background: "var(--border)", margin: "2px 0" }} />
        <BreakdownRow
          label="· 물타기 A(k)"
          value={money(row.required_averaging_adds, row.currency)}
        />
        <BreakdownRow
          label="· 지지 그물 (워치형)"
          value={money(row.required_support_net, row.currency)}
        />
        <BreakdownRow
          label="· 그 밖의 활성 매수 워치"
          value={money(row.required_active_watches, row.currency)}
        />
      </div>
      {row.notes.length > 0 && (
        <ul
          style={{
            margin: 0,
            paddingLeft: 16,
            fontSize: 11,
            color: "var(--fg-3, var(--fg-2))",
            display: "grid",
            gap: 2,
          }}
        >
          {row.notes.map((note) => (
            <li key={note}>{note}</li>
          ))}
        </ul>
      )}
    </Card>
  );
}

export function FundingBlock({ funding }: Readonly<{ funding: BuyPlanFunding }>) {
  const included = funding.accounts.filter((a) => a.included_in_reserve);
  return (
    <section style={{ display: "grid", gap: 12 }}>
      <h2 style={{ margin: 0, fontSize: 16 }}>자금 대조</h2>
      <div
        style={{
          display: "grid",
          gap: 12,
          gridTemplateColumns: "repeat(auto-fit, minmax(260px, 1fr))",
        }}
      >
        {funding.currencies.map((row) => (
          <CurrencyCard key={row.currency} row={row} />
        ))}
      </div>
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
                  title={account.available_cash ?? "확인 불가"}
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
