import { Link } from "react-router-dom";
import { Sparkline } from "../../ds";
import type { MarketDashboardResponse, MarketDashboardTone } from "../../types/marketDashboard";

export interface MarketStripItem {
  name: string;
  value: string;
  changeLabel: string;
  pct: number;
  direction: "up" | "down" | "flat";
  spark?: number[];
  href?: string;
}

const COLOR: Record<MarketStripItem["direction"], string> = {
  up: "var(--gain)",
  down: "var(--loss)",
  flat: "var(--flat)",
};

const ARROW: Record<MarketStripItem["direction"], string> = {
  up: "▲",
  down: "▼",
  flat: "·",
};

function toDirection(tone: MarketDashboardTone): MarketStripItem["direction"] {
  return tone === "up" || tone === "down" ? tone : "flat";
}

export function marketDashboardToStripItems(data: MarketDashboardResponse | null): MarketStripItem[] {
  if (!data) return [];
  const sections = ["kr_market", "global_indices", "fx_macro"];
  return data.sections
    .filter((section) => sections.includes(section.id))
    .flatMap((section) =>
      section.metrics.map((metric) => ({
        name: section.id === "fx_macro" ? `FX·매크로 ${metric.label}` : metric.label,
        value: metric.value ?? "—",
        changeLabel: metric.change != null ? `${metric.change >= 0 ? "+" : ""}${metric.change.toLocaleString()}` : "변동 정보 없음",
        pct: metric.changePct ?? 0,
        direction: toDirection(metric.tone),
        href: section.id === "fx_macro" ? "/market/fx" : undefined,
      })),
    )
    .slice(0, 5);
}

export function MarketStrip({ items }: { items: MarketStripItem[] }) {
  if (items.length === 0) {
    // Keep the rail stable while the market endpoint is loading or unavailable.
    return (
      <div data-testid="market-strip-placeholder" style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(150px, 1fr))", gap: 10 }}>
        {Array.from({ length: 4 }).map((_, i) => (
          <div
            key={i}
            style={{
              padding: 12,
              background: "var(--surface)",
              border: "1px solid var(--border)",
              borderRadius: 14,
              boxShadow: "var(--shadow-1)",
              minHeight: 92,
            }}
          >
            <div style={{ fontSize: 12, fontWeight: 600, color: "var(--fg-3)" }}>지수 정보</div>
            <div style={{ fontSize: 12, color: "var(--fg-3)", marginTop: 8 }}>곧 제공 예정</div>
          </div>
        ))}
      </div>
    );
  }

  return (
    <div data-testid="market-strip" style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(150px, 1fr))", gap: 10 }}>
      {items.map((m) => {
        const c = COLOR[m.direction];
        const card = (
          <div
            style={{
              padding: 12,
              background: "var(--surface)",
              border: "1px solid var(--border)",
              borderRadius: 14,
              boxShadow: "var(--shadow-1)",
              minHeight: 92,
            }}
          >
            <div style={{ fontSize: 12, fontWeight: 600, color: "var(--fg-2)" }}>{m.name}</div>
            <div style={{ fontSize: 16, fontWeight: 700, marginTop: 2, fontFeatureSettings: '"tnum"' }}>{m.value}</div>
            <div style={{ fontSize: 12, color: c, fontWeight: 600, marginTop: 1, fontFeatureSettings: '"tnum"' }}>
              <span style={{ marginRight: 4 }}>{ARROW[m.direction]}</span>
              {m.changeLabel} · {m.pct >= 0 ? "+" : ""}
              {m.pct.toFixed(2)}%
            </div>
            {m.href && <div style={{ color: "var(--accent)", fontWeight: 900, fontSize: 12, marginTop: 6 }}>상세 보기</div>}
            {m.spark && m.spark.length > 1 && (
              <div style={{ marginTop: 6 }}>
                <Sparkline points={m.spark} color={c} width={200} height={28} />
              </div>
            )}
          </div>
        );
        return m.href ? (
          <Link key={m.name} to={m.href} style={{ textDecoration: "none", color: "inherit" }}>
            {card}
          </Link>
        ) : (
          <div key={m.name}>{card}</div>
        );
      })}
    </div>
  );
}
