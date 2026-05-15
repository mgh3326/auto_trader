import type { Account, InvestHomeWarning } from "../types/invest";
import { formatKrw, formatUsd } from "../format/currency";
import { formatPercent } from "../format/percent";
import { accountSourceMeta, displayNameWithSource } from "../desktop/AccountSourceMeta";

function gainClass(rate: number | null | undefined): string {
  if (rate === null || rate === undefined) return "fallback";
  return rate >= 0 ? "gain-pos" : "gain-neg";
}

function AccountCard({ a, warnings = [] }: { a: Account; warnings?: InvestHomeWarning[] }) {
  const meta = accountSourceMeta(a.source);
  const isManual = a.accountKind === "manual";
  const showUsd = a.cashBalances.usd != null || a.buyingPower.usd != null || a.source === "kis" || a.source === "kis_mock" || a.source === "alpaca_paper";
  const kisUsdWarning =
    a.source === "kis" && warnings.some((w) => w.source === "kis" && w.message.includes("USD"));

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
      <div style={{ fontWeight: 600, fontSize: 12, display: "flex", justifyContent: "space-between", gap: 8 }}>
        <span style={{ minWidth: 0, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{displayNameWithSource(a)}</span>
        <span
          style={{
            padding: "1px 6px",
            borderRadius: 5,
            background: `var(--pill-${meta.tone}-bg)`,
            color: `var(--pill-${meta.tone}-fg)`,
            fontSize: 9,
            flexShrink: 0,
          }}
        >
          {isManual ? "수동" : meta.badge}
        </span>
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
        <>
          <Cell k="KRW · 현금" v={formatKrw(a.cashBalances.krw ?? null)} />
          {showUsd && (
            <Cell
              k="USD · 현금"
              v={kisUsdWarning ? "확인 필요" : formatUsd(a.cashBalances.usd ?? null)}
              warn={kisUsdWarning}
            />
          )}
          <Cell k="KRW · 매수 가능" v={formatKrw(a.buyingPower.krw ?? null)} />
          {showUsd && (
            <Cell
              k="USD · 매수 가능"
              v={kisUsdWarning ? "확인 필요" : formatUsd(a.buyingPower.usd ?? null)}
              warn={kisUsdWarning}
            />
          )}
        </>
      </div>
    </div>
  );
}

function Cell({ k, v, warn }: { k: string; v: string; warn?: boolean }) {
  return (
    <div style={{ display: "flex", justifyContent: "space-between" }}>
      <span style={{ color: "var(--muted)", fontSize: 10 }}>{k}</span>
      <span style={{ fontSize: 11, textAlign: "right", color: warn ? "var(--warn)" : "inherit" }}>
        {v}
      </span>
    </div>
  );
}

export function AccountCardList({
  accounts,
  warnings = [],
}: {
  accounts: Account[];
  warnings?: InvestHomeWarning[];
}) {
  return (
    <div style={{ padding: "0 16px" }}>
      <div className="subtle" style={{ padding: "0 4px 6px" }}>
        계좌
      </div>
      <div
        style={{
          display: "flex",
          gap: 8,
          overflowX: "auto",
          WebkitOverflowScrolling: "touch",
          scrollbarWidth: "none",
          msOverflowStyle: "none",
        }}
      >
        <style>
          {`div::-webkit-scrollbar { display: none; }`}
        </style>
        {accounts.map((a) => (
          <AccountCard key={a.accountId} a={a} warnings={warnings} />
        ))}
      </div>
    </div>
  );
}
