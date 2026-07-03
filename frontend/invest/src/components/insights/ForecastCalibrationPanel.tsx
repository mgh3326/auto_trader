import { useEffect, useState } from "react";
import { Link } from "react-router-dom";

import {
  fetchClosedForecasts,
  fetchForecastCalibration,
  fetchOpenForecasts,
} from "../../api/forecasts";
import { Card, Pill } from "../../ds";
import { stockDetailPath } from "../../stockDetailPath";
import type {
  CalibrationGroupRow,
  ForecastGroupBy,
  ForecastRow,
} from "../../types/forecasts";

const GROUP_BY_OPTIONS: { key: ForecastGroupBy; label: string }[] = [
  { key: "created_by", label: "작성자" },
  { key: "model_label", label: "모델" },
  { key: "session_label", label: "세션" },
  { key: "day", label: "일자" },
];

const INSTRUMENT_MARKET: Record<string, "kr" | "us" | "crypto"> = {
  equity_kr: "kr",
  equity_us: "us",
  crypto: "crypto",
};

function symbolHref(row: ForecastRow): string | null {
  const market = row.instrument_type ? INSTRUMENT_MARKET[row.instrument_type] : undefined;
  return market ? stockDetailPath(market, row.symbol) : null;
}

function SymbolCell({ row }: { row: ForecastRow }) {
  const href = symbolHref(row);
  return href ? (
    <Link to={href} style={{ color: "inherit", textDecoration: "none" }}>
      {row.symbol}
    </Link>
  ) : (
    <>{row.symbol}</>
  );
}

function pct(x: number | null): string {
  return x == null ? "—" : `${(x * 100).toFixed(0)}%`;
}

function num(x: number | null, digits = 3): string {
  return x == null ? "—" : x.toFixed(digits);
}

function gapText(x: number | null): { text: string; tone: "gain" | "loss" | "paper" } {
  if (x == null) return { text: "—", tone: "paper" };
  const sign = x > 0 ? "+" : "";
  // positive gap = over-confident (predicted higher than realized) → caution
  const tone = Math.abs(x) < 0.05 ? "paper" : x > 0 ? "loss" : "gain";
  return { text: `${sign}${x.toFixed(3)}`, tone };
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
const tdNum: React.CSSProperties = {
  ...td,
  textAlign: "right",
  fontFeatureSettings: '"tnum"',
};

function CalibrationTable({ rows }: { rows: CalibrationGroupRow[] }) {
  if (rows.length === 0) {
    return (
      <div style={{ padding: 16, color: "var(--fg-3)", fontSize: 13, textAlign: "center" }}>
        채점 완료된 예측이 아직 없습니다.
      </div>
    );
  }
  return (
    <div style={{ overflowX: "auto" }}>
      <table style={{ width: "100%", borderCollapse: "collapse", minWidth: 560 }}>
        <thead>
          <tr style={{ color: "var(--fg-3)", fontSize: 11, textAlign: "left" }}>
            <th style={th}>그룹</th>
            <th style={{ ...th, textAlign: "right" }}>표본</th>
            <th style={{ ...th, textAlign: "right" }}>적중률</th>
            <th style={{ ...th, textAlign: "right" }}>평균확신</th>
            <th style={{ ...th, textAlign: "right" }}>Brier</th>
            <th style={{ ...th, textAlign: "right" }}>보정오차</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => {
            const gap = gapText(r.calibration_gap);
            return (
              <tr key={r.group}>
                <td style={{ ...td, fontWeight: 700 }}>{r.group}</td>
                <td style={tdNum}>
                  {r.sample_size}
                  <span style={{ color: "var(--fg-3)", fontSize: 11 }}> ({r.hits}/{r.sample_size})</span>
                </td>
                <td style={tdNum}>{pct(r.hit_rate)}</td>
                <td style={tdNum}>{pct(r.avg_probability)}</td>
                <td style={tdNum}>{num(r.avg_brier_score)}</td>
                <td style={{ ...tdNum }}>
                  <Pill tone={gap.tone} size="sm">{gap.text}</Pill>
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

function OpenList({ rows }: { rows: ForecastRow[] }) {
  if (rows.length === 0) {
    return (
      <div style={{ padding: 16, color: "var(--fg-3)", fontSize: 13, textAlign: "center" }}>
        대기 중인 예측이 없습니다.
      </div>
    );
  }
  return (
    <div style={{ display: "grid", gap: 6 }}>
      {rows.map((r) => (
        <div
          key={r.id}
          style={{ display: "flex", alignItems: "center", gap: 8, fontSize: 13, padding: "2px 0" }}
        >
          <span style={{ color: "var(--fg-3)", fontSize: 11, minWidth: 84 }}>{r.review_date ?? "—"}</span>
          <span style={{ fontWeight: 700 }}><SymbolCell row={r} /></span>
          <Pill tone="paper" size="sm">확신 {pct(r.probability)}</Pill>
          {r.created_by && <span style={{ color: "var(--fg-3)", fontSize: 11 }}>· {r.created_by}</span>}
        </div>
      ))}
    </div>
  );
}

function ClosedList({ rows }: { rows: ForecastRow[] }) {
  if (rows.length === 0) {
    return (
      <div style={{ padding: 16, color: "var(--fg-3)", fontSize: 13, textAlign: "center" }}>
        최근 채점 결과가 없습니다.
      </div>
    );
  }
  return (
    <div style={{ display: "grid", gap: 6 }}>
      {rows.map((r) => (
        <div
          key={r.id}
          style={{ display: "flex", alignItems: "center", gap: 8, fontSize: 13, padding: "2px 0" }}
        >
          <Pill tone={r.outcome ? "gain" : "loss"} size="sm">{r.outcome ? "적중" : "빗나감"}</Pill>
          <span style={{ fontWeight: 700 }}><SymbolCell row={r} /></span>
          <span style={{ color: "var(--fg-3)", fontSize: 11 }}>확신 {pct(r.probability)}</span>
          <span style={{ color: "var(--fg-3)", fontSize: 11 }}>· Brier {num(r.brier_score)}</span>
          {r.resolved_at && (
            <span style={{ color: "var(--fg-3)", fontSize: 11 }}>· {r.resolved_at.slice(0, 10)}</span>
          )}
        </div>
      ))}
    </div>
  );
}

type LoadState<T> =
  | { status: "loading" }
  | { status: "ready"; data: T }
  | { status: "error"; message: string };

function Section({ title, hint, children }: { title: string; hint?: string; children: React.ReactNode }) {
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

export function ForecastCalibrationPanel() {
  const [groupBy, setGroupBy] = useState<ForecastGroupBy>("created_by");
  const [calib, setCalib] = useState<LoadState<CalibrationGroupRow[]>>({ status: "loading" });
  const [open, setOpen] = useState<LoadState<ForecastRow[]>>({ status: "loading" });
  const [closed, setClosed] = useState<LoadState<ForecastRow[]>>({ status: "loading" });

  useEffect(() => {
    let cancelled = false;
    setCalib({ status: "loading" });
    fetchForecastCalibration({ groupBy })
      .then((d) => { if (!cancelled) setCalib({ status: "ready", data: d.groups }); })
      .catch((e: unknown) => {
        if (!cancelled) setCalib({ status: "error", message: e instanceof Error ? e.message : String(e) });
      });
    return () => { cancelled = true; };
  }, [groupBy]);

  useEffect(() => {
    let cancelled = false;
    fetchOpenForecasts({ limit: 20 })
      .then((d) => { if (!cancelled) setOpen({ status: "ready", data: d.items }); })
      .catch((e: unknown) => {
        if (!cancelled) setOpen({ status: "error", message: e instanceof Error ? e.message : String(e) });
      });
    fetchClosedForecasts({ limit: 20 })
      .then((d) => { if (!cancelled) setClosed({ status: "ready", data: d.items }); })
      .catch((e: unknown) => {
        if (!cancelled) setClosed({ status: "error", message: e instanceof Error ? e.message : String(e) });
      });
    return () => { cancelled = true; };
  }, []);

  return (
    <Card>
      <section data-testid="forecast-calibration-panel" style={{ display: "grid", gap: 16 }}>
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start", gap: 12, flexWrap: "wrap" }}>
          <div>
            <h2 style={{ margin: 0, fontSize: 18 }}>예측 캘리브레이션</h2>
            <p style={{ margin: "4px 0 0", fontSize: 12, color: "var(--fg-3)" }}>
              어떤 모델·세션의 판단을 얼마나 믿을 수 있는지 — resolvable 예측의 Brier·적중률·보정오차.
            </p>
          </div>
          <div style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
            {GROUP_BY_OPTIONS.map((o) => (
              <button
                key={o.key}
                type="button"
                onClick={() => setGroupBy(o.key)}
                style={{
                  border: "none", borderRadius: 999, padding: "4px 10px", fontSize: 11,
                  fontWeight: 700, cursor: "pointer", fontFamily: "inherit",
                  background: groupBy === o.key ? "var(--fg)" : "var(--surface-2)",
                  color: groupBy === o.key ? "var(--bg)" : "var(--fg-2)",
                }}
              >
                {o.label}
              </button>
            ))}
          </div>
        </div>

        <Section title="그룹별 신뢰도" hint="양수 보정오차 = 과신(예측이 실현보다 높음).">
          {calib.status === "loading" && <div style={{ padding: 16, color: "var(--fg-3)", fontSize: 13, textAlign: "center" }}>불러오는 중…</div>}
          {calib.status === "error" && <div role="alert" style={{ padding: 12, color: "var(--danger)", fontSize: 13 }}>캘리브레이션을 불러오지 못했습니다. {calib.message}</div>}
          {calib.status === "ready" && <CalibrationTable rows={calib.data} />}
        </Section>

        <Section title="채점 대기열" hint="review_date 임박순 — 채점이 필요한 열린 예측.">
          {open.status === "loading" && <div style={{ padding: 16, color: "var(--fg-3)", fontSize: 13, textAlign: "center" }}>불러오는 중…</div>}
          {open.status === "error" && <div role="alert" style={{ padding: 12, color: "var(--danger)", fontSize: 13 }}>대기열을 불러오지 못했습니다. {open.message}</div>}
          {open.status === "ready" && <OpenList rows={open.data} />}
        </Section>

        <Section title="최근 채점 결과" hint="가장 최근에 해소된 예측.">
          {closed.status === "loading" && <div style={{ padding: 16, color: "var(--fg-3)", fontSize: 13, textAlign: "center" }}>불러오는 중…</div>}
          {closed.status === "error" && <div role="alert" style={{ padding: 12, color: "var(--danger)", fontSize: 13 }}>채점 결과를 불러오지 못했습니다. {closed.message}</div>}
          {closed.status === "ready" && <ClosedList rows={closed.data} />}
        </Section>
      </section>
    </Card>
  );
}
