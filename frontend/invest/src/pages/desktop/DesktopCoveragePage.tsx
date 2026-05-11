import { useEffect, useMemo, useState } from "react";
import { fetchInvestCoverage } from "../../api/coverage";
import { DesktopShell } from "../../desktop/DesktopShell";
import { Card } from "../../ds";
import type {
  CoverageCandidateReadiness,
  CoverageState,
  InvestCoverageResponse,
  InvestCoverageSurface,
} from "../../types/coverage";

type Market = "kr" | "us" | "crypto" | "all";

const STATE_LABEL: Record<CoverageState, string> = {
  fresh: "정상",
  stale: "오래됨",
  partial: "부분",
  missing: "없음",
  unsupported: "미지원",
  error: "오류",
  provider_unwired: "미연결",
};

const STATE_COLOR: Record<CoverageState, string> = {
  fresh: "#16a34a",
  stale: "#d97706",
  partial: "#ca8a04",
  missing: "#dc2626",
  unsupported: "#64748b",
  error: "#b91c1c",
  provider_unwired: "#7c3aed",
};

const READINESS_LABEL: Record<CoverageCandidateReadiness, string> = {
  live: "live",
  request_time_only: "request-time",
  fixture_backed_poc: "PoC",
  aggregate_only_blocked: "blocked",
  not_wired: "미연결",
};

const READINESS_COLOR: Record<CoverageCandidateReadiness, string> = {
  live: "#0ea5e9",
  request_time_only: "#6366f1",
  fixture_backed_poc: "#a16207",
  aggregate_only_blocked: "#9333ea",
  not_wired: "#64748b",
};

function StatePill({ state }: { state: CoverageState }) {
  return (
    <span
      style={{
        display: "inline-flex",
        alignItems: "center",
        borderRadius: 999,
        padding: "3px 8px",
        fontSize: 12,
        fontWeight: 800,
        color: "white",
        background: STATE_COLOR[state],
        whiteSpace: "nowrap",
      }}
    >
      {STATE_LABEL[state]}
    </span>
  );
}

function SurfaceRow({ surface }: { surface: InvestCoverageSurface }) {
  const latest = surface.latestDate ?? surface.latestAt ?? "-";
  const counts = surface.counts;
  return (
    <tr>
      <td style={{ padding: "12px 10px", borderBottom: "1px solid var(--divider)" }}>
        <div style={{ fontWeight: 800 }}>{surface.label}</div>
        <div style={{ color: "var(--fg-3)", fontSize: 12 }}>{surface.surface}</div>
        {surface.sourceCandidates.length > 0 && (
          <div style={{ marginTop: 6, display: "flex", gap: 6, flexWrap: "wrap" }}>
            {surface.sourceCandidates.map((candidate) => (
              <span
                key={`${candidate.name}-${candidate.surface}-${candidate.readiness}`}
                title={candidate.notes[0] ?? candidate.warnings[0] ?? ""}
                style={{
                  display: "inline-flex",
                  alignItems: "center",
                  gap: 4,
                  borderRadius: 8,
                  padding: "2px 6px",
                  fontSize: 11,
                  fontWeight: 700,
                  color: "white",
                  background: READINESS_COLOR[candidate.readiness],
                }}
              >
                {candidate.name} · {READINESS_LABEL[candidate.readiness]}
              </span>
            ))}
          </div>
        )}
      </td>
      <td style={{ padding: "12px 10px", borderBottom: "1px solid var(--divider)" }}>
        <StatePill state={surface.state} />
      </td>
      <td style={{ padding: "12px 10px", borderBottom: "1px solid var(--divider)", color: "var(--fg-2)" }}>
        {surface.market ?? "-"}
      </td>
      <td style={{ padding: "12px 10px", borderBottom: "1px solid var(--divider)", fontFamily: "var(--font-mono)", fontSize: 12 }}>
        {surface.sourceOfTruth}
      </td>
      <td style={{ padding: "12px 10px", borderBottom: "1px solid var(--divider)", color: "var(--fg-2)", fontSize: 12 }}>
        {latest}
      </td>
      <td style={{ padding: "12px 10px", borderBottom: "1px solid var(--divider)", fontSize: 12 }}>
        <span>fresh {counts.fresh}</span>
        <span style={{ marginLeft: 8 }}>stale {counts.stale}</span>
        <span style={{ marginLeft: 8 }}>missing {counts.missing}</span>
        {counts.expected != null && <span style={{ marginLeft: 8 }}>expected {counts.expected}</span>}
      </td>
      <td style={{ padding: "12px 10px", borderBottom: "1px solid var(--divider)", color: "var(--fg-3)", fontSize: 12 }}>
        {surface.warnings[0] ?? surface.notes[0] ?? "-"}
      </td>
    </tr>
  );
}

export function CoverageRoute() {
  const [market, setMarket] = useState<Market>("kr");
  const [symbols, setSymbols] = useState("005930, AAPL");
  const [data, setData] = useState<InvestCoverageResponse | undefined>();
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    setErr(null);
    fetchInvestCoverage({ market, symbols, signal: controller.signal })
      .then((response) => {
        setData(response);
        setLoading(false);
      })
      .catch((e) => {
        if (controller.signal.aborted) return;
        setErr(String(e?.message ?? e));
        setLoading(false);
      });
    return () => controller.abort();
  }, [market, symbols]);

  const summary = useMemo(() => {
    const surfaces = data?.surfaces ?? [];
    return data?.states.map((state) => ({
      state,
      count: surfaces.filter((surface) => surface.state === state).length,
    })) ?? [];
  }, [data]);

  return (
    <DesktopShell
      center={
        <div style={{ padding: 24, display: "grid", gap: 16 }}>
        <div>
          <h1 style={{ margin: 0, fontSize: 26, letterSpacing: "-0.04em" }}>데이터 커버리지</h1>
          <p style={{ margin: "6px 0 0", color: "var(--fg-2)", fontSize: 14 }}>
            /invest 소유 read-model의 freshness와 Toss/Naver 기준·후보 신호를 구분해 확인합니다.
          </p>
        </div>

        <Card>
          <div style={{ display: "flex", gap: 12, alignItems: "end", flexWrap: "wrap" }}>
            <label style={{ display: "grid", gap: 6, fontSize: 12, color: "var(--fg-2)" }}>
              Market
              <select value={market} onChange={(e) => setMarket(e.target.value as Market)} style={{ padding: "8px 10px", borderRadius: 8, border: "1px solid var(--border)", background: "var(--surface)" }}>
                <option value="kr">KR</option>
                <option value="us">US</option>
                <option value="crypto">Crypto</option>
                <option value="all">All</option>
              </select>
            </label>
            <label style={{ display: "grid", gap: 6, fontSize: 12, color: "var(--fg-2)", minWidth: 260 }}>
              Symbols (optional)
              <input value={symbols} onChange={(e) => setSymbols(e.target.value)} placeholder="005930, AAPL" style={{ padding: "8px 10px", borderRadius: 8, border: "1px solid var(--border)", background: "var(--surface)" }} />
            </label>
            {data && <div style={{ color: "var(--fg-3)", fontSize: 12 }}>asOf {data.asOf} · tradingDate {data.tradingDate}</div>}
          </div>
        </Card>

        {loading && <Card>커버리지 로딩 중…</Card>}
        {err && <Card><span style={{ color: STATE_COLOR.error }}>커버리지 API 오류: {err}</span></Card>}

        {data && !loading && (
          <>
            <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(130px, 1fr))", gap: 12 }}>
              {summary.map(({ state, count }) => (
                <Card key={state}>
                  <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", gap: 8 }}>
                    <StatePill state={state} />
                    <strong style={{ fontSize: 22 }}>{count}</strong>
                  </div>
                </Card>
              ))}
            </div>

            <Card>
              <div style={{ overflowX: "auto" }}>
                <table style={{ width: "100%", borderCollapse: "collapse", minWidth: 980 }}>
                  <thead>
                    <tr style={{ textAlign: "left", color: "var(--fg-3)", fontSize: 12 }}>
                      <th style={{ padding: "0 10px 8px" }}>Surface</th>
                      <th style={{ padding: "0 10px 8px" }}>State</th>
                      <th style={{ padding: "0 10px 8px" }}>Market</th>
                      <th style={{ padding: "0 10px 8px" }}>Source of truth</th>
                      <th style={{ padding: "0 10px 8px" }}>Latest</th>
                      <th style={{ padding: "0 10px 8px" }}>Counts</th>
                      <th style={{ padding: "0 10px 8px" }}>Gap / note</th>
                    </tr>
                  </thead>
                  <tbody>{data.surfaces.map((surface, idx) => <SurfaceRow key={`${surface.surface}-${surface.market}-${idx}`} surface={surface} />)}</tbody>
                </table>
              </div>
            </Card>

            {data.symbols.length > 0 && (
              <Card>
                <h2 style={{ margin: "0 0 12px", fontSize: 18 }}>Symbol coverage</h2>
                <div style={{ display: "grid", gap: 10 }}>
                  {data.symbols.map((symbol) => (
                    <div key={symbol.symbol} style={{ display: "flex", gap: 10, alignItems: "center", flexWrap: "wrap" }}>
                      <strong style={{ width: 84 }}>{symbol.symbol}</strong>
                      {Object.entries(symbol.surfaces).map(([name, state]) => <span key={name} style={{ display: "inline-flex", gap: 6, alignItems: "center" }}><span style={{ color: "var(--fg-3)", fontSize: 12 }}>{name}</span><StatePill state={state} /></span>)}
                    </div>
                  ))}
                </div>
              </Card>
            )}
          </>
        )}
        </div>
      }
      right={
        <Card>
          <div style={{ fontWeight: 800, marginBottom: 8 }}>ROB-201 원칙</div>
          <ul style={{ margin: 0, paddingLeft: 18, color: "var(--fg-2)", fontSize: 13 }}>
            <li>source-of-truth는 로컬 DB/read-model만</li>
            <li>Toss/Naver는 기준·후보 신호로만 표기</li>
            <li>매매 추천·주문·백필·요청 중 스크래핑 없음</li>
          </ul>
        </Card>
      }
    />
  );
}
