import type { CSSProperties } from "react";
import { Icon, type IconName } from "../ds";
import { useTheme, type Theme } from "./useTheme";

const OPTIONS: { value: Theme; label: string; icon: IconName }[] = [
  { value: "light", label: "라이트", icon: "sun" },
  { value: "system", label: "시스템", icon: "monitor" },
  { value: "dark", label: "다크", icon: "moon" },
];

const TRACK_STYLE: CSSProperties = {
  display: "inline-flex",
  alignItems: "center",
  padding: 2,
  borderRadius: 999,
  background: "var(--surface-2)",
  border: "1px solid var(--border)",
  gap: 2,
};

const SEGMENT_BASE: CSSProperties = {
  display: "inline-flex",
  alignItems: "center",
  justifyContent: "center",
  gap: 6,
  height: 28,
  padding: "0 10px",
  borderRadius: 999,
  border: "none",
  background: "transparent",
  color: "var(--fg-3)",
  fontSize: 12,
  fontWeight: 600,
  cursor: "pointer",
  transition: "background 160ms cubic-bezier(0.2,0,0,1), color 160ms cubic-bezier(0.2,0,0,1)",
};

const SEGMENT_ACTIVE: CSSProperties = {
  background: "var(--surface)",
  color: "var(--fg)",
  boxShadow: "var(--shadow-1)",
};

export type ThemeToggleVariant = "labeled" | "compact";

export function ThemeToggle({ variant = "labeled" }: { variant?: ThemeToggleVariant } = {}) {
  const [theme, setTheme] = useTheme();
  const compact = variant === "compact";
  return (
    <div role="radiogroup" aria-label="테마 선택" style={TRACK_STYLE}>
      {OPTIONS.map((opt) => {
        const active = theme === opt.value;
        return (
          <button
            key={opt.value}
            type="button"
            role="radio"
            aria-checked={active}
            aria-label={opt.label}
            title={opt.label}
            onClick={() => setTheme(opt.value)}
            style={{
              ...SEGMENT_BASE,
              ...(active ? SEGMENT_ACTIVE : null),
              padding: compact ? "0 8px" : SEGMENT_BASE.padding,
            }}
          >
            <Icon name={opt.icon} size={14} />
            {!compact ? <span>{opt.label}</span> : null}
          </button>
        );
      })}
    </div>
  );
}
