// /invest (mobile) — market-entry home.
// Route contract: summary-level only. Full holdings live at /invest/my.
import { useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { MobileShell } from "../../mobile/MobileShell";
import { useInvestHome } from "../../hooks/useInvestHome";
import { useAccountPanel } from "../../desktop/useAccountPanel";
import { scopeGroupedToSource } from "../../desktop/scopeHoldings";
import { accountSourceMeta, displayNameWithSource } from "../../desktop/AccountSourceMeta";
import { PL } from "../../ds";
import type { PillTone } from "../../ds";
import { Icon } from "../../ds";
import type { AccountSource, HomeSummary } from "../../types/invest";

function fmtKrw(v: number | null | undefined): string {
  if (v == null) return "—";
  return `₩${Math.round(v).toLocaleString("ko-KR")}`;
}

interface MobileNavCard {
  to: string;
  label: string;
  desc: string;
  icon: React.ReactNode;
}

const NAV_CARDS: MobileNavCard[] = [
  {
    to: "/my",
    label: "내 포트폴리오",
    desc: "보유 종목 전체 목록",
    icon: <Icon name="person" size={18} />,
  },
  {
    to: "/feed/news",
    label: "뉴스",
    desc: "시장 뉴스 피드",
    icon: <Icon name="bell" size={18} />,
  },
  {
    to: "/discover",
    label: "발견",
    desc: "투자 아이디어 탐색",
    icon: <Icon name="flash" size={18} />,
  },
  {
    to: "/insights",
    label: "인사이트",
    desc: "괴리·패리티 read-only 관찰",
    icon: <Icon name="chart" size={18} />,
  },
  {
    to: "/my?tab=signals",
    label: "시그널",
    desc: "AI 분석 신호",
    icon: <Icon name="chart" size={18} />,
  },
  {
    to: "/calendar",
    label: "캘린더",
    desc: "실적·배당 일정",
    icon: <Icon name="calendar" size={18} />,
  },
  {
    to: "/reports",
    label: "투자 리포트",
    desc: "리포트별 액션·와치·리스크 검토",
    icon: <Icon name="flash" size={18} />,
  },
];

export function MobileHomePage() {
  const home = useInvestHome();
  const panel = useAccountPanel();
  const [account, setAccount] = useState<"all" | AccountSource>("all");

  const data = home.state.status === "ready" ? home.state.data : null;

  const scopedGrouped = useMemo(() => {
    if (!data) return [];
    if (account === "all") return data.groupedHoldings;
    return scopeGroupedToSource(data.groupedHoldings, account);
  }, [data, account]);

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

  // Compute per-category totals for the summary breakdown
  const categoryTotals = useMemo(() => {
    const totals: Record<string, number> = {};
    for (const h of scopedGrouped) {
      if (h.valueKrw != null) {
        totals[h.assetCategory] = (totals[h.assetCategory] ?? 0) + h.valueKrw;
      }
    }
    return totals;
  }, [scopedGrouped]);

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
          {/* Summary hero — account totals only, not a full holdings list */}
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

            {/* Per-category summary chips */}
            {Object.keys(categoryTotals).length > 0 && (
              <div style={{ display: "flex", gap: 8, marginTop: 10, flexWrap: "wrap" }}>
                {Object.entries(categoryTotals).map(([cat, val]) => {
                  const label = cat === "kr_stock" ? "한국" : cat === "us_stock" ? "해외" : "코인";
                  return (
                    <div
                      key={cat}
                      style={{
                        padding: "4px 10px",
                        background: "var(--surface-2)",
                        borderRadius: 8,
                        fontSize: 11,
                        fontWeight: 600,
                        color: "var(--fg-2)",
                        fontFeatureSettings: '"tnum"',
                      }}
                    >
                      {label} {fmtKrw(val)}
                    </div>
                  );
                })}
              </div>
            )}
          </section>

          {/* Account selector */}
          {data.accounts.length > 0 && (
            <section style={{ padding: "0 16px" }} data-testid="mobile-account-row">
              <div style={{ display: "flex", gap: 6, overflowX: "auto", paddingBottom: 4 }}>
                <PillButton on={account === "all"} onClick={() => setAccount("all")} tone="accent">
                  전체
                </PillButton>
                {data.accounts.map((a) => {
                  const meta = accountSourceMeta(a.source);
                  return (
                    <PillButton
                      key={a.accountId}
                      on={account === a.source}
                      onClick={() => setAccount(a.source)}
                      tone={meta.tone}
                    >
                      <span>{displayNameWithSource(a)}</span>
                      <span style={{ opacity: 0.72, marginLeft: 4 }}>{meta.badge}</span>
                    </PillButton>
                  );
                })}
              </div>
            </section>
          )}

          {/* Navigation shortcuts — market-entry role, linking to dedicated surfaces */}
          <section style={{ padding: "0 16px" }}>
            <div style={{ fontSize: 12, fontWeight: 600, color: "var(--fg-3)", marginBottom: 8 }}>바로가기</div>
            <div
              data-testid="mobile-home-nav-cards"
              style={{ display: "flex", flexDirection: "column", gap: 6 }}
            >
              {NAV_CARDS.map((card) => (
                <Link
                  key={card.to}
                  to={card.to}
                  style={{ textDecoration: "none" }}
                >
                  <div
                    data-testid="mobile-home-nav-card"
                    style={{
                      display: "flex",
                      alignItems: "center",
                      gap: 12,
                      padding: "12px 14px",
                      background: "var(--surface)",
                      border: "1px solid var(--border)",
                      borderRadius: 12,
                    }}
                  >
                    <div
                      style={{
                        width: 32,
                        height: 32,
                        borderRadius: 9,
                        background: "var(--accent-soft)",
                        color: "var(--accent-press)",
                        display: "grid",
                        placeItems: "center",
                        flexShrink: 0,
                      }}
                    >
                      {card.icon}
                    </div>
                    <div style={{ flex: 1, minWidth: 0 }}>
                      <div style={{ fontSize: 14, fontWeight: 700, color: "var(--fg)" }}>{card.label}</div>
                      <div style={{ fontSize: 11, color: "var(--fg-3)", marginTop: 1 }}>{card.desc}</div>
                    </div>
                    <Icon name="chev" size={16} />
                  </div>
                </Link>
              ))}
            </div>
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

function PillButton({ on, onClick, children, tone = "paper" }: Readonly<{ on: boolean; onClick: () => void; children: React.ReactNode; tone?: PillTone }>) {
  return (
    <button
      type="button"
      onClick={onClick}
      style={{
        flex: "0 0 auto",
        padding: "6px 12px",
        borderRadius: 999,
        border: "none",
        background: on ? `var(--pill-${tone}-fg)` : `var(--pill-${tone}-bg)`,
        color: on ? "var(--bg)" : `var(--pill-${tone}-fg)`,
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
