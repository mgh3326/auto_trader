import { useEffect, useState } from "react";

import { fetchArtifactDetail, fetchArtifacts } from "../../api/analysisArtifacts";
import { Card, Pill } from "../../ds";
import type { ArtifactMeta, ArtifactRead } from "../../types/analysisArtifacts";

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

function fmt(ts: string): string {
  return ts.replace("T", " ").slice(0, 16);
}

export function AnalysisArtifactPanel() {
  const [state, setState] = useState<LoadState<ArtifactMeta[]>>({
    status: "loading",
  });
  const [detail, setDetail] = useState<ArtifactRead | null>(null);
  const [detailError, setDetailError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    fetchArtifacts({ includeStale: true, limit: 20 })
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
  }, []);

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
