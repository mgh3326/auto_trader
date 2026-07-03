import { useEffect, useState } from "react";
import { Link } from "react-router-dom";

import { fetchRecentSessionContext } from "../../api/sessionContext";
import { Card, Pill } from "../../ds";
import { stockDetailPath } from "../../stockDetailPath";
import type { SessionContextEntry } from "../../types/sessionContext";

type LoadState<T> =
  | { status: "loading" }
  | { status: "ready"; data: T }
  | { status: "error"; message: string };

function fmt(ts: string): string {
  return ts.replace("T", " ").slice(0, 16);
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
          <ul style={{ listStyle: "none", margin: 0, padding: 0 }}>
            {state.data.map((e) => {
              const refs = e.refs as {
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
                <li
                  key={e.entry_uuid}
                  style={{
                    padding: "10px 0",
                    borderBottom: "1px solid var(--divider, #8882)",
                  }}
                >
                  <div style={{ display: "flex", gap: 6, alignItems: "center", flexWrap: "wrap" }}>
                    <Pill tone="paper" size="sm">
                      {e.entry_type}
                    </Pill>
                    <Pill tone="paper" size="sm">
                      {e.market}
                    </Pill>
                    {e.account_scope && (
                      <Pill tone="paper" size="sm">
                        {e.account_scope}
                      </Pill>
                    )}
                    <span style={{ opacity: 0.6, fontSize: 12 }}>
                      {e.kst_date} · {fmt(e.created_at)}
                    </span>
                  </div>
                  <div style={{ fontWeight: 700, marginTop: 4 }}>{e.title}</div>
                  <div
                    style={{ fontSize: 13, opacity: 0.85, whiteSpace: "pre-wrap" }}
                  >
                    {e.body}
                  </div>
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
                        const href = stockDetailPath(e.market, sym);
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
            })}
          </ul>
        )}
      </section>
    </Card>
  );
}
