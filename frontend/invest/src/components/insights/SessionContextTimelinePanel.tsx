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

// Row shows time only (HH:MM); the date lives in the per-kst_date group header.
function hhmm(ts: string): string {
  return ts.slice(11, 16);
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

// Visual clamp height (in lines) for a collapsed row body, and the
// content-based threshold used to decide whether a row's body is long enough
// to need a 더보기/접기 toggle. Content-based (not DOM-measured) so the
// decision is deterministic under jsdom (no layout/scrollHeight available).
const CLAMP_LINES = 3;
const CLAMP_CHAR_THRESHOLD = 160;

// Pure heuristic: more newlines than the clamp allows, or long enough that it
// likely wraps past CLAMP_LINES. Biased fail-open (toggle shown) over
// false-negative (content truncated with no way to expand it).
function isBodyClampable(body: string): boolean {
  return body.split("\n").length > CLAMP_LINES || body.length > CLAMP_CHAR_THRESHOLD;
}

// Body of one timeline entry, clamped to CLAMP_LINES with a per-row
// 더보기/접기 toggle when the content is long enough to warrant one (per
// isBodyClampable). Local state is safe here because each row is keyed by
// entry_uuid (stable identity across renders/regroupings).
function ExpandableBody({ body, bodyId }: { body: string; bodyId: string }) {
  const [expanded, setExpanded] = useState(false);
  const clampable = isBodyClampable(body);
  return (
    <div>
      <div
        id={bodyId}
        style={{
          fontSize: 13,
          opacity: 0.85,
          whiteSpace: "pre-wrap",
          ...(clampable && !expanded
            ? {
                display: "-webkit-box",
                WebkitBoxOrient: "vertical",
                WebkitLineClamp: CLAMP_LINES,
                overflow: "hidden",
              }
            : { display: "block" }),
        }}
      >
        {body}
      </div>
      {clampable && (
        <button
          type="button"
          data-testid="session-row-toggle"
          onClick={() => setExpanded((v) => !v)}
          aria-expanded={expanded}
          aria-controls={bodyId}
          aria-label={expanded ? "본문 접기" : "본문 더보기"}
          style={{
            border: "none",
            background: "transparent",
            color: "var(--fg-3)",
            cursor: "pointer",
            fontFamily: "inherit",
            fontSize: 12,
            fontWeight: 700,
            padding: "2px 0",
          }}
        >
          {expanded ? "접기" : "더보기"}
        </button>
      )}
    </div>
  );
}

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
        <span style={{ opacity: 0.6, fontSize: 12 }}>{hhmm(entry.created_at)}</span>
      </div>
      <div style={{ fontWeight: 700, marginTop: 4 }}>{entry.title}</div>
      <ExpandableBody body={entry.body} bodyId={`sess-body-${entry.entry_uuid}`} />
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

export function SessionContextTimelinePanel({
  onEmptyChange,
}: { onEmptyChange?: (isEmpty: boolean) => void } = {}) {
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

  // Report emptiness to the page (ROB-677 banner): empty only when ready + no rows.
  useEffect(() => {
    if (!onEmptyChange) return;
    onEmptyChange(state.status === "ready" && state.data.length === 0);
  }, [state, onEmptyChange]);

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
          <div style={{ padding: 12, color: "var(--fg-3)", fontSize: 13, lineHeight: 1.6 }}>
            최근 세션 컨텍스트 없음 — 운영 세션이 결정·계획·핸드오프 메모를 남기면 여기에 타임라인으로 쌓입니다.
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
