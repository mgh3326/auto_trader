// ROB-691 — judgment scoreboard (win-rate / realized-PnL / win-loss), the
// existing deterministic `build_retrospective_aggregate` surfaced on the web
// via GET /trading/api/invest/retrospectives/scoreboard. Style mirrors
// ForecastCalibrationPanel (Card + Section + Pill + chip toggles + LoadState).
import { useEffect, useState } from "react";

import { fetchScoreboard } from "../../api/scoreboard";
import { Card, Pill } from "../../ds";
import type {
  ScoreboardGroupBy,
  ScoreboardGroupRow,
  ScoreboardTotals,
} from "../../types/scoreboard";

type ScoreboardMarket = "all" | "kr" | "us" | "crypto";

const MARKET_OPTIONS: { key: ScoreboardMarket; label: string }[] = [
  { key: "all", label: "전체" },
  { key: "kr", label: "국내" },
  { key: "us", label: "미국" },
  { key: "crypto", label: "코인" },
];

// "전체" omits the date-range params entirely, spanning full history — same
// convention as ForecastCalibrationPanel's DAYS_OPTIONS.
const DAYS_OPTIONS: { key: number | "all"; label: string }[] = [
  { key: 30, label: "30일" },
  { key: 90, label: "90일" },
  { key: "all", label: "전체" },
];

const GROUP_BY_OPTIONS: { key: ScoreboardGroupBy; label: string }[] = [
  { key: "strategy", label: "전략" },
  { key: "day", label: "일자" },
  { key: "trigger_type", label: "트리거" },
  { key: "root_cause", label: "원인" },
];

// Process dims (trigger_type/root_cause) include no-fill-evidence rows
// (see build_retrospective_aggregate's include_no_evidence) — win/loss there
// is diluted, advisory-only.
const PROCESS_GROUP_BYS = new Set<ScoreboardGroupBy>(["trigger_type", "root_cause"]);

const SMALL_SAMPLE = 5;

function kstDateString(daysAgo: number): string {
  const d = new Date(Date.now() - daysAgo * 86400000);
  return new Intl.DateTimeFormat("sv-SE", { timeZone: "Asia/Seoul" }).format(d);
}

function pct(x: number | null): string {
  return x == null ? "—" : `${x.toFixed(1)}%`;
}

function formatSignedCurrency(amount: number, currency: string): string {
  const sign = amount > 0 ? "+" : amount < 0 ? "-" : "";
  const abs = Math.abs(amount);
  if (currency === "USD") {
    return `${sign}$${abs.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
  }
  if (currency === "KRW") {
    return `${sign}₩${Math.round(abs).toLocaleString("ko-KR")}`;
  }
  return `${sign}${abs.toLocaleString("ko-KR")} ${currency}`;
}

function currencyColor(amount: number): string {
  if (amount > 0) return "var(--gain)";
  if (amount < 0) return "var(--danger)";
  return "var(--fg-2)";
}

type LoadState<T> =
  | { status: "loading" }
  | { status: "ready"; data: T }
  | { status: "error"; message: string };

function Section({
  title,
  hint,
  children,
}: {
  title: string;
  hint?: string;
  children: React.ReactNode;
}) {
  return (
    <div style={{ display: "grid", gap: 8 }}>
      <div>
        <h3 style={{ margin: 0, fontSize: 15 }}>{title}</h3>
        {hint && <p style={{ margin: "2px 0 0", fontSize: 12, color: "var(--fg-3)" }}>{hint}</p>}
      </div>
      {children}
    </div>
  );
}

function ChipRow<T extends string | number>({
  options,
  value,
  onChange,
}: {
  options: { key: T; label: string }[];
  value: T;
  onChange: (key: T) => void;
}) {
  return (
    <div style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
      {options.map((o) => (
        <button
          key={String(o.key)}
          type="button"
          onClick={() => onChange(o.key)}
          style={{
            border: "none",
            borderRadius: 999,
            padding: "4px 10px",
            fontSize: 11,
            fontWeight: 700,
            cursor: "pointer",
            fontFamily: "inherit",
            background: value === o.key ? "var(--fg)" : "var(--surface-2)",
            color: value === o.key ? "var(--bg)" : "var(--fg-2)",
          }}
        >
          {o.label}
        </button>
      ))}
    </div>
  );
}

function RealizedPnlRows({ sums }: { sums: Record<string, number> }) {
  const entries = Object.entries(sums);
  if (entries.length === 0) {
    return <span style={{ color: "var(--fg-3)" }}>—</span>;
  }
  return (
    <div style={{ display: "grid", gap: 2 }}>
      {entries.map(([currency, amount]) => (
        <span
          key={currency}
          style={{ fontWeight: 900, fontFeatureSettings: '"tnum"', color: currencyColor(amount) }}
        >
          {formatSignedCurrency(amount, currency)}
        </span>
      ))}
    </div>
  );
}

const tile: React.CSSProperties = {
  borderRadius: 12,
  background: "var(--surface-2)",
  padding: "10px 12px",
  display: "grid",
  gap: 4,
  minWidth: 0,
};
const tileLabel: React.CSSProperties = {
  fontSize: 11,
  color: "var(--fg-3)",
  fontWeight: 700,
  display: "flex",
  alignItems: "center",
  gap: 6,
  flexWrap: "wrap",
};
const tileValue: React.CSSProperties = { fontSize: 20, fontWeight: 900, fontFeatureSettings: '"tnum"' };
const tileHint: React.CSSProperties = { fontSize: 11, color: "var(--fg-3)" };

function HeadlineTiles({ totals }: { totals: ScoreboardTotals }) {
  const small = totals.decided > 0 && totals.decided < SMALL_SAMPLE;
  return (
    <div
      data-testid="scoreboard-headline"
      style={{
        display: "grid",
        gridTemplateColumns: "repeat(auto-fit, minmax(140px, 1fr))",
        gap: 10,
      }}
    >
      <div style={tile}>
        <div style={tileLabel}>
          승률
          {small && <Pill tone="warn" size="sm">소표본</Pill>}
        </div>
        <div style={tileValue}>{pct(totals.win_rate_pct)}</div>
        <div style={tileHint}>결정 {totals.decided}건 중</div>
      </div>

      <div style={tile}>
        <div style={tileLabel}>승/패</div>
        <div style={{ display: "flex", gap: 6, alignItems: "center" }}>
          <Pill tone="gain">{totals.wins}승</Pill>
          <Pill tone="loss">{totals.misses}패</Pill>
        </div>
      </div>

      <div style={tile}>
        <div style={tileLabel}>결정 표본</div>
        <div style={tileValue}>{totals.decided}</div>
        {totals.excluded_no_fill_evidence > 0 && (
          <div style={tileHint}>증거 부족 {totals.excluded_no_fill_evidence}건 제외</div>
        )}
      </div>

      <div style={tile}>
        <div style={tileLabel}>실현손익</div>
        <RealizedPnlRows sums={totals.realized_pnl_sum} />
      </div>
    </div>
  );
}

const th: React.CSSProperties = {
  padding: "8px 12px",
  borderTop: "1px solid var(--divider)",
  borderBottom: "1px solid var(--divider)",
  fontWeight: 700,
};
const td: React.CSSProperties = {
  padding: "9px 12px",
  borderBottom: "1px solid var(--divider)",
  fontSize: 13,
};
const tdNum: React.CSSProperties = { ...td, textAlign: "right", fontFeatureSettings: '"tnum"' };

function BreakdownTable({
  rows,
  groupBy,
}: {
  rows: ScoreboardGroupRow[];
  groupBy: ScoreboardGroupBy;
}) {
  if (rows.length === 0) {
    return (
      <div style={{ padding: 16, color: "var(--fg-3)", fontSize: 13, textAlign: "center" }}>
        표시할 그룹이 없습니다.
      </div>
    );
  }
  return (
    <div data-testid="scoreboard-breakdown" style={{ display: "grid", gap: 8 }}>
      {PROCESS_GROUP_BYS.has(groupBy) && (
        <Pill tone="paper" size="sm">무증거 포함, 승률 참고용</Pill>
      )}
      <div style={{ overflowX: "auto" }}>
        <table style={{ width: "100%", borderCollapse: "collapse", minWidth: 560 }}>
          <thead>
            <tr style={{ color: "var(--fg-3)", fontSize: 11, textAlign: "left" }}>
              <th style={th}>그룹</th>
              <th style={{ ...th, textAlign: "right" }}>표본</th>
              <th style={{ ...th, textAlign: "right" }}>승/패</th>
              <th style={{ ...th, textAlign: "right" }}>승률</th>
              <th style={{ ...th, textAlign: "right" }}>실현손익</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => {
              const small = r.sample_size < SMALL_SAMPLE;
              return (
                <tr key={r.group} style={small ? { background: "var(--surface-2)" } : undefined}>
                  <td style={{ ...td, fontWeight: 700 }}>
                    <span style={{ display: "inline-flex", gap: 6, alignItems: "center", flexWrap: "wrap" }}>
                      {r.group}
                      {small && <Pill tone="warn" size="sm">n={r.sample_size} 소표본</Pill>}
                    </span>
                  </td>
                  <td style={tdNum}>{r.sample_size}</td>
                  <td style={tdNum}>
                    {r.wins}승 · {r.misses}패
                  </td>
                  <td style={tdNum}>{pct(r.win_rate_pct)}</td>
                  <td style={tdNum}>
                    <RealizedPnlRows sums={r.realized_pnl_sum} />
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </div>
  );
}

export function JudgmentScoreboardPanel({
  compact = false,
  onEmptyChange,
}: {
  compact?: boolean;
  onEmptyChange?: (isEmpty: boolean) => void;
} = {}) {
  const [market, setMarket] = useState<ScoreboardMarket>("all");
  const [days, setDays] = useState<number | "all">(90);
  const [groupBy, setGroupBy] = useState<ScoreboardGroupBy>("strategy");
  const [headline, setHeadline] = useState<LoadState<ScoreboardTotals>>({ status: "loading" });
  const [breakdown, setBreakdown] = useState<LoadState<ScoreboardGroupRow[]>>({ status: "loading" });

  const dateFrom = days === "all" ? undefined : kstDateString(days);
  const dateTo = days === "all" ? undefined : kstDateString(0);

  // Headline tile is always the strategy/day-oriented PnL grouping regardless
  // of the breakdown toggle below (plan §3.4/§4 — totals from trigger_type/
  // root_cause would be diluted by no-fill-evidence rows).
  useEffect(() => {
    let cancelled = false;
    setHeadline({ status: "loading" });
    fetchScoreboard({ groupBy: "strategy", market, dateFrom, dateTo })
      .then((d) => {
        if (!cancelled) setHeadline({ status: "ready", data: d.totals });
      })
      .catch((e: unknown) => {
        if (!cancelled) setHeadline({ status: "error", message: e instanceof Error ? e.message : String(e) });
      });
    return () => {
      cancelled = true;
    };
  }, [market, dateFrom, dateTo]);

  useEffect(() => {
    let cancelled = false;
    setBreakdown({ status: "loading" });
    fetchScoreboard({ groupBy, market, dateFrom, dateTo })
      .then((d) => {
        if (!cancelled) setBreakdown({ status: "ready", data: d.groups });
      })
      .catch((e: unknown) => {
        if (!cancelled) setBreakdown({ status: "error", message: e instanceof Error ? e.message : String(e) });
      });
    return () => {
      cancelled = true;
    };
  }, [groupBy, market, dateFrom, dateTo]);

  useEffect(() => {
    if (!onEmptyChange) return;
    if (headline.status !== "ready") return;
    onEmptyChange(headline.data.sample_size === 0);
  }, [headline, onEmptyChange]);

  return (
    <Card>
      <section data-testid="judgment-scoreboard-panel" style={{ display: "grid", gap: 16 }}>
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start", gap: 12, flexWrap: "wrap" }}>
          <div>
            <h2 style={{ margin: 0, fontSize: compact ? 16 : 18 }}>판단 성적표</h2>
            <p style={{ margin: "4px 0 0", fontSize: 12, color: "var(--fg-3)", lineHeight: 1.6, maxWidth: 520 }}>
              체결·증거 기반 — 실제 체결과 PnL 증거가 있는 회고만 집계합니다(무증거·거부·취소
              제외). 자기신고보다 강하지만 표본이 적을 수 있습니다.
            </p>
          </div>
          <div style={{ display: "flex", flexDirection: "column", gap: 6, alignItems: "flex-end" }}>
            <ChipRow options={MARKET_OPTIONS} value={market} onChange={setMarket} />
            <ChipRow options={DAYS_OPTIONS} value={days} onChange={setDays} />
          </div>
        </div>

        <Section title="핵심 지표">
          {headline.status === "loading" && (
            <div style={{ padding: 16, color: "var(--fg-3)", fontSize: 13, textAlign: "center" }}>불러오는 중…</div>
          )}
          {headline.status === "error" && (
            <div role="alert" style={{ padding: 12, color: "var(--danger)", fontSize: 13 }}>
              성적표를 불러오지 못했습니다. {headline.message}
            </div>
          )}
          {headline.status === "ready" && <HeadlineTiles totals={headline.data} />}
        </Section>

        <Section
          title="그룹별 상세"
          hint="승률 정의: 실현손익 > 0 기준(동점은 패로 집계)."
        >
          <ChipRow options={GROUP_BY_OPTIONS} value={groupBy} onChange={setGroupBy} />
          {breakdown.status === "loading" && (
            <div style={{ padding: 16, color: "var(--fg-3)", fontSize: 13, textAlign: "center" }}>불러오는 중…</div>
          )}
          {breakdown.status === "error" && (
            <div role="alert" style={{ padding: 12, color: "var(--danger)", fontSize: 13 }}>
              그룹별 상세를 불러오지 못했습니다. {breakdown.message}
            </div>
          )}
          {breakdown.status === "ready" && (
            <BreakdownTable rows={compact ? breakdown.data.slice(0, 8) : breakdown.data} groupBy={groupBy} />
          )}
        </Section>
      </section>
    </Card>
  );
}
