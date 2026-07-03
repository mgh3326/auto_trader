import { useEffect, useState } from "react";
import { Link } from "react-router-dom";

import { fetchArtifactDetail, fetchArtifacts } from "../../api/analysisArtifacts";
import { Card, Pill } from "../../ds";
import { stockDetailPath } from "../../stockDetailPath";
import type {
  ArtifactKind,
  ArtifactMeta,
  ArtifactRead,
  ArtifactReadiness,
} from "../../types/analysisArtifacts";

type LoadState<T> =
  | { status: "loading" }
  | { status: "ready"; data: T }
  | { status: "error"; message: string };

const th: React.CSSProperties = {
  textAlign: "left",
  padding: "6px 8px",
  fontSize: 12,
  opacity: 0.7,
  whiteSpace: "nowrap",
};
const td: React.CSSProperties = { padding: "6px 8px", fontSize: 13 };

const selectStyle: React.CSSProperties = {
  border: "1px solid var(--border)",
  borderRadius: 8,
  padding: "4px 8px",
  fontSize: 12,
  background: "var(--surface-2)",
  color: "var(--fg-1)",
  fontFamily: "inherit",
};

const MARKET_OPTIONS: { key: "kr" | "us" | "crypto"; label: string }[] = [
  { key: "kr", label: "국내" },
  { key: "us", label: "미국" },
  { key: "crypto", label: "코인" },
];

const KIND_OPTIONS: { key: ArtifactKind; label: string }[] = [
  { key: "screening_ranking", label: "스크리닝 랭킹" },
  { key: "profit_taking_verdicts", label: "익절 판정" },
  { key: "support_resistance_map", label: "지지/저항" },
  { key: "flow_assessment", label: "수급 평가" },
  { key: "candidate_pool", label: "후보 풀" },
  { key: "session_summary", label: "세션 요약" },
  { key: "briefing", label: "브리핑" },
];

const READINESS_OPTIONS: { key: ArtifactReadiness; label: string }[] = [
  { key: "screen_grade", label: "스크리닝 등급" },
  { key: "not_decision_ready", label: "미결정" },
  { key: "ready_for_order_review", label: "주문검토 가능" },
  { key: "blocked", label: "차단" },
];

function fmt(ts: string): string {
  return ts.replace("T", " ").slice(0, 16);
}

export function AnalysisArtifactPanel() {
  const [state, setState] = useState<LoadState<ArtifactMeta[]>>({
    status: "loading",
  });
  const [detail, setDetail] = useState<ArtifactRead | null>(null);
  const [detailError, setDetailError] = useState<string | null>(null);
  const [market, setMarket] = useState<"kr" | "us" | "crypto" | "">("");
  const [kind, setKind] = useState<ArtifactKind | "">("");
  const [readiness, setReadiness] = useState<ArtifactReadiness | "">("");

  useEffect(() => {
    let cancelled = false;
    setState({ status: "loading" });
    fetchArtifacts({
      includeStale: true,
      limit: 20,
      market: market || undefined,
      kind: kind || undefined,
      readinessLabel: readiness || undefined,
    })
      .then((res) => {
        if (!cancelled) setState({ status: "ready", data: res.artifacts });
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
  }, [market, kind, readiness]);

  async function openDetail(id: number) {
    setDetail(null);
    setDetailError(null);
    try {
      const res = await fetchArtifactDetail(id);
      setDetail(res.artifact);
    } catch (e: unknown) {
      setDetailError(e instanceof Error ? e.message : String(e));
    }
  }

  return (
    <Card data-testid="analysis-artifact-panel">
      <section style={{ display: "grid", gap: 12 }}>
        <div>
          <h2 style={{ margin: 0, fontSize: 18 }}>분석 아티팩트</h2>
          <p style={{ margin: "4px 0 0", fontSize: 12, color: "var(--fg-3)" }}>
            review.analysis_artifacts에서 가장 최근 결정 자료 — 시장/종류/준비상태 필터와 신선도 배지로 활용.
          </p>
        </div>
        <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
          <select
            aria-label="시장 필터"
            value={market}
            onChange={(e) => setMarket(e.target.value as "kr" | "us" | "crypto" | "")}
            style={selectStyle}
          >
            <option value="">전체 시장</option>
            {MARKET_OPTIONS.map((o) => (
              <option key={o.key} value={o.key}>{o.label}</option>
            ))}
          </select>
          <select
            aria-label="종류 필터"
            value={kind}
            onChange={(e) => setKind(e.target.value as ArtifactKind | "")}
            style={selectStyle}
          >
            <option value="">전체 종류</option>
            {KIND_OPTIONS.map((o) => (
              <option key={o.key} value={o.key}>{o.label}</option>
            ))}
          </select>
          <select
            aria-label="준비상태 필터"
            value={readiness}
            onChange={(e) => setReadiness(e.target.value as ArtifactReadiness | "")}
            style={selectStyle}
          >
            <option value="">전체 준비상태</option>
            {READINESS_OPTIONS.map((o) => (
              <option key={o.key} value={o.key}>{o.label}</option>
            ))}
          </select>
        </div>
        {state.status === "loading" && (
          <div style={{ padding: 12, color: "var(--fg-3)", fontSize: 13 }}>
            불러오는 중…
          </div>
        )}
        {state.status === "error" && (
          <div role="alert" style={{ padding: 12, color: "var(--danger)", fontSize: 13 }}>
            아티팩트를 불러오지 못했습니다. {state.message}
          </div>
        )}
        {state.status === "ready" && state.data.length === 0 && (
          <div style={{ padding: 12, color: "var(--fg-3)", fontSize: 13 }}>
            저장된 분석 아티팩트가 없습니다.
          </div>
        )}
        {state.status === "ready" && state.data.length > 0 && (
          <div style={{ overflowX: "auto" }}>
            <table style={{ borderCollapse: "collapse", width: "100%" }}>
              <thead>
                <tr>
                  <th style={th}>종류</th>
                  <th style={th}>제목</th>
                  <th style={th}>종목</th>
                  <th style={th}>시장</th>
                  <th style={th}>as_of</th>
                  <th style={th}>상태</th>
                  <th style={th}>ver</th>
                </tr>
              </thead>
              <tbody>
                {state.data.map((a) => (
                  <tr key={a.id}>
                    <td style={td}>
                      <Pill tone="paper" size="sm">
                        {a.kind}
                      </Pill>
                    </td>
                    <td style={td}>
                      <button
                        type="button"
                        onClick={() => openDetail(a.id)}
                        style={{
                          background: "none",
                          border: "none",
                          color: "var(--link, #4a9)",
                          cursor: "pointer",
                          padding: 0,
                          font: "inherit",
                          textAlign: "left",
                        }}
                      >
                        {a.title}
                      </button>
                    </td>
                    <td style={td}>
                      {a.symbols.length > 0 ? (
                        <span style={{ display: "inline-flex", gap: 6, flexWrap: "wrap" }}>
                          {a.symbols.map((sym) => {
                            const href = stockDetailPath(a.market, sym);
                            return href ? (
                              <Link key={sym} to={href} style={{ color: "var(--link, #4a9)", textDecoration: "none" }}>
                                {sym}
                              </Link>
                            ) : (
                              <span key={sym}>{sym}</span>
                            );
                          })}
                        </span>
                      ) : (
                        <span style={{ color: "var(--fg-3)" }}>—</span>
                      )}
                    </td>
                    <td style={td}>{a.market}</td>
                    <td style={td}>{fmt(a.as_of)}</td>
                    <td style={td}>
                      <span style={{ display: "inline-flex", gap: 4, flexWrap: "wrap" }}>
                        {a.is_stale ? (
                          <Pill tone="loss" size="sm">
                            stale
                          </Pill>
                        ) : (
                          <Pill tone="gain" size="sm">
                            fresh
                          </Pill>
                        )}
                        {a.readiness_label && (
                          <Pill tone="paper" size="sm">
                            {a.readiness_label}
                          </Pill>
                        )}
                      </span>
                    </td>
                    <td style={td}>v{a.version}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        {detailError && (
          <div role="alert" style={{ padding: 12, color: "var(--danger)", fontSize: 13 }}>
            페이로드 조회 실패: {detailError}
          </div>
        )}
        {detail && (
          <div style={{ marginTop: 4 }}>
            <h4 style={{ margin: "0 0 4px" }}>
              payload — {detail.title}{" "}
              {detail.content_hash && (
                <span style={{ opacity: 0.6, fontSize: 12 }}>
                  #{detail.content_hash.slice(0, 12)}
                </span>
              )}
            </h4>
            <pre
              style={{
                maxHeight: 320,
                overflow: "auto",
                background: "var(--surface-2, #1113)",
                padding: 8,
                fontSize: 12,
                borderRadius: 4,
                margin: 0,
              }}
            >
              {JSON.stringify(detail.payload, null, 2)}
            </pre>
          </div>
        )}
      </section>
    </Card>
  );
}
