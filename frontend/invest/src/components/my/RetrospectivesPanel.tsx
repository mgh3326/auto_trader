import { useEffect, useMemo, useRef, useState } from "react";
import { Link } from "react-router-dom";

import { fetchRetrospectiveActions, fetchRetrospectives } from "../../api/retrospectives";
import { Pill } from "../../ds";
import { buildLinearIssueUrl } from "../../linearLink";
import { crosslinkAnchorSlug, crosslinkKey, retroMarket } from "../../insightsCrosslink";
import { stockDetailPath } from "../../stockDetailPath";
import type {
  RetroMarket,
  RetroOutcomeFilter,
  RetrospectiveAction,
  RetrospectiveRow,
} from "../../types/retrospectives";

const LINEAR_WORKSPACE_URL = import.meta.env.VITE_LINEAR_WORKSPACE_URL;

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

const OUTCOME_OPTIONS: { key: RetroOutcomeFilter | ""; label: string }[] = [
  { key: "", label: "전체" },
  { key: "win", label: "승" },
  { key: "loss", label: "패" },
  { key: "decided", label: "결정" },
];

const SEARCH_DEBOUNCE_MS = 300;

// ROB-885 — read-only triage UX. No mutation controls; the action network
// call is GET-only and always requests status=open,in_progress explicitly.
const ACTION_STATUS_LABEL: Record<string, { label: string; tone: "paper" | "accent" }> = {
  open: { label: "예정", tone: "paper" },
  in_progress: { label: "진행중", tone: "accent" },
};

function pnlText(row: { realized_pnl: number | null; realized_pnl_currency: string | null }): string {
  if (row.realized_pnl == null) return "—";
  const sign = row.realized_pnl > 0 ? "+" : "";
  return `${sign}${row.realized_pnl.toLocaleString("ko-KR")} ${row.realized_pnl_currency ?? ""}`.trim();
}

function ActionIssueLink({ issueId }: { issueId: string | null }) {
  if (!issueId) return null;
  const href = buildLinearIssueUrl(issueId, LINEAR_WORKSPACE_URL);
  if (!href) {
    return <span style={{ color: "var(--fg-3)", fontSize: 11 }}>· {issueId}</span>;
  }
  return (
    <a
      href={href}
      target="_blank"
      rel="noopener noreferrer"
      style={{ color: "var(--link, #4a9)", textDecoration: "none", fontSize: 11, whiteSpace: "nowrap" }}
    >
      · {issueId}
    </a>
  );
}

function ActionQueue({
  state,
  compact,
  loadingMore,
  onLoadMore,
}: {
  state:
    | { status: "loading" }
    | { status: "ready"; items: RetrospectiveAction[]; total: number; hasMore: boolean }
    | { status: "error"; message: string };
  compact: boolean;
  loadingMore: boolean;
  onLoadMore: () => void;
}) {
  if (state.status === "loading") {
    return (
      <div data-testid="retro-actions" style={{ margin: "0 14px 12px", padding: "10px 12px", borderRadius: 12, background: "var(--surface-2)" }}>
        <div style={{ fontSize: 12, fontWeight: 800, marginBottom: 6 }}>미완료 액션</div>
        <div style={{ fontSize: 13, color: "var(--fg-3)" }}>액션을 불러오는 중…</div>
      </div>
    );
  }
  if (state.status === "error") {
    return (
      <div data-testid="retro-actions" role="alert" style={{ margin: "0 14px 12px", padding: "10px 12px", borderRadius: 12, background: "var(--surface-2)" }}>
        <div style={{ fontSize: 12, fontWeight: 800, marginBottom: 6 }}>미완료 액션</div>
        <div style={{ fontSize: 13, color: "var(--danger)" }}>액션을 불러오지 못했습니다. {state.message}</div>
      </div>
    );
  }
  const { items, total, hasMore } = state;
  if (items.length === 0) {
    return (
      <div data-testid="retro-actions" style={{ margin: "0 14px 12px", padding: "10px 12px", borderRadius: 12, background: "var(--surface-2)" }}>
        <div style={{ fontSize: 12, fontWeight: 800, marginBottom: 6 }}>미완료 액션 ({total})</div>
        <div style={{ fontSize: 13, color: "var(--fg-3)" }}>진행 중인 액션이 없습니다.</div>
      </div>
    );
  }
  return (
    <div data-testid="retro-actions" style={{ margin: "0 14px 12px", padding: "10px 12px", borderRadius: 12, background: "var(--surface-2)" }}>
      <div style={{ fontSize: 12, fontWeight: 800, marginBottom: 6 }}>미완료 액션 ({total})</div>
      <div style={{ display: "grid", gap: 6 }}>
        {items.map((a) => {
          const statusMeta = ACTION_STATUS_LABEL[a.status] ?? { label: a.status, tone: "paper" as const };
          const href = a.market ? stockDetailPath(a.market as "kr" | "us" | "crypto", a.symbol ?? "") : null;
          return (
            <div key={a.action_id} style={{ display: "grid", gap: 3, fontSize: 13, padding: "4px 0", borderBottom: "1px solid var(--divider)" }}>
              <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
                <Pill tone={statusMeta.tone} size="sm">{statusMeta.label}</Pill>
                {a.overdue && <Pill tone="warn" size="sm">지연</Pill>}
                <span>{a.action}</span>
              </div>
              <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap", color: "var(--fg-3)", fontSize: 11 }}>
                {href && a.symbol ? (
                  <Link to={href} style={{ color: "inherit", textDecoration: "none", fontWeight: 700 }}>{a.symbol}</Link>
                ) : a.symbol ? (
                  <span style={{ fontWeight: 700 }}>{a.symbol}</span>
                ) : null}
                {a.owner && <span>· 담당 {a.owner}</span>}
                <ActionIssueLink issueId={a.issue_id} />
                {a.due_kst_date && <span>· 마감 {a.due_kst_date}</span>}
              </div>
            </div>
          );
        })}
      </div>
      {hasMore && (
        <button
          type="button"
          onClick={onLoadMore}
          disabled={loadingMore}
          style={{
            marginTop: 8, border: "1px solid var(--border)", borderRadius: 8, padding: "4px 10px",
            fontSize: 11, fontWeight: 700, cursor: loadingMore ? "default" : "pointer", fontFamily: "inherit",
            background: "var(--surface)", color: "var(--fg-2)",
          }}
        >
          {loadingMore ? "불러오는 중…" : compact ? "더 보기" : "더 많은 액션 보기"}
        </button>
      )}
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
  const [symbolSearchInput, setSymbolSearchInput] = useState("");
  const [symbolSearch, setSymbolSearch] = useState("");
  const [dateFrom, setDateFrom] = useState("");
  const [dateTo, setDateTo] = useState("");
  const [state, setState] = useState<
    | { status: "loading" }
    | { status: "ready"; items: RetrospectiveRow[]; total: number }
    | { status: "error"; message: string }
  >({ status: "loading" });

  const actionPageSize = compact ? 5 : 10;
  const [actionOffset, setActionOffset] = useState(0);
  const [loadingMore, setLoadingMore] = useState(false);
  const [actionState, setActionState] = useState<
    | { status: "loading" }
    | { status: "ready"; items: RetrospectiveAction[]; total: number; hasMore: boolean }
    | { status: "error"; message: string }
  >({ status: "loading" });
  const actionReqIdRef = useRef(0);

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

  // ROB-885 — canonical action queue. Shares the same filter semantics as the
  // retrospective list. Filter changes reset offset to 0. A request-id guard
  // discards stale responses so a slow older fetch can't overwrite a newer one.
  useEffect(() => {
    const reqId = ++actionReqIdRef.current;
    setActionOffset(0);
    setLoadingMore(false);
    setActionState({ status: "loading" });
    fetchRetrospectiveActions({
      market,
      triggerType: triggerType || undefined,
      outcomeFilter: outcomeFilter || undefined,
      q: symbolSearch || undefined,
      dateFrom: dateFrom || undefined,
      dateTo: dateTo || undefined,
      limit: actionPageSize,
      offset: 0,
    })
      .then((data) => {
        if (actionReqIdRef.current !== reqId) return;
        setActionState({
          status: "ready",
          items: data.items,
          total: data.total,
          hasMore: data.offset + data.count < data.total,
        });
      })
      .catch((err: unknown) => {
        if (actionReqIdRef.current !== reqId) return;
        setActionState({ status: "error", message: err instanceof Error ? err.message : String(err) });
      });
  }, [market, triggerType, outcomeFilter, symbolSearch, dateFrom, dateTo, actionPageSize]);

  function handleLoadMoreActions() {
    if (actionState.status !== "ready" || !actionState.hasMore || loadingMore) return;
    const nextOffset = actionOffset + actionPageSize;
    const reqId = ++actionReqIdRef.current;
    setActionOffset(nextOffset);
    setLoadingMore(true);
    fetchRetrospectiveActions({
      market,
      triggerType: triggerType || undefined,
      outcomeFilter: outcomeFilter || undefined,
      q: symbolSearch || undefined,
      dateFrom: dateFrom || undefined,
      dateTo: dateTo || undefined,
      limit: actionPageSize,
      offset: nextOffset,
    })
      .then((data) => {
        if (actionReqIdRef.current !== reqId) return;
        setActionState((prev) => {
          if (prev.status !== "ready") return prev;
          return {
            status: "ready",
            items: [...prev.items, ...data.items],
            total: data.total,
            hasMore: data.offset + data.count < data.total,
          };
        });
      })
      .catch((err: unknown) => {
        if (actionReqIdRef.current !== reqId) return;
        setActionState({ status: "error", message: err instanceof Error ? err.message : String(err) });
      })
      .finally(() => {
        if (actionReqIdRef.current === reqId) setLoadingMore(false);
      });
  }

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

      <ActionQueue state={actionState} compact={compact} loadingMore={loadingMore} onLoadMore={handleLoadMoreActions} />

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
