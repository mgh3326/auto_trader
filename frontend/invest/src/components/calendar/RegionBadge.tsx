import type { DisplayRegion } from "./vm";

const STYLES: Record<
  DisplayRegion,
  { bg: string; fg: string; label: string }
> = {
  us: { bg: "var(--accent-soft)", fg: "var(--accent-press)", label: "US" },
  kr: { bg: "var(--surface-2)", fg: "var(--fg-1)", label: "KR" },
};

export function RegionBadge({ region, size = "sm" }: { region: DisplayRegion; size?: "sm" | "md" }) {
  const t = STYLES[region];
  const s =
    size === "sm"
      ? { fontSize: 10, padding: "1px 5px", borderRadius: 4 }
      : { fontSize: 11, padding: "2px 6px", borderRadius: 5 };
  return (
    <span
      style={{
        ...s,
        background: t.bg,
        color: t.fg,
        fontWeight: 700,
        fontFamily: "var(--font-mono)",
        letterSpacing: "0.04em",
        flexShrink: 0,
        display: "inline-block",
      }}
    >
      {t.label}
    </span>
  );
}
