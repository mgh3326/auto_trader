import { useEffect, useState } from "react";
import { Link } from "react-router-dom";

import { fetchRecentSessionContext } from "../../api/sessionContext";
import { Card, Pill } from "../../ds";
import type { PillTone } from "../../ds";
import { stockDetailPath } from "../../stockDetailPath";
import type { SessionContextEntry, SessionEntryType } from "../../types/sessionContext";

type LoadState<T> =
  | { status: "loading" }
  | { status: "ready"; data: T }
  | { status: "error"; message: string };

function fmt(ts: string): string {
  return ts.replace("T", " ").slice(0, 16);
}

// 8 entry_type values (session_context schema) → Pill tone, so decision /
// deferred / next_action etc. read distinctly instead of uniform grey.
const ENTRY_TYPE_TONE: Record<SessionEntryType, PillTone> = {
  decision: "gain",
  next_action: "accent",
  plan: "accent",
  handoff_note: "paper",
  deferred: "loss",
  rejected_candidate: "loss",
  constraint: "warn",
  open_question: "warn",
};

// Bucket entries by kst_date, preserving the incoming newest-first order both
// across groups (first date seen = newest) and within each group.
function groupByDate(
  entries: SessionContextEntry[],
): { kst_date: string; entries: SessionContextEntry[] }[] {
  const groups: { kst_date: string; entries: SessionContextEntry[] }[] = [];
  const index = new Map<string, SessionContextEntry[]>();
  for (const e of entries) {
    let bucket = index.get(e.kst_date);
    if (!bucket) {
      bucket = [];
      index.set(e.kst_date, bucket);
      groups.push({ kst_date: e.kst_date, entries: bucket });
    }
    bucket.push(e);
  }
  return groups;
}

// One timeline entry. Owns the refs footer (ROB-673) so the date-grouping
// wrapper (this ticket) and the per-row content stay decoupled.
function SessionRow({ entry }: { entry: SessionContextEntry }) {
  const refs = entry.refs as {
    symbols?: unknown;
    order_id?: unknown;
    report_uuid?: unknown;
  };
  const refSymbols = Array.isArray(refs.symbols)
    ? refs.symbols.filter((s): s is string => typeof s === "string")
    : [];
  const orderId = typeof refs.order_id === "string" ? refs.order_id : null;
  const reportUuid = typeof refs.report_uuid === "string" ? refs.report_uuid : null;
  const hasRefs = refSymbols.length > 0 || orderId != null || reportUuid != null;
  return (
    <li style={{ padding: "10px 0", borderBottom: "1px solid var(--divider, #8882)" }}>
      <div style={{ display: "flex", gap: 6, alignItems: "center", flexWrap: "wrap" }}>
        <Pill tone={ENTRY_TYPE_TONE[entry.entry_type] ?? "paper"} size="sm">
          {entry.entry_type}
        </Pill>
        <Pill tone="paper" size="sm">{entry.market}</Pill>
        {entry.account_scope && <Pill tone="paper" size="sm">{entry.account_scope}</Pill>}
        <span style={{ opacity: 0.6, fontSize: 12 }}>{fmt(entry.created_at)}</span>
      </div>
      <div style={{ fontWeight: 700, marginTop: 4 }}>{entry.title}</div>
      <div style={{ fontSize: 13, opacity: 0.85, whiteSpace: "pre-wrap" }}>{entry.body}</div>
      {hasRefs && (
        <div
          style={{
            display: "flex",
            gap: 8,
            flexWrap: "wrap",
            marginTop: 6,
            fontSize: 11,
            color: "var(--fg-3)",
            alignItems: "center",
          }}
        >
          {refSymbols.map((sym) => {
            const href = stockDetailPath(entry.market, sym);
            return href ? (
              <Link
                key={sym}
                to={href}
                style={{ color: "var(--link, #4a9)", textDecoration: "none", fontWeight: 700 }}
              >
                {sym}
              </Link>
            ) : (
              <span key={sym} style={{ fontWeight: 700 }}>{sym}</span>
            );
          })}
          {orderId && <span>주문 {orderId}</span>}
          {reportUuid && <span>리포트 {reportUuid.slice(0, 8)}</span>}
        </div>
      )}
    </li>
  );
}

export function SessionContextTimelinePanel() {
  const [state, setState] = useState<LoadState<SessionContextEntry[]>>({
    status: "loading",
  });

  useEffect(() => {
    let cancelled = false;
    fetchRecentSessionContext({ limit: 15 })
      .then((res) => {
        if (!cancelled) setState({ status: "ready", data: res.entries });
      })
      .catch((e: unknown) => {
        if (!cancelled)
          setState({
            status: "error",
            message: e instanceof Error ? e.message : String(e),
          });
      });
    return () => {
      cancelled = true;
    };
  }, []);

  return (
    <Card data-testid="session-context-timeline-panel">
      <section style={{ display: "grid", gap: 12 }}>
        <div>
          <h2 style={{ margin: 0, fontSize: 18 }}>최근 핸드오프</h2>
          <p style={{ margin: "4px 0 0", fontSize: 12, color: "var(--fg-3)" }}>
            review.operator_session_context — 결정/계획/deferred 등 운영 메모의 최신순 피드.
          </p>
        </div>
        {state.status === "loading" && (
          <div style={{ padding: 12, color: "var(--fg-3)", fontSize: 13 }}>
            불러오는 중…
          </div>
        )}
        {state.status === "error" && (
          <div role="alert" style={{ padding: 12, color: "var(--danger)", fontSize: 13 }}>
            세션 컨텍스트를 불러오지 못했습니다. {state.message}
          </div>
        )}
        {state.status === "ready" && state.data.length === 0 && (
          <div style={{ padding: 12, color: "var(--fg-3)", fontSize: 13 }}>
            최근 세션 컨텍스트 없음
          </div>
        )}
        {state.status === "ready" && state.data.length > 0 && (
          <div style={{ display: "grid", gap: 12 }}>
            {groupByDate(state.data).map((group) => (
              <div key={group.kst_date}>
                <div
                  style={{
                    fontSize: 12,
                    fontWeight: 800,
                    color: "var(--fg-2)",
                    borderBottom: "1px solid var(--divider)",
                    paddingBottom: 4,
                  }}
                >
                  {group.kst_date}
                </div>
                <ul style={{ listStyle: "none", margin: 0, padding: 0 }}>
                  {group.entries.map((e) => (
                    <SessionRow key={e.entry_uuid} entry={e} />
                  ))}
                </ul>
              </div>
            ))}
          </div>
        )}
      </section>
    </Card>
  );
}
