import { useState } from "react";
import { Link } from "react-router-dom";

import { fetchArtifacts } from "../../api/analysisArtifacts";
import { Pill } from "../../ds";
import { stockDetailPath } from "../../stockDetailPath";
import type { ArtifactMeta } from "../../types/analysisArtifacts";
import type { Market } from "../../types/investmentReports";

const KIND_LABEL: Record<string, string> = {
  screening_ranking: "스크리닝 랭킹",
  profit_taking_verdicts: "익절 판정",
  support_resistance_map: "지지/저항",
  flow_assessment: "수급 평가",
  candidate_pool: "후보 풀",
  session_summary: "세션 요약",
  briefing: "브리핑",
};

type LoadState =
  | { status: "idle" }
  | { status: "loading" }
  | { status: "ready"; rows: ArtifactMeta[] }
  | { status: "error"; message: string };

export default function ItemArtifactLinks({
  symbol,
  market,
  correlationIds,
}: {
  symbol?: string | null;
  market: Market;
  correlationIds: string[];
}) {
  const [open, setOpen] = useState(false);
  const [state, setState] = useState<LoadState>({ status: "idle" });

  const byCorrelation = correlationIds.length > 0;
  const hasTarget = byCorrelation || !!symbol;
  if (!hasTarget) return null;

  const headerLabel = byCorrelation
    ? "이 판단이 인용한 분석"
    : "이 종목 최근 분석 아티팩트";

  async function load() {
    setState({ status: "loading" });
    try {
      const res = await fetchArtifacts(
        byCorrelation
          ? { market, correlationIds, includeStale: true, limit: 10 }
          : {
              market,
              symbol: symbol ?? undefined,
              includeStale: true,
              limit: 10,
            },
      );
      setState({ status: "ready", rows: res.artifacts });
    } catch (e) {
      setState({
        status: "error",
        message: e instanceof Error ? e.message : String(e),
      });
    }
  }

  function toggle() {
    const next = !open;
    setOpen(next);
    if (next && state.status === "idle") void load();
  }

  return (
    <div className="item-artifact-links" data-testid="item-artifact-links">
      <button
        type="button"
        onClick={toggle}
        style={{
          background: "none",
          border: "none",
          padding: 0,
          fontSize: 12,
          color: "var(--fg-2)",
          cursor: "pointer",
          fontWeight: 800,
        }}
      >
        {open ? "▾" : "▸"} {headerLabel}
      </button>
      {open && state.status === "loading" ? (
        <div style={{ fontSize: 12, color: "var(--fg-3)" }}>불러오는 중…</div>
      ) : null}
      {open && state.status === "error" ? (
        <div style={{ fontSize: 12, color: "var(--fg-3)" }}>
          아티팩트를 불러오지 못했습니다
        </div>
      ) : null}
      {open && state.status === "ready" && state.rows.length === 0 ? (
        <div style={{ fontSize: 12, color: "var(--fg-3)" }}>
          관련 아티팩트 없음
        </div>
      ) : null}
      {open && state.status === "ready" && state.rows.length > 0 ? (
        <ul
          style={{
            margin: "4px 0 0",
            paddingLeft: 0,
            listStyle: "none",
            display: "grid",
            gap: 4,
          }}
        >
          {state.rows.map((a) => (
            <li
              key={a.id}
              style={{
                fontSize: 12,
                color: "var(--fg-2)",
                display: "flex",
                gap: 6,
                alignItems: "center",
                flexWrap: "wrap",
              }}
            >
              <Pill size="sm" tone={a.is_stale ? "warn" : "paper"}>
                {KIND_LABEL[a.kind] ?? a.kind}
              </Pill>
              <span>{a.title}</span>
              <span style={{ color: "var(--fg-3)" }}>
                {a.as_of.slice(0, 10)}
              </span>
              {a.symbols[0] ? (
                <Link
                  to={stockDetailPath(a.market, a.symbols[0]) ?? "#"}
                  style={{ color: "var(--accent)" }}
                >
                  종목
                </Link>
              ) : null}
            </li>
          ))}
        </ul>
      ) : null}
    </div>
  );
}
