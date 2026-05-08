import type { GroupedHolding, HomeSummary } from "../../types/invest";
import { Card, PL } from "../../ds";

const CATEGORY_LABEL: Record<string, string> = {
  kr_stock: "한국주식",
  us_stock: "해외주식",
  crypto: "코인",
};

function fmtKrw(v?: number | null): string {
  if (v == null) return "—";
  return `₩${Math.round(v).toLocaleString("ko-KR")}`;
}

function fmtKrwShort(v?: number | null): string {
  if (v == null) return "—";
  if (v >= 1e8) return `₩${(v / 1e8).toFixed(2)}억`;
  if (v >= 1e4) return `₩${(v / 1e4).toFixed(0)}만`;
  return `₩${Math.round(v).toLocaleString("ko-KR")}`;
}

function buildBreakdown(holdings: GroupedHolding[]) {
  const totals = new Map<string, number>();
  for (const h of holdings) {
    if (h.valueKrw == null) continue;
    totals.set(h.assetCategory, (totals.get(h.assetCategory) ?? 0) + h.valueKrw);
  }
  const ordered = ["kr_stock", "us_stock", "crypto"];
  return ordered
    .filter((k) => totals.has(k))
    .map((k) => ({ key: k, label: CATEGORY_LABEL[k] ?? k, value: totals.get(k) ?? 0 }));
}

export function DesktopHero({
  summary,
  accountCount,
  holdings,
  asOfLabel,
}: {
  summary: HomeSummary;
  accountCount: number;
  holdings: GroupedHolding[];
  asOfLabel?: string;
}) {
  const breakdown = buildBreakdown(holdings);
  const subtitle = `내 투자 포트폴리오${accountCount > 0 ? ` · ${accountCount}개 계좌` : ""}`;

  return (
    <Card data-testid="hero-card" style={{ padding: 24 }}>
      <div style={{ minWidth: 0 }}>
        <div style={{ fontSize: 13, color: "var(--fg-3)", fontWeight: 500 }}>{subtitle}</div>
        <div
          style={{
            fontSize: "clamp(28px, 2.2vw, 32px)",
            fontWeight: 700,
            letterSpacing: "-0.025em",
            marginTop: 2,
            fontFeatureSettings: '"tnum"',
          }}
        >
          {fmtKrw(summary.totalValueKrw)}
        </div>
        {summary.pnlKrw != null && summary.pnlRate != null ? (
          <div style={{ marginTop: 2 }}>
            <PL value={summary.pnlKrw} pct={summary.pnlRate * 100} size={14} />
          </div>
        ) : (
          <div style={{ marginTop: 2, fontSize: 14, color: "var(--fg-3)" }}>—</div>
        )}
        <div style={{ fontSize: 12, color: "var(--fg-3)", marginTop: 4 }}>
          {summary.costBasisKrw != null ? `원금 ${fmtKrw(summary.costBasisKrw)}` : "원금 산정 불가"}
          {asOfLabel ? ` · ${asOfLabel} 업데이트` : ""}
        </div>
      </div>

      {breakdown.length > 0 && (
        <div
          style={{
            display: "grid",
            gridTemplateColumns: `repeat(${breakdown.length}, 1fr)`,
            gap: 24,
            marginTop: 18,
            paddingTop: 16,
            borderTop: "1px solid var(--divider)",
          }}
        >
          {breakdown.map((b) => (
            <div key={b.key} data-testid="hero-breakdown" data-category={b.key}>
              <div style={{ display: "flex", alignItems: "center", gap: 6, fontSize: 12, color: "var(--fg-3)" }}>
                <span aria-hidden style={{ width: 5, height: 5, borderRadius: 999, background: "var(--fg-4)" }} />
                {b.label}
              </div>
              <div style={{ fontSize: 17, fontWeight: 700, marginTop: 2, fontFeatureSettings: '"tnum"' }}>
                {fmtKrwShort(b.value)}
              </div>
            </div>
          ))}
        </div>
      )}
    </Card>
  );
}
