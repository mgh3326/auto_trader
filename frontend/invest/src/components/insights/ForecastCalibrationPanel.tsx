import { useEffect, useState } from "react";
import { Link } from "react-router-dom";

import {
  fetchClosedForecasts,
  fetchForecastCalibration,
  fetchOpenForecasts,
} from "../../api/forecasts";
import { Card, Pill } from "../../ds";
import { stockDetailPath } from "../../stockDetailPath";
import { formatWatchMoney } from "../my/watchPresentation";
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

// "전체" = omit the days param entirely (the backend has no all-time sentinel;
// days is Query(ge=1)), so the calibration aggregate spans the full history.
const DAYS_OPTIONS: { key: number | "all"; label: string }[] = [
  { key: 30, label: "30일" },
  { key: 90, label: "90일" },
  { key: "all", label: "전체" },
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

const DIRECTION_GLYPH: Record<string, string> = {
  at_or_above: "≥",
  at_or_below: "≤",
};

// forecast_target arrives as an untyped dict (Record<string, unknown>). Narrow
// it and render "≥ ₩80,000"-style text for price_target kinds; fall back to the
// raw kind label for other kinds, or null when absent.
function formatForecastTarget(row: ForecastRow): string | null {
  const t = row.forecast_target;
  if (!t || typeof t !== "object") return null;
  const kind = typeof t.kind === "string" ? t.kind : null;
  if (!kind) return null;
  if (kind === "price_target") {
    const direction = typeof t.direction === "string" ? t.direction : null;
    const price = typeof t.target_price === "number" ? t.target_price : null;
    const market = row.instrument_type ? INSTRUMENT_MARKET[row.instrument_type] : undefined;
    const glyph = direction ? DIRECTION_GLYPH[direction] ?? "" : "";
    const priceText = price != null ? formatWatchMoney(price, market ?? "") : "";
    const text = `${glyph}${glyph && priceText ? " " : ""}${priceText}`.trim();
    return text || kind;
  }
  return kind;
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

type SortKey = "sample_size" | "hit_rate" | "avg_brier_score" | "calibration_gap";

// Client-side sort. key===null preserves the server order (sample_size desc,
// per forecast_service). Nulls always sort last regardless of direction.
function sortRows(
  rows: CalibrationGroupRow[],
  key: SortKey | null,
  dir: "asc" | "desc",
): CalibrationGroupRow[] {
  if (key === null) return rows;
  const factor = dir === "asc" ? 1 : -1;
  return [...rows].sort((a, b) => {
    const av = a[key];
    const bv = b[key];
    if (av == null && bv == null) return 0;
    if (av == null) return 1;
    if (bv == null) return -1;
    return (av - bv) * factor;
  });
}

// Two stacked fill bars: confidence (avg_probability) over actual (hit_rate).
// The length difference is the calibration gap, made visible at a glance.
function CalibrationBar({ confidence, actual }: { confidence: number | null; actual: number | null }) {
  const track: React.CSSProperties = {
    position: "relative",
    width: 88,
    height: 6,
    borderRadius: 3,
    background: "var(--surface-2)",
    overflow: "hidden",
  };
  const clamp = (x: number) => Math.max(0, Math.min(1, x));
  return (
    <div style={{ display: "grid", gap: 3 }} aria-hidden>
      <div style={track} title="확신(평균확신)">
        {confidence != null && (
          <div style={{ position: "absolute", top: 0, bottom: 0, left: 0, width: `${clamp(confidence) * 100}%`, background: "var(--fg-3)" }} />
        )}
      </div>
      <div style={track} title="실제(적중률)">
        {actual != null && (
          <div style={{ position: "absolute", top: 0, bottom: 0, left: 0, width: `${clamp(actual) * 100}%`, background: "var(--accent)" }} />
        )}
      </div>
    </div>
  );
}

const SMALL_SAMPLE = 5;

function CalibrationTable({ rows }: { rows: CalibrationGroupRow[] }) {
  const [sort, setSort] = useState<{ key: SortKey | null; dir: "asc" | "desc" }>({
    key: null,
    dir: "desc",
  });

  if (rows.length === 0) {
    return (
      <div style={{ padding: 16, color: "var(--fg-3)", fontSize: 13, textAlign: "center", lineHeight: 1.6 }}>
        채점 완료된 예측이 아직 없습니다 — forecast_resolve 실행 후 review_date가 지난 closed 예측에서 채워집니다.
      </div>
    );
  }

  const sorted = sortRows(rows, sort.key, sort.dir);

  // Header click cycles desc → asc → server-order (null).
  const toggleSort = (key: SortKey) =>
    setSort((s) => {
      if (s.key !== key) return { key, dir: "desc" };
      if (s.dir === "desc") return { key, dir: "asc" };
      return { key: null, dir: "desc" };
    });

  const sortableTh = (key: SortKey, label: string) => {
    const active = sort.key === key;
    return (
      <th
        style={{ ...th, textAlign: "right", cursor: "pointer", userSelect: "none" }}
        onClick={() => toggleSort(key)}
        aria-sort={active ? (sort.dir === "asc" ? "ascending" : "descending") : "none"}
      >
        {label}
        {active ? (sort.dir === "asc" ? " ▲" : " ▼") : ""}
      </th>
    );
  };

  return (
    <div style={{ overflowX: "auto" }}>
      <table style={{ width: "100%", borderCollapse: "collapse", minWidth: 640 }}>
        <thead>
          <tr style={{ color: "var(--fg-3)", fontSize: 11, textAlign: "left" }}>
            <th style={th}>그룹</th>
            {sortableTh("sample_size", "표본")}
            {sortableTh("hit_rate", "적중률")}
            <th style={{ ...th, textAlign: "right" }}>평균확신</th>
            <th style={{ ...th, textAlign: "center" }}>확신·적중</th>
            {sortableTh("avg_brier_score", "Brier")}
            {sortableTh("calibration_gap", "보정오차")}
          </tr>
        </thead>
        <tbody>
          {sorted.map((r) => {
            const gap = gapText(r.calibration_gap);
            const small = r.sample_size < SMALL_SAMPLE;
            return (
              <tr key={r.group} style={small ? { background: "var(--surface-2)" } : undefined}>
                <td style={{ ...td, fontWeight: 700 }}>
                  <span style={{ display: "inline-flex", gap: 6, alignItems: "center", flexWrap: "wrap" }}>
                    {r.group}
                    {small && <Pill tone="warn" size="sm">n={r.sample_size} 소표본</Pill>}
                  </span>
                </td>
                <td style={tdNum}>
                  {r.sample_size}
                  <span style={{ color: "var(--fg-3)", fontSize: 11 }}> (적중 {r.hits} · 실패 {r.misses})</span>
                </td>
                <td style={tdNum}>{pct(r.hit_rate)}</td>
                <td style={tdNum}>{pct(r.avg_probability)}</td>
                <td style={{ ...td, textAlign: "center" }}>
                  <div style={{ display: "inline-flex" }}>
                    <CalibrationBar confidence={r.avg_probability} actual={r.hit_rate} />
                  </div>
                </td>
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
      {rows.map((r) => {
        const target = formatForecastTarget(r);
        return (
          <div
            key={r.id}
            style={{ display: "flex", alignItems: "center", gap: 8, fontSize: 13, padding: "2px 0", flexWrap: "wrap" }}
          >
            <span style={{ color: "var(--fg-3)", fontSize: 11, minWidth: 84 }}>{r.review_date ?? "—"}</span>
            <span style={{ fontWeight: 700 }}><SymbolCell row={r} /></span>
            <Pill tone="paper" size="sm">확신 {pct(r.probability)}</Pill>
            {target && <span style={{ color: "var(--fg-3)", fontSize: 11 }}>· 목표 {target}</span>}
            {r.horizon && <span style={{ color: "var(--fg-3)", fontSize: 11 }}>· {r.horizon}</span>}
            {r.created_by && <span style={{ color: "var(--fg-3)", fontSize: 11 }}>· {r.created_by}</span>}
          </div>
        );
      })}
    </div>
  );
}

function ClosedList({
  rows,
  linkedCorrelationIds,
}: {
  rows: ForecastRow[];
  linkedCorrelationIds?: ReadonlySet<string>;
}) {
  if (rows.length === 0) {
    return (
      <div style={{ padding: 16, color: "var(--fg-3)", fontSize: 13, textAlign: "center" }}>
        최근 채점 결과가 없습니다.
      </div>
    );
  }
  return (
    <div style={{ display: "grid", gap: 6 }}>
      {rows.map((r) => {
        const target = formatForecastTarget(r);
        const market = r.instrument_type ? INSTRUMENT_MARKET[r.instrument_type] : undefined;
        const linked = r.correlation_id != null && (linkedCorrelationIds?.has(r.correlation_id) ?? false);
        return (
          <div
            key={r.id}
            id={linked ? `forecast-${r.correlation_id}` : undefined}
            style={{ display: "flex", alignItems: "center", gap: 8, fontSize: 13, padding: "2px 0", flexWrap: "wrap" }}
          >
            <Pill tone={r.outcome ? "gain" : "loss"} size="sm">{r.outcome ? "적중" : "빗나감"}</Pill>
            <span style={{ fontWeight: 700 }}><SymbolCell row={r} /></span>
            <span style={{ color: "var(--fg-3)", fontSize: 11 }}>확신 {pct(r.probability)}</span>
            {target && <span style={{ color: "var(--fg-3)", fontSize: 11 }}>· 목표 {target}</span>}
            {r.observed_value != null && (
              <span style={{ color: "var(--fg-3)", fontSize: 11 }}>· 실현 {formatWatchMoney(r.observed_value, market ?? "")}</span>
            )}
            <span style={{ color: "var(--fg-3)", fontSize: 11 }}>· Brier {num(r.brier_score)}</span>
            {r.resolved_at && (
              <span style={{ color: "var(--fg-3)", fontSize: 11 }}>· {r.resolved_at.slice(0, 10)}</span>
            )}
            {linked && (
              <a
                href={`#retro-${r.correlation_id}`}
                style={{ color: "var(--link, #4a9)", textDecoration: "none", fontSize: 11 }}
              >
                · 회고↓
              </a>
            )}
          </div>
        );
      })}
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

export function ForecastCalibrationPanel({
  onEmptyChange,
  onClosedCorrelationIds,
  linkedCorrelationIds,
}: {
  onEmptyChange?: (isEmpty: boolean) => void;
  onClosedCorrelationIds?: (ids: string[]) => void;
  linkedCorrelationIds?: ReadonlySet<string>;
} = {}) {
  const [groupBy, setGroupBy] = useState<ForecastGroupBy>("created_by");
  const [days, setDays] = useState<number | "all">(90);
  const [calib, setCalib] = useState<LoadState<CalibrationGroupRow[]>>({ status: "loading" });
  const [open, setOpen] = useState<LoadState<ForecastRow[]>>({ status: "loading" });
  const [closed, setClosed] = useState<LoadState<ForecastRow[]>>({ status: "loading" });

  useEffect(() => {
    let cancelled = false;
    setCalib({ status: "loading" });
    fetchForecastCalibration({ groupBy, days: days === "all" ? undefined : days })
      .then((d) => { if (!cancelled) setCalib({ status: "ready", data: d.groups }); })
      .catch((e: unknown) => {
        if (!cancelled) setCalib({ status: "error", message: e instanceof Error ? e.message : String(e) });
      });
    return () => { cancelled = true; };
  }, [groupBy, days]);

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

  // Report emptiness to the page (ROB-677 banner): empty only when all three
  // sub-fetches are ready AND empty; loading/error counts as not-empty.
  useEffect(() => {
    if (!onEmptyChange) return;
    const flags = [calib, open, closed].map((s) => (s.status === "ready" ? s.data.length === 0 : null));
    onEmptyChange(flags.every((f) => f === true));
  }, [calib, open, closed, onEmptyChange]);

  // Report closed-forecast correlation_ids so the page can crosslink them to
  // matching retrospectives (ROB-678).
  useEffect(() => {
    if (!onClosedCorrelationIds) return;
    if (closed.status !== "ready") return;
    onClosedCorrelationIds(
      closed.data.map((r) => r.correlation_id).filter((c): c is string => c != null),
    );
  }, [closed, onClosedCorrelationIds]);

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
          <div style={{ display: "flex", flexDirection: "column", gap: 6, alignItems: "flex-end" }}>
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
            <div style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
              {DAYS_OPTIONS.map((o) => (
                <button
                  key={String(o.key)}
                  type="button"
                  onClick={() => setDays(o.key)}
                  style={{
                    border: "none", borderRadius: 999, padding: "4px 10px", fontSize: 11,
                    fontWeight: 700, cursor: "pointer", fontFamily: "inherit",
                    background: days === o.key ? "var(--fg)" : "var(--surface-2)",
                    color: days === o.key ? "var(--bg)" : "var(--fg-2)",
                  }}
                >
                  {o.label}
                </button>
              ))}
            </div>
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
          {closed.status === "ready" && <ClosedList rows={closed.data} linkedCorrelationIds={linkedCorrelationIds} />}
        </Section>
      </section>
    </Card>
  );
}
