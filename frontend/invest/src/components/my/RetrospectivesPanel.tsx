import { useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";

import { fetchOpenNextActions, fetchRetrospectives } from "../../api/retrospectives";
import { Pill } from "../../ds";
import { crosslinkAnchorSlug, crosslinkKey, retroMarket } from "../../insightsCrosslink";
import { stockDetailPath } from "../../stockDetailPath";
import type {
  NextActionRow,
  RetroMarket,
  RetroOutcomeFilter,
  RetrospectiveRow,
} from "../../types/retrospectives";

const MARKET_OPTIONS: { key: RetroMarket; label: string }[] = [
  { key: "all", label: "전체" },
  { key: "kr", label: "국내" },
  { key: "us", label: "미국" },
  { key: "crypto", label: "코인" },
];

const TRIGGER_OPTIONS: { key: string; label: string }[] = [
  { key: "", label: "전체 트리거" },
  { key: "fill", label: "체결" },
  { key: "partial_fill", label: "부분체결" },
  { key: "rejected_order", label: "거부" },
  { key: "cancelled", label: "취소" },
  { key: "expired", label: "만료" },
  { key: "thesis_change", label: "논지변경" },
  { key: "policy_violation", label: "정책위반" },
  { key: "stale_evidence", label: "증거부족" },
  { key: "guardrail_block", label: "가드레일" },
];

// ROB-691 — win/loss/decided trade-history filter, forwarded as
// outcome_filter. "" = no filter (all outcomes, decided or not).
const OUTCOME_OPTIONS: { key: RetroOutcomeFilter | ""; label: string }[] = [
  { key: "", label: "전체" },
  { key: "win", label: "승" },
  { key: "loss", label: "패" },
  { key: "decided", label: "결정" },
];

// Debounce the free-text symbol search so every keystroke doesn't fire a
// request; 300ms mirrors common UI debounce defaults elsewhere in the app.
const SEARCH_DEBOUNCE_MS = 300;

function pnlText(row: { realized_pnl: number | null; realized_pnl_currency: string | null }): string {
  if (row.realized_pnl == null) return "—";
  const sign = row.realized_pnl > 0 ? "+" : "";
  return `${sign}${row.realized_pnl.toLocaleString("ko-KR")} ${row.realized_pnl_currency ?? ""}`.trim();
}

function NextActionChecklist({ items }: { items: NextActionRow[] }) {
  if (items.length === 0) return null;
  return (
    <div
      data-testid="retro-next-actions"
      style={{ margin: "0 14px 12px", padding: "10px 12px", borderRadius: 12, background: "var(--surface-2)" }}
    >
      <div style={{ fontSize: 12, fontWeight: 800, marginBottom: 6 }}>
        미완료 액션 ({items.length})
      </div>
      <div style={{ display: "grid", gap: 6 }}>
        {items.map((a, idx) => {
          const href = a.market ? stockDetailPath(a.market as "kr" | "us" | "crypto", a.symbol) : null;
          const sym = (
            <span style={{ fontWeight: 700 }}>
              {href ? <Link to={href} style={{ color: "inherit", textDecoration: "none" }}>{a.symbol}</Link> : a.symbol}
            </span>
          );
          return (
            <div key={`${a.retro_id}-${idx}`} style={{ display: "flex", gap: 8, alignItems: "center", fontSize: 13 }}>
              <Pill tone={a.status === "in_progress" ? "accent" : "paper"} size="sm">
                {a.status === "in_progress" ? "진행중" : "예정"}
              </Pill>
              <span>{a.action}</span>
              <span style={{ color: "var(--fg-3)", fontSize: 11 }}>· {sym}</span>
              {a.due_kst_date && <span style={{ color: "var(--fg-3)", fontSize: 11 }}>· {a.due_kst_date}</span>}
            </div>
          );
        })}
      </div>
    </div>
  );
}

export function RetrospectivesPanel({
  compact = false,
  onSymbolKeys,
  linkedSymbolKeys,
}: {
  compact?: boolean;
  onSymbolKeys?: (keys: string[]) => void;
  linkedSymbolKeys?: ReadonlySet<string>;
}) {
  const [market, setMarket] = useState<RetroMarket>("all");
  const [triggerType, setTriggerType] = useState<string>("");
  const [outcomeFilter, setOutcomeFilter] = useState<RetroOutcomeFilter | "">("");
  // Raw input vs. debounced value: the raw value drives the <input>, the
  // debounced value drives the fetch (ROB-691 — avoid firing a request per keystroke).
  const [symbolSearchInput, setSymbolSearchInput] = useState("");
  const [symbolSearch, setSymbolSearch] = useState("");
  const [dateFrom, setDateFrom] = useState("");
  const [dateTo, setDateTo] = useState("");
  const [nextActions, setNextActions] = useState<NextActionRow[]>([]);
  const [state, setState] = useState<
    | { status: "loading" }
    | { status: "ready"; items: RetrospectiveRow[]; total: number }
    | { status: "error"; message: string }
  >({ status: "loading" });

  useEffect(() => {
    const timer = setTimeout(() => setSymbolSearch(symbolSearchInput.trim()), SEARCH_DEBOUNCE_MS);
    return () => clearTimeout(timer);
  }, [symbolSearchInput]);

  useEffect(() => {
    let cancelled = false;
    setState({ status: "loading" });
    fetchRetrospectives({
      market,
      triggerType: triggerType || undefined,
      outcomeFilter: outcomeFilter || undefined,
      q: symbolSearch || undefined,
      dateFrom: dateFrom || undefined,
      dateTo: dateTo || undefined,
      limit: compact ? 8 : 50,
    })
      .then((data) => {
        if (!cancelled) setState({ status: "ready", items: data.items, total: data.total });
      })
      .catch((err: unknown) => {
        if (!cancelled) setState({ status: "error", message: err instanceof Error ? err.message : String(err) });
      });
    return () => { cancelled = true; };
  }, [market, triggerType, outcomeFilter, symbolSearch, dateFrom, dateTo, compact]);

  useEffect(() => {
    let cancelled = false;
    fetchOpenNextActions(market)
      .then((data) => { if (!cancelled) setNextActions(data.items); })
      .catch(() => { if (!cancelled) setNextActions([]); });
    return () => { cancelled = true; };
  }, [market]);

  // Report retrospective symbol keys so a host page can crosslink them to
  // matching closed forecasts (ROB-682 — re-keyed from correlation_id, which
  // was structurally dead: forecast/retro id namespaces never overlap).
  // No-op off /insights (prop undefined) — /my never passes this prop.
  useEffect(() => {
    if (!onSymbolKeys) return;
    if (state.status !== "ready") return;
    const keys = state.items
      .map((r) => crosslinkKey(retroMarket(r.market, r.instrument_type), r.symbol))
      .filter((k): k is string => k != null);
    onSymbolKeys(Array.from(new Set(keys)));
  }, [state, onSymbolKeys]);

  const rows = useMemo(() => (state.status === "ready" ? state.items : []), [state]);

  return (
    <section
      data-testid="retrospectives-panel"
      style={{ border: "1px solid var(--border)", borderRadius: 16, background: "var(--surface)", overflow: "hidden" }}
    >
      <div style={{ display: "flex", alignItems: compact ? "flex-start" : "center", justifyContent: "space-between", gap: 12, padding: compact ? "14px 14px 10px" : "16px 18px 12px", flexDirection: compact ? "column" : "row" }}>
        <div>
          <h2 style={{ margin: 0, fontSize: compact ? 16 : 18 }}>매매 회고</h2>
          <p style={{ margin: "4px 0 0", fontSize: 12, color: "var(--fg-3)" }}>
            체결·회고에서 도출한 교훈과 미완료 액션을 확인합니다.
          </p>
        </div>
        <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
          <div style={{ display: "flex", gap: 6, flexWrap: "wrap", alignSelf: "flex-end" }}>
            {MARKET_OPTIONS.map((option) => (
              <button key={option.key} type="button" onClick={() => setMarket(option.key)}
                style={{ border: "none", borderRadius: 999, padding: "4px 8px", fontSize: 11, fontWeight: 700, cursor: "pointer", fontFamily: "inherit", background: market === option.key ? "var(--fg)" : "var(--surface-2)", color: market === option.key ? "var(--bg)" : "var(--fg-2)" }}>
                {option.label}
              </button>
            ))}
          </div>
          {/* ROB-691 — win/loss/decided filter: small footprint like the
              market chips above, so it stays visible in compact mode too
              (unlike the wider trigger/search/date-range controls below). */}
          <div style={{ display: "flex", gap: 6, flexWrap: "wrap", alignSelf: "flex-end" }}>
            {OUTCOME_OPTIONS.map((option) => (
              <button key={option.key || "all"} type="button" onClick={() => setOutcomeFilter(option.key)}
                style={{ border: "none", borderRadius: 999, padding: "4px 8px", fontSize: 11, fontWeight: 700, cursor: "pointer", fontFamily: "inherit", background: outcomeFilter === option.key ? "var(--fg)" : "var(--surface-2)", color: outcomeFilter === option.key ? "var(--bg)" : "var(--fg-2)" }}>
                {option.label}
              </button>
            ))}
          </div>
          {!compact && (
            <div style={{ display: "flex", gap: 6, flexWrap: "wrap", alignSelf: "flex-end" }}>
              {TRIGGER_OPTIONS.map((option) => (
                <button key={option.key || "all"} type="button" onClick={() => setTriggerType(option.key)}
                  style={{ border: "none", borderRadius: 999, padding: "4px 8px", fontSize: 11, fontWeight: 700, cursor: "pointer", fontFamily: "inherit", background: triggerType === option.key ? "var(--fg)" : "var(--surface-2)", color: triggerType === option.key ? "var(--bg)" : "var(--fg-2)" }}>
                  {option.label}
                </button>
              ))}
            </div>
          )}
          {!compact && (
            <div style={{ display: "flex", gap: 6, flexWrap: "wrap", alignSelf: "flex-end", alignItems: "center" }}>
              <input
                type="text"
                placeholder="종목 검색"
                value={symbolSearchInput}
                onChange={(e) => setSymbolSearchInput(e.target.value)}
                style={{ border: "1px solid var(--border)", borderRadius: 999, padding: "4px 10px", fontSize: 11, fontFamily: "inherit", background: "var(--surface)", color: "var(--fg-1)", width: 100 }}
              />
              <label style={{ display: "flex", alignItems: "center", gap: 4, fontSize: 11, color: "var(--fg-3)" }}>
                시작일
                <input
                  type="date"
                  aria-label="시작일"
                  value={dateFrom}
                  onChange={(e) => setDateFrom(e.target.value)}
                  style={{ border: "1px solid var(--border)", borderRadius: 8, padding: "3px 6px", fontSize: 11, fontFamily: "inherit", background: "var(--surface)", color: "var(--fg-1)" }}
                />
              </label>
              <label style={{ display: "flex", alignItems: "center", gap: 4, fontSize: 11, color: "var(--fg-3)" }}>
                종료일
                <input
                  type="date"
                  aria-label="종료일"
                  value={dateTo}
                  onChange={(e) => setDateTo(e.target.value)}
                  style={{ border: "1px solid var(--border)", borderRadius: 8, padding: "3px 6px", fontSize: 11, fontFamily: "inherit", background: "var(--surface)", color: "var(--fg-1)" }}
                />
              </label>
            </div>
          )}
        </div>
      </div>

      <NextActionChecklist items={nextActions} />

      {state.status === "loading" && (
        <div style={{ padding: 24, color: "var(--fg-3)", fontSize: 13, textAlign: "center" }}>회고를 불러오는 중…</div>
      )}
      {state.status === "error" && (
        <div role="alert" style={{ padding: 16, color: "var(--danger)", fontSize: 13 }}>
          회고를 불러오지 못했습니다. {state.message}
        </div>
      )}
      {state.status === "ready" && rows.length === 0 && (
        <div style={{ padding: 24, color: "var(--fg-3)", fontSize: 13, textAlign: "center" }}>등록된 회고가 없습니다.</div>
      )}
      {state.status === "ready" && rows.length > 0 && (
        <div style={{ overflowX: "auto" }}>
          <table style={{ width: "100%", borderCollapse: "collapse", minWidth: compact ? 0 : 720 }}>
            <thead>
              <tr style={{ color: "var(--fg-3)", fontSize: 11, textAlign: "left" }}>
                <th style={{ padding: "8px 14px", borderTop: "1px solid var(--divider)", borderBottom: "1px solid var(--divider)" }}>종목</th>
                <th style={{ padding: "8px 14px", borderTop: "1px solid var(--divider)", borderBottom: "1px solid var(--divider)" }}>트리거 / 원인</th>
                <th style={{ padding: "8px 14px", borderTop: "1px solid var(--divider)", borderBottom: "1px solid var(--divider)", textAlign: "right" }}>실현손익</th>
                <th style={{ padding: "8px 14px", borderTop: "1px solid var(--divider)", borderBottom: "1px solid var(--divider)" }}>교훈</th>
              </tr>
            </thead>
            <tbody>
              {(() => {
                // Anchor ids are keyed by symbol, so a symbol with multiple
                // retrospectives would otherwise emit duplicate DOM ids. Only
                // the first matching row per key gets the anchor; the
                // crosslink `<a>` still renders on every match.
                const anchored = new Set<string>();
                return rows.map((row) => {
                  const href = row.market ? stockDetailPath(row.market as "kr" | "us" | "crypto", row.symbol) : null;
                  const key = crosslinkKey(retroMarket(row.market, row.instrument_type), row.symbol);
                  const slug = key != null ? crosslinkAnchorSlug(key) : null;
                  const linked = key != null && (linkedSymbolKeys?.has(key) ?? false);
                  const anchorId = linked && key != null && slug != null && !anchored.has(key)
                    ? `retro-${slug}`
                    : undefined;
                  if (anchorId != null && key != null) anchored.add(key);
                  return (
                    <tr key={row.id} id={anchorId}>
                      <td style={{ padding: "10px 14px", borderBottom: "1px solid var(--divider)", fontSize: 13, fontWeight: 700 }}>
                        {href ? <Link to={href} style={{ color: "inherit", textDecoration: "none" }}>{row.symbol}</Link> : row.symbol}
                      </td>
                      <td style={{ padding: "10px 14px", borderBottom: "1px solid var(--divider)", fontSize: 12 }}>
                        <div style={{ display: "flex", gap: 4, flexWrap: "wrap" }}>
                          {row.trigger_type && <Pill tone="paper" size="sm">{row.trigger_type}</Pill>}
                          {row.root_cause_class && <Pill tone="paper" size="sm">{row.root_cause_class}</Pill>}
                        </div>
                      </td>
                      <td style={{ padding: "10px 14px", borderBottom: "1px solid var(--divider)", fontSize: 13, textAlign: "right", fontFeatureSettings: '"tnum"' }}>
                        {pnlText(row)}
                      </td>
                      <td style={{ padding: "10px 14px", borderBottom: "1px solid var(--divider)", fontSize: 12, color: "var(--fg-2)", maxWidth: 320 }}>
                        {row.lesson ?? row.result_summary ?? "—"}
                        {linked && slug != null && (
                          <a
                            href={`#forecast-${slug}`}
                            style={{ marginLeft: 8, color: "var(--link, #4a9)", textDecoration: "none", whiteSpace: "nowrap" }}
                          >
                            예측↑
                          </a>
                        )}
                      </td>
                    </tr>
                  );
                });
              })()}
            </tbody>
          </table>
          {state.status === "ready" && (
            <div style={{ padding: "8px 14px", fontSize: 11, color: "var(--fg-3)" }}>
              총 {state.total.toLocaleString("ko-KR")}건{compact && state.total > 8 && " (최근 8건 표시)"}
            </div>
          )}
        </div>
      )}
    </section>
  );
}