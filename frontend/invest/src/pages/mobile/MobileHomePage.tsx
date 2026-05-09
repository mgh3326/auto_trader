import { useMemo, useState } from "react";
import { MobileShell } from "../../mobile/MobileShell";
import { useInvestHome } from "../../hooks/useInvestHome";
import { useAccountPanel } from "../../desktop/useAccountPanel";
import { scopeGroupedToSource } from "../../desktop/scopeHoldings";
import { pillToneForSource } from "../../desktop/AccountSourceTone";
import { PL, Pill } from "../../ds";
import type { AccountSource, GroupedHolding, HomeSummary } from "../../types/invest";
import type { AssetCategoryKey } from "../../components/AssetCategoryFilter";

function fmtKrw(v: number | null | undefined): string {
  if (v == null) return "—";
  return `₩${Math.round(v).toLocaleString("ko-KR")}`;
}

function fmtUsd(v: number | null | undefined): string {
  if (v == null) return "—";
  return `$${v.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
}

function fmtQty(qty: number, assetType: GroupedHolding["assetType"]): string {
  if (assetType === "crypto") return `${qty}`;
  return `${qty.toLocaleString("ko-KR")}주`;
}

const CATEGORIES: { key: AssetCategoryKey; label: string }[] = [
  { key: "all", label: "전체" },
  { key: "kr_stock", label: "한국주식" },
  { key: "us_stock", label: "해외주식" },
  { key: "crypto", label: "코인" },
];

export function MobileHomePage() {
  const home = useInvestHome();
  const panel = useAccountPanel();
  const [account, setAccount] = useState<"all" | AccountSource>("all");
  const [category, setCategory] = useState<AssetCategoryKey>("all");

  const data = home.state.status === "ready" ? home.state.data : null;

  const scopedGrouped = useMemo(() => {
    if (!data) return [];
    if (account === "all") return data.groupedHoldings;
    return scopeGroupedToSource(data.groupedHoldings, account);
  }, [data, account]);

  const filteredScoped = useMemo(() => {
    return category === "all"
      ? scopedGrouped
      : scopedGrouped.filter((g) => g.assetCategory === category);
  }, [scopedGrouped, category]);

  const summary: HomeSummary | null = useMemo(() => {
    if (!data) return null;
    if (account === "all") return data.homeSummary;
    const acct = data.accounts.find((a) => a.source === account);
    if (!acct) return data.homeSummary;
    return {
      includedSources: [acct.source],
      excludedSources: [],
      totalValueKrw: acct.valueKrw,
      costBasisKrw: acct.costBasisKrw,
      pnlKrw: acct.pnlKrw,
      pnlRate: acct.pnlRate,
    };
  }, [data, account]);

  return (
    <MobileShell title="홈">
      {home.state.status === "loading" && (
        <div style={{ padding: 32, color: "var(--fg-3)", textAlign: "center" }}>불러오는 중…</div>
      )}
      {home.state.status === "error" && (
        <div style={{ padding: 16, color: "var(--danger)" }}>
          잠시 후 다시 시도해 주세요.{" "}
          <button
            type="button"
            onClick={home.reload}
            style={{
              marginLeft: 8,
              padding: "4px 10px",
              borderRadius: 8,
              border: "1px solid var(--border)",
              background: "var(--surface)",
              color: "var(--fg-1)",
              cursor: "pointer",
              fontFamily: "inherit",
              fontSize: 12,
            }}
          >
            재시도
          </button>
        </div>
      )}

      {data && summary && (
        <div style={{ display: "flex", flexDirection: "column", gap: 14, padding: "14px 0 16px" }}>
          {/* Hero — single column on mobile */}
          <section style={{ padding: "0 16px" }} data-testid="mobile-hero">
            <div style={{ fontSize: 13, fontWeight: 600, color: "var(--fg-3)" }}>
              내 투자 포트폴리오
              {account === "all" && data.accounts.length > 0 && ` · ${data.accounts.length}개 계좌`}
            </div>
            <div
              style={{
                fontSize: 28,
                fontWeight: 700,
                marginTop: 2,
                letterSpacing: "-0.02em",
                fontFeatureSettings: '"tnum"',
              }}
            >
              {fmtKrw(summary.totalValueKrw)}
            </div>
            {summary.pnlKrw != null && summary.pnlRate != null ? (
              <div style={{ marginTop: 2 }}>
                <PL value={summary.pnlKrw} pct={summary.pnlRate * 100} size={13} />
              </div>
            ) : (
              <div style={{ marginTop: 2, fontSize: 13, color: "var(--fg-3)" }}>—</div>
            )}
            {summary.costBasisKrw != null && (
              <div style={{ fontSize: 12, color: "var(--fg-3)", marginTop: 4 }}>
                원금 {fmtKrw(summary.costBasisKrw)}
              </div>
            )}
          </section>

          {/* Account selector — 전체 + per-source pills */}
          {data.accounts.length > 0 && (
            <section style={{ padding: "0 16px" }} data-testid="mobile-account-row">
              <div style={{ display: "flex", gap: 6, overflowX: "auto", paddingBottom: 4 }}>
                <PillButton on={account === "all"} onClick={() => setAccount("all")}>
                  전체
                </PillButton>
                {data.accounts.map((a) => (
                  <PillButton
                    key={a.accountId}
                    on={account === a.source}
                    onClick={() => setAccount(a.source)}
                  >
                    {a.displayName}
                  </PillButton>
                ))}
              </div>
            </section>
          )}

          {/* Category filter chips */}
          <section style={{ padding: "0 16px" }}>
            <div style={{ display: "flex", gap: 6, overflowX: "auto", paddingBottom: 4 }}>
              {CATEGORIES.map((c) => (
                <PillButton
                  key={c.key}
                  on={category === c.key}
                  onClick={() => setCategory(c.key)}
                >
                  {c.label}
                </PillButton>
              ))}
            </div>
          </section>

          {/* Holdings list */}
          <section style={{ padding: "0 16px" }}>
            <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 8 }}>
              <h3 style={{ margin: 0, fontSize: 13, fontWeight: 700, color: "var(--fg)" }}>보유 종목</h3>
            </div>
            {filteredScoped.length === 0 ? (
              <div data-testid="mobile-holdings-empty" style={{ padding: 32, textAlign: "center", color: "var(--fg-3)", fontSize: 13 }}>
                해당 조건에 보유 종목이 없습니다.
              </div>
            ) : (
              <div style={{ display: "flex", flexDirection: "column" }}>
                {filteredScoped.map((h) => {
                  const tone = h.includedSources[0] ? pillToneForSource(h.includedSources[0]) : "paper";
                  const usd = h.currency === "USD";
                  const value = h.valueNative ?? h.valueKrw;
                  return (
                    <div
                      key={h.groupId}
                      data-testid="mobile-holdings-row"
                      data-category={h.assetCategory}
                      style={{
                        display: "flex",
                        alignItems: "center",
                        gap: 10,
                        padding: "10px 0",
                        borderBottom: "1px solid var(--divider)",
                      }}
                    >
                      <div
                        aria-hidden
                        style={{
                          width: 32,
                          height: 32,
                          borderRadius: 8,
                          flexShrink: 0,
                          background: `var(--pill-${tone}-bg)`,
                          color: `var(--pill-${tone}-fg)`,
                          display: "grid",
                          placeItems: "center",
                          fontWeight: 700,
                          fontSize: 12,
                        }}
                      >
                        {h.displayName.slice(0, 1)}
                      </div>
                      <div style={{ flex: 1, minWidth: 0 }}>
                        <div
                          style={{
                            fontSize: 14,
                            fontWeight: 700,
                            overflow: "hidden",
                            textOverflow: "ellipsis",
                            whiteSpace: "nowrap",
                            color: "var(--fg)",
                          }}
                        >
                          {h.displayName}
                        </div>
                        <div style={{ display: "flex", alignItems: "center", gap: 6, marginTop: 1 }}>
                          <span style={{ fontSize: 11, color: "var(--fg-3)", fontFeatureSettings: '"tnum"' }}>
                            {fmtQty(h.totalQuantity, h.assetType)}
                          </span>
                          <Pill tone={tone} size="sm">
                            {tone.toUpperCase()}
                          </Pill>
                        </div>
                      </div>
                      <div style={{ textAlign: "right", fontFeatureSettings: '"tnum"' }}>
                        <div style={{ fontSize: 14, fontWeight: 700, color: "var(--fg)" }}>
                          {usd ? fmtUsd(value) : fmtKrw(value)}
                        </div>
                        {h.pnlRate != null ? (
                          <div
                            style={{
                              fontSize: 12,
                              fontWeight: 700,
                              color: h.pnlRate >= 0 ? "var(--gain)" : "var(--loss)",
                            }}
                          >
                            <span style={{ fontSize: 9, marginRight: 2 }}>{h.pnlRate >= 0 ? "▲" : "▼"}</span>
                            {h.pnlRate >= 0 ? "+" : ""}
                            {(h.pnlRate * 100).toFixed(2)}%
                          </div>
                        ) : (
                          <div style={{ fontSize: 12, color: "var(--fg-3)" }}>—</div>
                        )}
                      </div>
                    </div>
                  );
                })}
              </div>
            )}
          </section>

          {data.meta?.warnings && data.meta.warnings.length > 0 && (
            <div
              role="alert"
              style={{
                margin: "0 16px",
                padding: "10px 14px",
                color: "var(--warn)",
                background: "var(--warn-soft)",
                borderRadius: 12,
                fontSize: 12,
              }}
            >
              {data.meta.warnings.map((w) => `⚠ ${w.source}: ${w.message}`).join(" · ")}
            </div>
          )}

          {panel.error && (
            <div
              role="alert"
              style={{
                margin: "0 16px",
                padding: "10px 14px",
                color: "var(--danger)",
                background: "var(--danger-soft)",
                borderRadius: 12,
                fontSize: 12,
              }}
            >
              계좌 정보를 불러오지 못했습니다.{" "}
              <button
                type="button"
                onClick={panel.reload}
                style={{
                  marginLeft: 8,
                  padding: "2px 8px",
                  borderRadius: 6,
                  border: "1px solid var(--border)",
                  background: "var(--surface)",
                  color: "var(--fg-1)",
                  cursor: "pointer",
                  fontFamily: "inherit",
                  fontSize: 11,
                }}
              >
                재시도
              </button>
            </div>
          )}
        </div>
      )}
    </MobileShell>
  );
}

function PillButton({ on, onClick, children }: { on: boolean; onClick: () => void; children: React.ReactNode }) {
  return (
    <button
      type="button"
      onClick={onClick}
      style={{
        flex: "0 0 auto",
        padding: "6px 12px",
        borderRadius: 999,
        border: "none",
        background: on ? "var(--fg)" : "var(--surface-2)",
        color: on ? "var(--bg)" : "var(--fg-2)",
        fontSize: 12,
        fontWeight: 600,
        whiteSpace: "nowrap",
        cursor: "pointer",
        fontFamily: "inherit",
      }}
    >
      {children}
    </button>
  );
}
