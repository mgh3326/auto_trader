import type { AccountSource, GroupedHolding, Holding, Market } from "../types/invest";
import { formatKrw, formatUsd } from "../format/currency";
import { formatNumber } from "../format/number";
import { formatPercent } from "../format/percent";

const SRC_LABEL: Record<AccountSource, string> = {
  kis: "KIS",
  upbit: "Upbit",
  toss_manual: "Toss 수동",
  pension_manual: "퇴직연금",
  isa_manual: "ISA",
  kis_mock: "KIS 모의",
  kiwoom_mock: "키움 모의",
  alpaca_paper: "Alpaca",
  db_simulated: "DB 시뮬",
};

function valueFmt(currency: string, v: number | null | undefined): string {
  if (v === null || v === undefined) return "-";
  return currency === "USD" ? formatUsd(v) : formatKrw(v);
}

function gainClass(rate: number | null | undefined): string {
  if (rate === null || rate === undefined) return "fallback";
  return rate >= 0 ? "gain-pos" : "gain-neg";
}

export function GroupedRow({ row }: { row: GroupedHolding }) {
  const sources = row.includedSources.map((s) => SRC_LABEL[s]).join(" · ");
  return (
    <div data-testid="grouped-row" style={rowStyle}>
      <div>
        <div style={{ fontWeight: 600, fontSize: 12 }}>
          {row.displayName} <SourceChip text={sources} />
        </div>
        <div className="subtle" style={{ fontSize: 10 }}>
          {row.symbol} · 합산 {formatNumber(row.totalQuantity)}
          {unitFor(row.market)} · 평단 {valueFmt(row.currency, row.averageCost)}
        </div>
      </div>
      <div style={{ textAlign: "right", fontSize: 11 }}>
        <div>{valueFmt(row.currency, row.valueNative ?? row.valueKrw)}</div>
        <div className={gainClass(row.pnlRate)}>{formatPercent(row.pnlRate)}</div>
      </div>
    </div>
  );
}

export function RawRow({ row }: { row: Holding }) {
  return (
    <div data-testid="raw-row" style={rowStyle}>
      <div>
        <div style={{ fontWeight: 600, fontSize: 12 }}>
          {row.displayName} <SourceChip text={SRC_LABEL[row.source]} />
        </div>
        <div className="subtle" style={{ fontSize: 10 }}>
          {row.symbol} · {formatNumber(row.quantity)}
          {unitFor(row.market)} · 평단 {valueFmt(row.currency, row.averageCost)}
        </div>
      </div>
      <div style={{ textAlign: "right", fontSize: 11 }}>
        <div>{valueFmt(row.currency, row.valueNative ?? row.valueKrw)}</div>
        <div className={gainClass(row.pnlRate)}>{formatPercent(row.pnlRate)}</div>
      </div>
    </div>
  );
}

const rowStyle: React.CSSProperties = {
  display: "flex",
  justifyContent: "space-between",
  alignItems: "center",
  padding: "8px 4px",
  borderBottom: "1px solid var(--surface-2)",
};

function SourceChip({ text }: { text: string }) {
  return (
    <span
      style={{
        display: "inline-block",
        padding: "1px 6px",
        marginLeft: 4,
        background: "var(--pill-mix)",
        color: "var(--pill-mix-fg)",
        borderRadius: 6,
        fontSize: 9,
        verticalAlign: "middle",
      }}
    >
      {text}
    </span>
  );
}

function unitFor(market: Market): string {
  if (market === "CRYPTO") return "";
  return market === "KR" ? "주" : "shares";
}
