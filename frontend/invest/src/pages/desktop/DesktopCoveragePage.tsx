import { useEffect, useMemo, useState } from "react";
import { fetchKrActionReadiness } from "../../api/actionReadiness";
import { fetchInvestCoverage } from "../../api/coverage";
import { DesktopShell } from "../../desktop/DesktopShell";
import { Card } from "../../ds";
import type {
  ActionReadinessState,
  KrActionReadinessResponse,
} from "../../types/actionReadiness";
import type {
  CoverageActionability,
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

const ACTION_PRIORITY_LABEL: Record<CoverageActionability["priority"], string> = {
  none: "관찰",
  low: "낮음",
  medium: "중간",
  high: "높음",
  blocked: "차단",
};

const ACTION_PRIORITY_COLOR: Record<CoverageActionability["priority"], string> = {
  none: "#64748b",
  low: "#0ea5e9",
  medium: "#ca8a04",
  high: "#dc2626",
  blocked: "#7c3aed",
};

const ACTION_READINESS_LABEL: Record<ActionReadinessState, string> = {
  ready: "준비됨",
  degraded: "저하",
  blocked: "차단",
  missing: "없음",
  unsupported: "미지원",
  unknown: "확인 불가",
};

const ACTION_READINESS_COLOR: Record<ActionReadinessState, string> = {
  ready: "#16a34a",
  degraded: "#ca8a04",
  blocked: "#dc2626",
  missing: "#d97706",
  unsupported: "#64748b",
  unknown: "#7c3aed",
};

const IMPACT_LABEL: Record<string, string> = {
  none: "영향 없음",
  degrades_report: "리포트 품질 저하",
  blocks_buy_report: "매수 리포트 차단",
  blocks_sell_report: "매도 리포트 차단",
  blocks_all_action_reports: "전체 차단",
};

const AUTHORITY_LABEL: Record<string, string> = {
  kis_live_broker: "KIS live",
  auto_trader_read_model: "read-model",
  manual_or_paper_reference: "참조 계좌",
  external_reference: "외부 참조",
  unsupported: "미지원",
};

const ACTION_LABEL: Record<CoverageActionability["action"], string> = {
  none: "조치 없음",
  monitor: "모니터링",
  investigate: "조사 필요",
  repair_read_model: "read-model 보수 후보",
  backfill_candidate: "백필 후보",
  scheduler_candidate: "스케줄 후보",
  provider_contract_needed: "provider 계약 필요",
  unsupported_no_action: "미지원 · 조치 없음",
};

function ActionabilityBadge({ actionability }: { actionability: CoverageActionability }) {
  const gates = actionability.approvalGates.filter((gate) => gate !== "none");
  return (
    <div style={{ display: "grid", gap: 4, minWidth: 180 }}>
      <span
        title={actionability.reason ?? undefined}
        style={{
          display: "inline-flex",
          width: "fit-content",
          borderRadius: 999,
          padding: "3px 8px",
          fontSize: 12,
          fontWeight: 800,
          color: "white",
          background: ACTION_PRIORITY_COLOR[actionability.priority],
        }}
      >
        {ACTION_PRIORITY_LABEL[actionability.priority]} · {ACTION_LABEL[actionability.action]}
      </span>
      <span style={{ color: "var(--fg-3)", fontSize: 12 }}>
        queue {actionability.queue ?? "none"}
        {gates.length > 0 ? ` · gate ${gates.join(", ")}` : " · no gate"}
      </span>
    </div>
  );
}

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

function ActionReadinessPill({ state }: { state: ActionReadinessState }) {
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
        background: ACTION_READINESS_COLOR[state],
        whiteSpace: "nowrap",
      }}
    >
      {ACTION_READINESS_LABEL[state]}
    </span>
  );
}

function firstKrSymbol(symbols: string): string | undefined {
  return symbols
    .split(",")
    .map((part) => part.trim())
    .find((part) => /^\d{6}$/.test(part));
}

function ReadinessFamilyCard({ family }: { family: KrActionReadinessResponse["families"][number] }) {
  const latest = family.latestDate ?? family.latestAt ?? "-";
  const message = family.blockers[0] ?? family.warnings[0] ?? family.notes[0] ?? "확인된 blocker 없음";
  return (
    <div style={{ border: "1px solid var(--divider)", borderRadius: 12, padding: 12, display: "grid", gap: 8 }}>
      <div style={{ display: "flex", justifyContent: "space-between", gap: 8, alignItems: "center" }}>
        <div>
          <div style={{ fontWeight: 900 }}>{family.labelKo}</div>
          <div style={{ color: "var(--fg-3)", fontSize: 12 }}>{family.key} · {IMPACT_LABEL[family.impact] ?? family.impact}</div>
        </div>
        <ActionReadinessPill state={family.state} />
      </div>
      <div style={{ color: "var(--fg-2)", fontSize: 12 }}>
        authority {AUTHORITY_LABEL[family.authority] ?? family.authority} · latest {latest}
      </div>
      <div style={{ fontFamily: "var(--font-mono)", fontSize: 12, color: "var(--fg-2)", overflowWrap: "anywhere" }}>
        sourceOfTruth: {family.sourceOfTruth}
      </div>
      <div style={{ color: family.blockers.length > 0 ? "#dc2626" : "var(--fg-3)", fontSize: 12 }}>
        {message}
      </div>
      {family.references.length > 0 && (
        <div style={{ color: "var(--fg-3)", fontSize: 12 }}>references: {family.references.join(", ")}</div>
      )}
      <ActionabilityBadge actionability={family.actionability} />
    </div>
  );
}

function ActionReadinessCard({ data }: { data: KrActionReadinessResponse }) {
  const groups = Array.from(new Set(data.families.map((family) => family.category)));
  return (
    <Card>
      <div style={{ display: "grid", gap: 14 }}>
        <div style={{ display: "flex", justifyContent: "space-between", gap: 12, alignItems: "flex-start", flexWrap: "wrap" }}>
          <div>
            <h2 style={{ margin: 0, fontSize: 20 }}>KR 액션 리포트 준비도</h2>
            <p style={{ margin: "6px 0 0", color: "var(--fg-2)", fontSize: 13 }}>
              read-only source dashboard입니다. 매매 추천·주문·백필·스케줄 실행 버튼을 제공하지 않습니다.
            </p>
          </div>
          <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
            <ActionReadinessPill state={data.overallState} />
            <span style={{ color: data.canGenerateBuyReport ? "#16a34a" : "#dc2626", fontWeight: 800 }}>
              매수 리포트 {data.canGenerateBuyReport ? "가능" : "차단"}
            </span>
            <span style={{ color: data.canGenerateSellReport ? "#16a34a" : "#dc2626", fontWeight: 800 }}>
              매도 리포트 {data.canGenerateSellReport ? "가능" : "차단"}
            </span>
          </div>
        </div>

        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(220px, 1fr))", gap: 10 }}>
          <div>
            <div style={{ fontWeight: 800, marginBottom: 6 }}>Blockers</div>
            {(data.blockers.length ? data.blockers : ["현재 전체 blocker 없음"]).slice(0, 6).map((item) => (
              <div key={item} style={{ color: data.blockers.length ? "#dc2626" : "var(--fg-3)", fontSize: 12, marginBottom: 4 }}>{item}</div>
            ))}
          </div>
          <div>
            <div style={{ fontWeight: 800, marginBottom: 6 }}>Degraded signals</div>
            {(data.degradedSignals.length ? data.degradedSignals : ["저하 신호 없음"]).slice(0, 6).map((item) => (
              <div key={item} style={{ color: "var(--fg-3)", fontSize: 12, marginBottom: 4 }}>{item}</div>
            ))}
          </div>
          <div>
            <div style={{ fontWeight: 800, marginBottom: 6 }}>Source policy</div>
            {data.sourcePolicy.slice(0, 4).map((item) => (
              <div key={item} style={{ color: "var(--fg-3)", fontSize: 12, marginBottom: 4 }}>{item}</div>
            ))}
          </div>
        </div>

        {groups.map((group) => (
          <div key={group} style={{ display: "grid", gap: 10 }}>
            <h3 style={{ margin: 0, fontSize: 15 }}>{group}</h3>
            <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(260px, 1fr))", gap: 10 }}>
              {data.families.filter((family) => family.category === group).map((family) => (
                <ReadinessFamilyCard key={family.key} family={family} />
              ))}
            </div>
          </div>
        ))}
      </div>
    </Card>
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
      <td style={{ padding: "12px 10px", borderBottom: "1px solid var(--divider)", fontSize: 12 }}>
        <ActionabilityBadge actionability={surface.actionability} />
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
  const [readiness, setReadiness] = useState<KrActionReadinessResponse | undefined>();
  const [readinessLoading, setReadinessLoading] = useState(true);
  const [readinessErr, setReadinessErr] = useState<string | null>(null);
  const readinessSymbol = market === "kr" ? firstKrSymbol(symbols) : undefined;

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

  useEffect(() => {
    if (market !== "kr") {
      setReadiness(undefined);
      setReadinessLoading(false);
      setReadinessErr(null);
      return;
    }
    const controller = new AbortController();
    setReadinessLoading(true);
    setReadinessErr(null);
    fetchKrActionReadiness({ symbol: readinessSymbol, signal: controller.signal })
      .then((response) => {
        setReadiness(response);
        setReadinessLoading(false);
      })
      .catch((e) => {
        if (controller.signal.aborted) return;
        setReadinessErr(String(e?.message ?? e));
        setReadinessLoading(false);
      });
    return () => controller.abort();
  }, [market, readinessSymbol]);

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
        {market === "kr" && readinessLoading && <Card>KR 액션 리포트 준비도 로딩 중…</Card>}
        {market === "kr" && readinessErr && <Card><span style={{ color: STATE_COLOR.error }}>액션 준비도 API 오류: {readinessErr}</span></Card>}
        {market === "kr" && readiness && !readinessLoading && <ActionReadinessCard data={readiness} />}

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
                <table style={{ width: "100%", borderCollapse: "collapse", minWidth: 1160 }}>
                  <thead>
                    <tr style={{ textAlign: "left", color: "var(--fg-3)", fontSize: 12 }}>
                      <th style={{ padding: "0 10px 8px" }}>Surface</th>
                      <th style={{ padding: "0 10px 8px" }}>State</th>
                      <th style={{ padding: "0 10px 8px" }}>Market</th>
                      <th style={{ padding: "0 10px 8px" }}>Source of truth</th>
                      <th style={{ padding: "0 10px 8px" }}>Latest</th>
                      <th style={{ padding: "0 10px 8px" }}>Counts</th>
                      <th style={{ padding: "0 10px 8px" }}>Actionability</th>
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
                      <span style={{ color: "var(--fg-3)", fontSize: 12 }}>{symbol.market}</span>
                      {Object.entries(symbol.surfaces).map(([name, state]) => <span key={name} style={{ display: "inline-flex", gap: 6, alignItems: "center" }}><span style={{ color: "var(--fg-3)", fontSize: 12 }}>{name}</span><StatePill state={state} /></span>)}
                      <ActionabilityBadge actionability={symbol.actionability} />
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
            <li>Actionability는 승인 게이트를 표시하는 advisory metadata이며 실행 버튼이 아님</li>
          </ul>
        </Card>
      }
    />
  );
}
