import type { DisplayOwnership } from "./vm";

const MAP: Record<
  Exclude<DisplayOwnership, null>,
  { label: string; bg: string; fg: string }
> = {
  holdings: { label: "보유", bg: "var(--gain-soft)", fg: "var(--gain)" },
  watchlist: { label: "관심", bg: "var(--accent-soft)", fg: "var(--accent-press)" },
  major: { label: "주요", bg: "var(--warn-soft)", fg: "var(--warn)" },
};

export function OwnershipTag({ own }: { own: DisplayOwnership }) {
  if (!own) return null;
  const t = MAP[own];
  return (
    <span
      style={{
        fontSize: 11,
        fontWeight: 600,
        color: t.fg,
        background: t.bg,
        padding: "1px 6px",
        borderRadius: 4,
        marginLeft: 6,
        flexShrink: 0,
      }}
    >
      {t.label}
    </span>
  );
}
