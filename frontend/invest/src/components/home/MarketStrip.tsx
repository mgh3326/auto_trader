import { Sparkline } from "../../ds";

export interface MarketStripItem {
  name: string;
  value: string;
  changeLabel: string;
  pct: number;
  direction: "up" | "down" | "flat";
  spark?: number[];
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

export function MarketStrip({ items }: { items: MarketStripItem[] }) {
  if (items.length === 0) {
    // Backend hookup is deferred; render a 4-up muted placeholder so the
    // section does not collapse in dev.
    return (
      <div data-testid="market-strip-placeholder" style={{ display: "grid", gridTemplateColumns: "repeat(4, 1fr)", gap: 10 }}>
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
    <div data-testid="market-strip" style={{ display: "grid", gridTemplateColumns: "repeat(4, 1fr)", gap: 10 }}>
      {items.map((m) => {
        const c = COLOR[m.direction];
        return (
          <div
            key={m.name}
            style={{
              padding: 12,
              background: "var(--surface)",
              border: "1px solid var(--border)",
              borderRadius: 14,
              boxShadow: "var(--shadow-1)",
            }}
          >
            <div style={{ fontSize: 12, fontWeight: 600, color: "var(--fg-2)" }}>{m.name}</div>
            <div style={{ fontSize: 16, fontWeight: 700, marginTop: 2, fontFeatureSettings: '"tnum"' }}>{m.value}</div>
            <div style={{ fontSize: 12, color: c, fontWeight: 600, marginTop: 1, fontFeatureSettings: '"tnum"' }}>
              <span style={{ marginRight: 4 }}>{ARROW[m.direction]}</span>
              {m.changeLabel} · {m.pct >= 0 ? "+" : ""}
              {m.pct.toFixed(2)}%
            </div>
            {m.spark && m.spark.length > 1 && (
              <div style={{ marginTop: 6 }}>
                <Sparkline points={m.spark} color={c} width={200} height={28} />
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}
