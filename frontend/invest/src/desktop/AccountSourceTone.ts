import type { AccountSource, AccountSourceVisual, AccountTone } from "../types/invest";
import type { PillTone } from "../ds";

// Each tone maps onto the design system's source-pill tokens so the
// account chrome themes correctly under both light and dark.
const TONE_STYLE: Record<AccountTone, { color: string; bg: string; border: string }> = {
  navy:   { color: "var(--pill-kis-fg)",     bg: "var(--pill-kis-bg)",     border: "var(--border)" },
  gray:   { color: "var(--pill-pension-fg)", bg: "var(--pill-pension-bg)", border: "var(--border)" },
  purple: { color: "var(--pill-toss-fg)",    bg: "var(--pill-toss-bg)",    border: "var(--border)" },
  green:  { color: "var(--pill-upbit-fg)",   bg: "var(--pill-upbit-bg)",   border: "var(--border)" },
  dashed: { color: "var(--pill-paper-fg)",   bg: "var(--pill-paper-bg)",   border: "var(--border)" },
};

export function styleForVisual(v: AccountSourceVisual) {
  const s = TONE_STYLE[v.tone];
  return {
    color: s.color,
    background: s.bg,
    borderStyle: v.tone === "dashed" ? "dashed" : "solid" as const,
    borderColor: s.border,
    borderWidth: 1,
  };
}

export function visualBySource(
  visuals: AccountSourceVisual[],
  source: string,
): AccountSourceVisual | undefined {
  return visuals.find((v) => v.source === source);
}

const SOURCE_TO_PILL: Record<AccountSource, PillTone> = {
  kis: "kis",
  kis_mock: "paper",
  upbit: "upbit",
  toss_manual: "toss",
  toss_api: "toss",
  isa_manual: "isa",
  pension_manual: "pension",
  alpaca_paper: "paper",
  kiwoom_mock: "paper",
  db_simulated: "paper",
};

export function pillToneForSource(source: AccountSource): PillTone {
  return SOURCE_TO_PILL[source] ?? "paper";
}
