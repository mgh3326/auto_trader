import { Card } from "../ds";
import type { CommonPreferredDisparityCard, CommonPreferredDisparityResponse, DisparityState } from "../types/commonPreferredDisparity";

const STATE_LABEL: Record<DisparityState, string> = {
  fresh: "정상",
  partial: "부분",
  stale: "오래됨",
  missing: "없음",
};

const STATE_COLOR: Record<DisparityState, string> = {
  fresh: "#16a34a",
  partial: "#ca8a04",
  stale: "#ca8a04",
  missing: "#dc2626",
};

function fmtPct(value?: number | null) {
  return value == null ? "—" : `${value >= 0 ? "+" : ""}${value.toFixed(2)}%`;
}

function fmtPrice(value?: number | null) {
  return value == null ? "—" : `${Math.round(value).toLocaleString()}원`;
}

function StatePill({ state }: { state: DisparityState }) {
  return (
    <span style={{ borderRadius: 999, padding: "3px 8px", fontSize: 12, fontWeight: 800, color: "white", background: STATE_COLOR[state] }}>
      {STATE_LABEL[state]}
    </span>
  );
}

function DisparityMiniCard({ card }: { card: CommonPreferredDisparityCard }) {
  const window = card.windows.find((w) => w.period === card.primaryWindow) ?? card.windows[0];
  return (
    <div style={{ border: "1px solid var(--divider)", borderRadius: 16, padding: 14, display: "grid", gap: 8 }}>
      <div style={{ display: "flex", justifyContent: "space-between", gap: 10, alignItems: "start" }}>
        <div>
          <div style={{ fontWeight: 900 }}>{card.commonName} / {card.preferredName}</div>
          <div style={{ color: "var(--fg-3)", fontSize: 12 }}>{card.commonSymbol} · {card.preferredSymbol} · {card.source.source}</div>
        </div>
        <StatePill state={card.dataState} />
      </div>
      <div style={{ display: "grid", gridTemplateColumns: "repeat(3, minmax(0, 1fr))", gap: 8, fontFeatureSettings: '"tnum"' }}>
        <div>
          <div style={{ color: "var(--fg-3)", fontSize: 11 }}>괴리율</div>
          <div style={{ fontSize: 22, fontWeight: 900, color: card.tone === "discount" ? "var(--gain)" : card.tone === "premium" ? "var(--loss)" : "var(--fg-1)" }}>{fmtPct(card.disparityPct)}</div>
        </div>
        <div>
          <div style={{ color: "var(--fg-3)", fontSize: 11 }}>보통/우선</div>
          <div style={{ fontSize: 13, fontWeight: 800 }}>{fmtPrice(card.commonPrice)}</div>
          <div style={{ fontSize: 13, fontWeight: 800 }}>{fmtPrice(card.preferredPrice)}</div>
        </div>
        <div>
          <div style={{ color: "var(--fg-3)", fontSize: 11 }}>{card.primaryWindow} z-score</div>
          <div style={{ fontSize: 18, fontWeight: 900 }}>{card.zScore == null ? "—" : card.zScore.toFixed(2)}</div>
          <div style={{ color: "var(--fg-3)", fontSize: 11 }}>n={window?.sampleCount ?? 0}</div>
        </div>
      </div>
      {card.source.asOf && <div style={{ color: "var(--fg-3)", fontSize: 11 }}>asOf {card.source.asOf}</div>}
      {(card.warnings.length > 0 || card.emptyReason) && <div style={{ color: "var(--warn)", fontSize: 12 }}>⚠ {[card.emptyReason, ...card.warnings].filter(Boolean).join(" · ")}</div>}
      <div style={{ color: "var(--fg-3)", fontSize: 12 }}>{card.caution}</div>
    </div>
  );
}

export function CommonPreferredDisparityCardView({ data }: { data: CommonPreferredDisparityResponse }) {
  return (
    <Card>
      <div style={{ display: "flex", justifyContent: "space-between", gap: 12, alignItems: "start", marginBottom: 14 }}>
        <div>
          <h2 style={{ margin: 0, fontSize: 20, letterSpacing: "-0.03em" }}>보통주/우선주 괴리</h2>
          <p style={{ margin: "5px 0 0", color: "var(--fg-2)", fontSize: 13 }}>005930/005935부터 표시하는 read-only 참고 카드입니다.</p>
          <div style={{ marginTop: 6, color: "var(--fg-3)", fontSize: 12 }}>asOf {data.asOf}</div>
        </div>
        <StatePill state={data.state} />
      </div>
      {data.cards.length === 0 ? (
        <div style={{ color: "var(--fg-3)", fontSize: 13 }}>{data.emptyReason ?? "표시할 괴리 데이터가 없습니다."}</div>
      ) : (
        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(260px, 1fr))", gap: 12 }}>
          {data.cards.map((card) => <DisparityMiniCard key={card.id} card={card} />)}
        </div>
      )}
      {(data.warnings.length > 0 || data.notes.length > 0) && (
        <div style={{ marginTop: 12, display: "grid", gap: 4, color: "var(--fg-3)", fontSize: 12 }}>
          {data.warnings.map((warning) => <div key={warning} style={{ color: "var(--warn)" }}>⚠ {warning}</div>)}
          {data.notes.map((note) => <div key={note}>• {note}</div>)}
        </div>
      )}
    </Card>
  );
}
