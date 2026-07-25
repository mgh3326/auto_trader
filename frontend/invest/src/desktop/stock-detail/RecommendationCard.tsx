// ROB-692 — on-demand deterministic recommendation card for the stock detail
// page. Surfaces `build_recommendation_for_equity` (action/confidence/
// buy_zones/sell_targets/stop_loss/reasoning) plus, for a buy setup, an R:R
// chip that reuses the ROB-690 risk_reward helper server-side
// (`StockDetailRecommendationResponse.trade_setup`).
//
// On-demand by design: the underlying `analyze_stock_impl` call is the
// heaviest fetch on this page (OHLCV + quote + indicators + opinions +
// valuation, optionally peers). It only runs when the operator clicks the
// button below — never in the page's eager load effect.
//
// Self-contained parse/format — does NOT import from
// InvestmentReportBundleContent.tsx (that file is ROB-693's; kept disjoint).

import { Button, Card, Pill } from "../../ds";
import type { RecoZone, StockDetailRecommendationResponse } from "../../types/stockDetail";

const ACTION_LABELS: Record<StockDetailRecommendationResponse["action"], string> = {
  buy: "매수",
  hold: "관망",
  sell: "매도",
};

const ACTION_TONES: Record<StockDetailRecommendationResponse["action"], "gain" | "loss" | "paper"> = {
  buy: "gain",
  hold: "paper",
  sell: "loss",
};

const CONFIDENCE_LABELS: Record<StockDetailRecommendationResponse["confidence"], string> = {
  high: "높음",
  medium: "보통",
  low: "낮음",
};

function fmtPrice(value: number | null | undefined): string {
  if (value == null) return "−";
  return value.toLocaleString("ko-KR", { maximumFractionDigits: 4 });
}

function ZoneList({ title, zones }: { title: string; zones: RecoZone[] }) {
  if (zones.length === 0) return null;
  return (
    <div style={{ display: "grid", gap: 4 }}>
      <strong style={{ fontSize: 12, color: "var(--fg-3)" }}>{title}</strong>
      <ul style={{ margin: 0, paddingLeft: 18, display: "grid", gap: 2 }}>
        {zones.map((zone, idx) => (
          <li key={`${zone.type}-${idx}`} style={{ fontSize: 13 }}>
            {fmtPrice(zone.price)}
            <span style={{ color: "var(--fg-3)", marginLeft: 6, fontSize: 12 }}>{zone.reasoning}</span>
          </li>
        ))}
      </ul>
    </div>
  );
}

export function RecommendationCard({
  market,
  data,
  loading,
  error,
  onLoad,
}: {
  market: "kr" | "us" | "crypto";
  data: StockDetailRecommendationResponse | undefined;
  loading: boolean;
  error: string | undefined;
  onLoad: () => void;
}) {
  if (market === "crypto") return null;

  return (
    <Card data-testid="stock-detail-recommendation">
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "baseline", gap: 8 }}>
        <div>
          <h2 style={{ margin: "0 0 4px", fontSize: 16 }}>추천</h2>
          <p style={{ margin: 0, fontSize: 12, color: "var(--fg-3)" }}>
            결정론적 규칙 기반 action · 신뢰도 · 진입/목표/손절 (요청 시 계산)
          </p>
        </div>
        {data ? (
          <Button size="sm" variant="secondary" onClick={onLoad} disabled={loading}>
            {loading ? "계산 중…" : "다시 계산"}
          </Button>
        ) : null}
      </div>

      {!data && !loading && !error ? (
        <div style={{ marginTop: 12 }}>
          <Button size="sm" variant="primary" onClick={onLoad}>
            추천 실행 / R:R 보기
          </Button>
        </div>
      ) : null}

      {loading && !data ? (
        <p style={{ margin: "12px 0 0", color: "var(--fg-3)" }}>불러오는 중입니다…</p>
      ) : null}

      {error ? (
        <div style={{ marginTop: 12, display: "grid", gap: 8 }}>
          <span style={{ color: "var(--danger)", fontSize: 13 }}>오류: {error}</span>
          <div>
            <Button size="sm" variant="secondary" onClick={onLoad} disabled={loading}>
              다시 시도
            </Button>
          </div>
        </div>
      ) : null}

      {data ? (
        <div style={{ marginTop: 12, display: "grid", gap: 10 }}>
          <div style={{ display: "flex", gap: 6, flexWrap: "wrap", alignItems: "center" }}>
            <Pill tone={ACTION_TONES[data.action]}>{ACTION_LABELS[data.action]}</Pill>
            <Pill tone="accent" size="sm">
              신뢰도 {CONFIDENCE_LABELS[data.confidence]}
            </Pill>
            {data.rsi14 != null ? (
              <Pill tone="paper" size="sm">
                RSI {data.rsi14.toFixed(1)}
              </Pill>
            ) : null}
          </div>

          {data.reasoning ? (
            <p style={{ margin: 0, fontSize: 13, color: "var(--fg-2)", lineHeight: 1.5 }}>{data.reasoning}</p>
          ) : null}

          <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(140px, 1fr))", gap: 10 }}>
            <ZoneList title="매수 구간" zones={data.buy_zones} />
            <ZoneList title="목표가" zones={data.sell_targets} />
          </div>

          {data.stop_loss != null ? (
            <div style={{ fontSize: 13 }}>
              <strong style={{ color: "var(--fg-3)", fontWeight: 600, marginRight: 6 }}>손절가</strong>
              {fmtPrice(data.stop_loss)}
            </div>
          ) : null}

          {data.trade_setup ? (
            <div style={{ display: "flex", gap: 6, flexWrap: "wrap", alignItems: "center" }}>
              <Pill tone={data.trade_setup.direction === "long" ? "gain" : "warn"} size="sm">
                {data.trade_setup.direction === "long" ? "롱" : "숏"}
              </Pill>
              <span style={{ fontSize: 12, color: "var(--fg-2)" }}>
                손익비 R:R {data.trade_setup.rr_ratio} · 리스크 {data.trade_setup.risk_pct}% · 리워드{" "}
                {data.trade_setup.reward_pct}%
              </span>
            </div>
          ) : null}

          {data.insufficient_inputs.length > 0 ? (
            <p style={{ margin: 0, fontSize: 12, color: "var(--fg-3)" }}>
              데이터 부족: {data.insufficient_inputs.join(", ")}
            </p>
          ) : null}
        </div>
      ) : null}
    </Card>
  );
}
