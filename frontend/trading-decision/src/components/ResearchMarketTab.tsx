import { STAGE_VERDICT_LABEL } from "../i18n/ko";
import type { MarketSignals, StageAnalysis } from "../api/types";

interface Props {
  stage: StageAnalysis | null;
}

function fmt(v: unknown): string {
  if (v === null || v === undefined || v === "") return "—";
  if (typeof v === "number") return v.toString();
  return String(v);
}

export default function ResearchMarketTab({ stage }: Props) {
  if (!stage) return <p>시장 단계 데이터가 없습니다.</p>;
  if (stage.verdict === "unavailable")
    return <p>시장 단계 데이터를 가져올 수 없습니다.</p>;
  const s = stage.signals as MarketSignals;

  return (
    <div>
      <header>
        <span data-verdict={stage.verdict}>
          {STAGE_VERDICT_LABEL[stage.verdict]}
        </span>
        <progress value={stage.confidence} max={100}>
          {stage.confidence}%
        </progress>
      </header>

      <dl>
        <dt>종가</dt>
        <dd>{fmt(s.last_close)}</dd>
        <dt>변동률</dt>
        <dd>{s.change_pct != null ? `${s.change_pct}%` : "—"}</dd>
        <dt>RSI(14)</dt>
        <dd>{fmt(s.rsi_14)}</dd>
        <dt>ATR(14)</dt>
        <dd>{fmt(s.atr_14)}</dd>
        <dt>거래량 비율(20일)</dt>
        <dd>{fmt(s.volume_ratio_20d)}</dd>
        <dt>추세</dt>
        <dd>{fmt(s.trend ?? s.trend_short)}</dd>
        {s.macd_signal != null && (
          <>
            <dt>MACD</dt>
            <dd>{s.macd_signal}</dd>
          </>
        )}
        {s.bollinger_position != null && (
          <>
            <dt>볼린저 위치</dt>
            <dd>{s.bollinger_position}</dd>
          </>
        )}
      </dl>

      {(s.supports?.length ?? 0) + (s.resistances?.length ?? 0) > 0 && (
        <section aria-label="지지/저항">
          <p>
            지지: {(s.supports ?? []).join(", ") || "—"} · 저항:{" "}
            {(s.resistances ?? []).join(", ") || "—"}
          </p>
        </section>
      )}

      {stage.source_freshness && (
        <p>
          데이터 신선도: {stage.source_freshness.newest_age_minutes}분 전 ~{" "}
          {stage.source_freshness.oldest_age_minutes}분 전 (
          {stage.source_freshness.source_count}개 소스)
        </p>
      )}
    </div>
  );
}
