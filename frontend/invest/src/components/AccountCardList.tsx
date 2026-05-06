import type { Account } from "../types/invest";
import { formatKrw, formatUsd } from "../format/currency";
import { formatPercent } from "../format/percent";

function gainClass(rate: number | null | undefined): string {
  if (rate === null || rate === undefined) return "fallback";
  return rate >= 0 ? "gain-pos" : "gain-neg";
}

function AccountCard({ a }: { a: Account }) {
  const isToss = a.source === "toss_manual";
  return (
    <div
      data-testid="account-card"
      style={{
        minWidth: 220,
        background: "var(--surface)",
        borderRadius: 14,
        padding: 12,
        flex: "0 0 auto",
      }}
    >
      <div style={{ fontWeight: 600, fontSize: 12 }}>
        {a.displayName}
        {isToss && (
          <span
            style={{
              marginLeft: 6,
              padding: "1px 6px",
              borderRadius: 5,
              background: "#1c1e24",
              color: "var(--muted)",
              fontSize: 9,
            }}
          >
            수동
          </span>
        )}
      </div>
      <div style={{ fontWeight: 700, fontSize: 18, marginTop: 4 }}>{formatKrw(a.valueKrw)}</div>
      <div className={gainClass(a.pnlRate)} style={{ fontSize: 11 }}>
        {a.pnlKrw === null || a.pnlKrw === undefined ? "-" : formatKrw(a.pnlKrw)} ·{" "}
        {formatPercent(a.pnlRate)}
        {(a.costBasisKrw === null || a.costBasisKrw === undefined) && (
          <span className="subtle"> · 원금 정보 부족</span>
        )}
      </div>
      <div
        style={{
          marginTop: 10,
          paddingTop: 8,
          borderTop: "1px solid var(--surface-2)",
          display: "grid",
          gridTemplateColumns: "1fr 1fr",
          gap: "4px 10px",
        }}
      >
        {a.source === "kis" && (
          <>
            <Cell k="원화 현금" v={formatKrw(a.cashBalances.krw ?? null)} />
            <Cell k="달러 현금" v={formatUsd(a.cashBalances.usd ?? null)} />
            <Cell k="원화 매수" v={formatKrw(a.buyingPower.krw ?? null)} />
            <Cell k="달러 매수" v={formatUsd(a.buyingPower.usd ?? null)} />
          </>
        )}
        {a.source === "upbit" && (
          <>
            <Cell k="원화 현금" v={formatKrw(a.cashBalances.krw ?? null)} />
            <Cell k="원화 매수" v={formatKrw(a.buyingPower.krw ?? null)} />
          </>
        )}
        {isToss && (
          <>
            <Cell
              k="원화 현금"
              v={a.cashBalances.krw === undefined ? "-" : formatKrw(a.cashBalances.krw)}
            />
            <Cell
              k="원화 매수"
              v={a.buyingPower.krw === undefined ? "-" : formatKrw(a.buyingPower.krw)}
            />
          </>
        )}
      </div>
    </div>
  );
}

function Cell({ k, v }: { k: string; v: string }) {
  return (
    <div style={{ display: "flex", justifyContent: "space-between" }}>
      <span style={{ color: "var(--muted)", fontSize: 10 }}>{k}</span>
      <span style={{ fontSize: 11, textAlign: "right" }}>{v}</span>
    </div>
  );
}

export function AccountCardList({ accounts }: { accounts: Account[] }) {
  return (
    <div>
      <div className="subtle" style={{ padding: "0 4px 4px" }}>
        계좌
      </div>
      <div style={{ display: "flex", gap: 8, overflowX: "auto" }}>
        {accounts.map((a) => (
          <AccountCard key={a.accountId} a={a} />
        ))}
      </div>
    </div>
  );
}
