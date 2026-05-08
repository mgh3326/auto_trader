import type { AccountSource, AccountSourceVisual, AccountTone } from "../types/invest";
import type { PillTone } from "../ds";

const TONE_STYLE: Record<AccountTone, { color: string; bg: string; border: string }> = {
  navy:   { color: "#dde3ff", bg: "#1e2a55", border: "#3a4a8a" },
  gray:   { color: "#cfd2da", bg: "#2a2d35", border: "#3a3d45" },
  purple: { color: "#e7daff", bg: "#3a2660", border: "#624aa0" },
  green:  { color: "#dcf2e0", bg: "#1f3a2a", border: "#3c6a4d" },
  dashed: { color: "#dbdee5", bg: "#1e2026", border: "#5a5e6a" },
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
  kis_mock: "kis",
  upbit: "upbit",
  toss_manual: "toss",
  isa_manual: "isa",
  pension_manual: "pension",
  alpaca_paper: "paper",
  kiwoom_mock: "paper",
  db_simulated: "paper",
};

export function pillToneForSource(source: AccountSource): PillTone {
  return SOURCE_TO_PILL[source] ?? "paper";
}
