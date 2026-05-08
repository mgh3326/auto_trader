import type { SignalCard as SignalCardData } from "../../types/signals";
import { Pill } from "../../ds";
import { formatRelativeTime } from "../../format/relativeTime";

const MARKET_LABEL: Record<SignalCardData["market"], string> = {
  kr: "KR",
  us: "US",
  crypto: "CRYPTO",
};

const DECISION_LABEL: Record<NonNullable<SignalCardData["decisionLabel"]>, string> = {
  buy: "매수",
  sell: "매도",
  hold: "보유",
  watch: "관찰",
  neutral: "중립",
};

const DECISION_TONE: Record<NonNullable<SignalCardData["decisionLabel"]>, "gain" | "loss" | "warn" | "paper"> = {
  buy: "gain",
  sell: "loss",
  hold: "paper",
  watch: "warn",
  neutral: "paper",
};

const RELATION_LABEL: Record<SignalCardData["relation"], string | null> = {
  held: "보유",
  watchlist: "관심",
  both: "보유·관심",
  none: null,
};

export function SignalCard({
  signal,
  selected,
  onSelect,
  variant = "list",
}: {
  signal: SignalCardData;
  selected?: boolean;
  onSelect?: () => void;
  variant?: "list" | "grid";
}) {
  const decision = signal.decisionLabel ?? "neutral";
  const tone = DECISION_TONE[decision];
  const relationLabel = RELATION_LABEL[signal.relation];
  const ago = formatRelativeTime(signal.generatedAt) ?? "방금";

  const padding = variant === "grid" ? 16 : 12;
  const titleSize = variant === "grid" ? 15 : 14;

  return (
    <button
      type="button"
      data-testid="signal-list-item"
      data-relation={signal.relation}
      data-decision={decision}
      // Only the signal title flows into the accessible name. The decision /
      // relation pills inside are decorative status indicators, not order
      // CTAs — keeping them out of the button's a11y name avoids screen
      // readers announcing this button as a "매수" action.
      aria-label={`${signal.title} 시그널 보기`}
      onClick={onSelect}
      style={{
        width: "100%",
        textAlign: "left",
        padding,
        borderRadius: 12,
        background: selected ? "var(--surface-2)" : "var(--surface)",
        border: `1px solid ${selected ? "var(--accent)" : "var(--border)"}`,
        boxShadow: selected ? "var(--shadow-2)" : "var(--shadow-1)",
        color: "var(--fg)",
        cursor: onSelect ? "pointer" : "default",
        fontFamily: "inherit",
        display: "flex",
        flexDirection: "column",
        gap: 8,
        transition: "border-color 120ms cubic-bezier(0.2,0,0,1), box-shadow 120ms cubic-bezier(0.2,0,0,1)",
      }}
    >
      <div style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
        <Pill tone={tone} size="sm">
          {DECISION_LABEL[decision]}
        </Pill>
        {relationLabel && (
          <Pill tone="accent" size="sm">
            {relationLabel}
          </Pill>
        )}
        <span style={{ fontSize: 11, color: "var(--fg-3)", marginLeft: "auto" }}>{MARKET_LABEL[signal.market]}</span>
      </div>

      <div style={{ fontSize: titleSize, fontWeight: 700, lineHeight: 1.4, color: "var(--fg)" }}>
        {signal.title}
      </div>

      {signal.summary && (
        <div
          style={{
            fontSize: 12,
            color: "var(--fg-2)",
            lineHeight: 1.5,
            display: "-webkit-box",
            WebkitBoxOrient: "vertical",
            WebkitLineClamp: 2,
            overflow: "hidden",
          }}
        >
          {signal.summary}
        </div>
      )}

      <div
        style={{
          display: "flex",
          alignItems: "center",
          gap: 12,
          fontSize: 11,
          color: "var(--fg-3)",
          fontFeatureSettings: '"tnum"',
        }}
      >
        {signal.confidence != null && <span>신뢰도 {signal.confidence}%</span>}
        <span>{ago}</span>
      </div>
    </button>
  );
}
