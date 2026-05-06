import type { HomeSummary } from "../types/invest";
import { formatKrw } from "../format/currency";
import { formatPercent } from "../format/percent";

export function HeroCard({ summary }: { summary: HomeSummary }) {
  const gainCls = (summary.pnlRate ?? 0) >= 0 ? "gain-pos" : "gain-neg";
  return (
    <div
      data-testid="hero-card"
      style={{
        background: "var(--surface)",
        borderRadius: 14,
        padding: 16,
        textAlign: "center",
      }}
    >
      <div className="subtle">내 투자 ({summary.includedSources.join(" · ").toUpperCase()})</div>
      <div style={{ fontSize: 30, fontWeight: 700, marginTop: 4 }}>
        {formatKrw(summary.totalValueKrw)}
      </div>
      <div className={gainCls} style={{ fontSize: 13, marginTop: 2 }}>
        {summary.pnlKrw === null ? "-" : formatKrw(summary.pnlKrw)} ·{" "}
        {formatPercent(summary.pnlRate)}
      </div>
      <div className="subtle" style={{ marginTop: 4 }}>
        {summary.costBasisKrw === null
          ? "원금 산정 불가"
          : `원금 ${formatKrw(summary.costBasisKrw)} 기준`}
      </div>
    </div>
  );
}
