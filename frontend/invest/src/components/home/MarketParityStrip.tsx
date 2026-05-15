import { formatRelativeTime } from "../../format/relativeTime";
import type { MarketParityHookState } from "../../hooks/useMarketParity";
import type { MarketParityCard, MarketParityResponse, MarketParityTone } from "../../types/marketParity";

const TONE_COLOR: Record<MarketParityTone, string> = {
  premium: "var(--loss)",
  discount: "var(--gain)",
  flat: "var(--flat)",
  unknown: "var(--fg-3)",
};

const TONE_LABEL: Record<MarketParityTone, string> = {
  premium: "프리미엄",
  discount: "디스카운트",
  flat: "중립",
  unknown: "미확인",
};

function formatPremium(value: number | null | undefined): string {
  if (value === null || value === undefined || Number.isNaN(value)) return "괴리율 없음";
  const sign = value > 0 ? "+" : "";
  return `${sign}${value.toFixed(2)}%`;
}

function sourceFreshness(card: MarketParityCard): string {
  if (card.source.asOf) {
    const relative = formatRelativeTime(card.source.asOf);
    if (relative) return card.source.stale ? `${relative} · 지연` : relative;
  }
  if (card.source.freshnessSec != null) {
    const minutes = Math.floor(card.source.freshnessSec / 60);
    return minutes < 1 ? "방금" : `${minutes}분 전`;
  }
  return card.source.stale ? "지연 가능" : "시각 미확인";
}

function stateCopy(data: MarketParityResponse): string {
  if (data.state === "fresh") return "실시간 판단 대신 참고용으로만 보세요.";
  if (data.state === "partial") return "일부 다리가 비어 있어 괴리 관찰만 가능합니다.";
  if (data.state === "stale") return "오래된 값이 포함되어 방향성 참고만 가능합니다.";
  if (data.state === "disabled") return "승인되지 않은 데이터 다리는 비활성화되어 있습니다.";
  return data.emptyReason ?? "표시할 괴리 관찰값이 없습니다.";
}

function sourceBadge(card: MarketParityCard) {
  return (
    <span
      style={{
        display: "inline-flex",
        alignItems: "center",
        border: "1px solid var(--border)",
        borderRadius: 999,
        padding: "2px 7px",
        color: "var(--fg-3)",
        fontSize: 10,
        fontWeight: 700,
        maxWidth: "100%",
      }}
      title={card.source.sourceOfTruth}
    >
      {card.source.source}
      {card.source.stale ? " · stale" : ""}
    </span>
  );
}

function cardSubline(card: MarketParityCard): string {
  const parts = [card.baseSymbol ?? card.baseName, card.proxySymbol ?? card.syntheticSymbol].filter(Boolean);
  if (parts.length > 0) return parts.join(" ↔ ");
  if (card.emptyReason) return card.emptyReason;
  return card.formula ?? "관찰식 준비 중";
}

function ParityCard({ card }: { card: MarketParityCard }) {
  const color = TONE_COLOR[card.tone];
  const muted = card.dataState === "missing" || card.dataState === "disabled";
  return (
    <div
      data-testid="market-parity-card"
      style={{
        minHeight: 112,
        padding: 12,
        background: "var(--surface)",
        border: "1px solid var(--border)",
        borderRadius: 14,
        boxShadow: "var(--shadow-1)",
        opacity: muted ? 0.74 : 1,
      }}
    >
      <div style={{ display: "flex", justifyContent: "space-between", gap: 8, alignItems: "flex-start" }}>
        <div style={{ minWidth: 0 }}>
          <div style={{ fontSize: 12, fontWeight: 800, color: "var(--fg-2)", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
            {card.title}
          </div>
          <div style={{ fontSize: 11, color: "var(--fg-3)", marginTop: 3, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
            {cardSubline(card)}
          </div>
        </div>
        {sourceBadge(card)}
      </div>

      <div style={{ display: "flex", alignItems: "baseline", gap: 7, marginTop: 10 }}>
        <span style={{ color, fontSize: 18, fontWeight: 900, fontFeatureSettings: '"tnum"' }}>
          {formatPremium(card.premiumPct)}
        </span>
        <span style={{ color, fontSize: 11, fontWeight: 800 }}>{TONE_LABEL[card.tone]}</span>
      </div>

      <div style={{ display: "flex", justifyContent: "space-between", gap: 8, marginTop: 9, fontSize: 11, color: "var(--fg-3)" }}>
        <span>{sourceFreshness(card)}</span>
        <span>{card.dataState}</span>
      </div>
      {card.source.warnings.length > 0 && (
        <div style={{ marginTop: 6, color: "var(--warn)", fontSize: 10, whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" }}>
          {card.source.warnings[0]}
        </div>
      )}
    </div>
  );
}

function LoadingCards() {
  return (
    <div data-testid="market-parity-loading" style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(180px, 1fr))", gap: 10 }}>
      {Array.from({ length: 3 }).map((_, i) => (
        <div key={i} style={{ minHeight: 112, padding: 12, background: "var(--surface)", border: "1px solid var(--border)", borderRadius: 14, boxShadow: "var(--shadow-1)" }}>
          <div style={{ width: "60%", height: 12, borderRadius: 999, background: "var(--surface-2)" }} />
          <div style={{ width: "34%", height: 22, borderRadius: 999, background: "var(--surface-2)", marginTop: 16 }} />
          <div style={{ width: "70%", height: 10, borderRadius: 999, background: "var(--surface-2)", marginTop: 18 }} />
        </div>
      ))}
    </div>
  );
}

export function MarketParityStrip({ state, reload }: { state: MarketParityHookState; reload?: () => void }) {
  return (
    <section data-testid="market-parity-strip" aria-label="시장 괴리 참고 카드">
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "end", gap: 12, marginBottom: 10 }}>
        <div>
          <h2 style={{ margin: 0, fontSize: 15, fontWeight: 800, color: "var(--fg-2)", letterSpacing: "-0.01em" }}>
            괴리 참고
          </h2>
          <div style={{ marginTop: 3, color: "var(--fg-3)", fontSize: 12 }}>
            ETF·환율·김치프리미엄 괴리 관찰용입니다. 매수·매도 추천이 아닙니다.
          </div>
        </div>
        {state.status === "ready" && (
          <div style={{ color: "var(--fg-3)", fontSize: 11 }}>기준 {formatRelativeTime(state.data.asOf) ?? "시각 미확인"}</div>
        )}
      </div>

      {state.status === "loading" && <LoadingCards />}

      {state.status === "error" && (
        <div role="status" style={{ padding: 14, background: "var(--surface)", border: "1px solid var(--border)", borderRadius: 14, color: "var(--fg-3)", fontSize: 12 }}>
          괴리 참고 카드를 일시적으로 불러오지 못했습니다.
          {reload && (
            <button type="button" onClick={reload} style={{ marginLeft: 8, padding: "3px 9px", borderRadius: 8, border: "1px solid var(--border)", background: "var(--surface-2)", color: "var(--fg-1)", cursor: "pointer" }}>
              재시도
            </button>
          )}
        </div>
      )}

      {state.status === "ready" && state.data.cards.length === 0 && (
        <div role="status" style={{ padding: 14, background: "var(--surface)", border: "1px solid var(--border)", borderRadius: 14, color: "var(--fg-3)", fontSize: 12 }}>
          {stateCopy(state.data)}
        </div>
      )}

      {state.status === "ready" && state.data.cards.length > 0 && (
        <>
          <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(180px, 1fr))", gap: 10 }}>
            {state.data.cards.slice(0, 4).map((card) => <ParityCard key={card.id} card={card} />)}
          </div>
          <div style={{ marginTop: 8, color: state.data.state === "partial" ? "var(--warn)" : "var(--fg-3)", fontSize: 11 }}>
            {stateCopy(state.data)}
            {state.data.warnings.length > 0 ? ` · ${state.data.warnings[0]}` : ""}
          </div>
        </>
      )}
    </section>
  );
}
