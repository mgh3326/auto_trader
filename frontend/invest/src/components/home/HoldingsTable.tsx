import type { GroupedHolding } from "../../types/invest";
import { Pill } from "../../ds";
import { pillToneForSource } from "../../desktop/AccountSourceTone";
import type { AssetCategoryKey } from "../AssetCategoryFilter";

const COLS = "minmax(0,1.8fr) 100px 120px 140px 100px";

function fmtKrw(v: number | null | undefined): string {
  if (v == null) return "—";
  return `₩${Math.round(v).toLocaleString("ko-KR")}`;
}

function fmtUsd(v: number | null | undefined): string {
  if (v == null) return "—";
  return `$${v.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
}

function fmtQty(qty: number, _market: GroupedHolding["market"], assetType: GroupedHolding["assetType"]): string {
  if (assetType === "crypto") return `${qty}`;
  return `${qty.toLocaleString("ko-KR")}주`;
}

export function HoldingsTable({
  holdings,
  filter,
}: {
  holdings: GroupedHolding[];
  filter: AssetCategoryKey;
}) {
  const items = filter === "all" ? holdings : holdings.filter((h) => h.assetCategory === filter);

  return (
    <div
      data-testid="holdings-table"
      style={{
        background: "var(--surface)",
        border: "1px solid var(--border)",
        borderRadius: 16,
        boxShadow: "var(--shadow-1)",
        overflow: "hidden",
      }}
    >
      <div
        style={{
          display: "grid",
          gridTemplateColumns: COLS,
          padding: "12px 22px",
          fontSize: 12,
          fontWeight: 600,
          color: "var(--fg-3)",
          borderBottom: "1px solid var(--divider)",
        }}
      >
        <div>종목</div>
        <div style={{ textAlign: "right" }}>수량</div>
        <div style={{ textAlign: "right" }}>평단</div>
        <div style={{ textAlign: "right" }}>평가금액</div>
        <div style={{ textAlign: "right" }}>수익률</div>
      </div>

      {items.length === 0 ? (
        <div data-testid="holdings-empty" style={{ padding: 32, textAlign: "center", color: "var(--fg-3)", fontSize: 13 }}>
          해당 조건에 보유 종목이 없습니다.
        </div>
      ) : (
        items.map((h, i) => {
          const usd = h.currency === "USD";
          const dir = (h.pnlRate ?? 0) >= 0 ? "up" : "down";
          const color = dir === "up" ? "var(--gain)" : "var(--loss)";
          const arrow = dir === "up" ? "▲" : "▼";
          const tone = h.includedSources[0] ? pillToneForSource(h.includedSources[0]) : "paper";
          const value = h.valueNative ?? h.valueKrw;
          return (
            <div
              key={h.groupId}
              data-testid="holdings-row"
              data-category={h.assetCategory}
              style={{
                display: "grid",
                gridTemplateColumns: COLS,
                alignItems: "center",
                padding: "16px 22px",
                borderTop: i === 0 ? "none" : "1px solid var(--divider)",
                fontFeatureSettings: '"tnum"',
                fontSize: 14,
              }}
            >
              <div style={{ display: "flex", alignItems: "center", gap: 12, minWidth: 0 }}>
                <div
                  aria-hidden
                  style={{
                    width: 36,
                    height: 36,
                    borderRadius: 10,
                    flexShrink: 0,
                    background: `var(--pill-${tone}-bg)`,
                    color: `var(--pill-${tone}-fg)`,
                    display: "grid",
                    placeItems: "center",
                    fontWeight: 700,
                    fontSize: 13,
                  }}
                >
                  {h.displayName.slice(0, 1)}
                </div>
                <div style={{ minWidth: 0 }}>
                  <div
                    style={{
                      fontSize: 14,
                      fontWeight: 700,
                      color: "var(--fg)",
                      overflow: "hidden",
                      textOverflow: "ellipsis",
                      whiteSpace: "nowrap",
                    }}
                  >
                    {h.displayName}
                  </div>
                  <div style={{ display: "flex", alignItems: "center", gap: 6, marginTop: 2 }}>
                    <span style={{ fontSize: 11, color: "var(--fg-3)", fontFamily: "var(--font-mono)" }}>{h.symbol}</span>
                    <span aria-hidden style={{ width: 2, height: 2, background: "var(--fg-4)", borderRadius: 999 }} />
                    <Pill tone={tone} size="sm">
                      {tone.toUpperCase()}
                    </Pill>
                  </div>
                </div>
              </div>

              <div style={{ textAlign: "right", color: "var(--fg-1)", fontWeight: 500 }}>
                {fmtQty(h.totalQuantity, h.market, h.assetType)}
              </div>

              <div style={{ textAlign: "right", color: "var(--fg-2)", fontWeight: 500 }}>
                {usd ? fmtUsd(h.averageCost) : fmtKrw(h.averageCost)}
                {usd && <span style={{ fontSize: 10, color: "var(--fg-3)", marginLeft: 4 }}>USD</span>}
              </div>

              <div style={{ textAlign: "right" }}>
                <div style={{ fontWeight: 700, color: "var(--fg)" }}>
                  {usd ? fmtUsd(value) : fmtKrw(value)}
                </div>
                <div style={{ fontSize: 11, color: "var(--fg-3)", marginTop: 1 }}>{usd ? "USD" : "KRW"}</div>
              </div>

              <div style={{ textAlign: "right", color, fontWeight: 700 }}>
                {h.pnlRate != null ? (
                  <>
                    <span style={{ fontSize: 10, marginRight: 3 }}>{arrow}</span>
                    {h.pnlRate >= 0 ? "+" : ""}
                    {(h.pnlRate * 100).toFixed(2)}%
                  </>
                ) : (
                  <span style={{ color: "var(--fg-3)" }}>—</span>
                )}
              </div>
            </div>
          );
        })
      )}
    </div>
  );
}
