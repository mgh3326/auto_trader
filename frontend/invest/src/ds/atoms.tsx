import type { ButtonHTMLAttributes, CSSProperties, HTMLAttributes, ReactNode } from "react";

export type PillTone =
  | "kis"
  | "upbit"
  | "toss"
  | "isa"
  | "pension"
  | "paper"
  | "accent"
  | "gain"
  | "loss"
  | "warn";

export type PillSize = "sm" | "md";

const PILL_TONES: Record<PillTone, { bg: string; fg: string }> = {
  kis: { bg: "var(--pill-kis-bg)", fg: "var(--pill-kis-fg)" },
  upbit: { bg: "var(--pill-upbit-bg)", fg: "var(--pill-upbit-fg)" },
  toss: { bg: "var(--pill-toss-bg)", fg: "var(--pill-toss-fg)" },
  isa: { bg: "var(--pill-isa-bg)", fg: "var(--pill-isa-fg)" },
  pension: { bg: "var(--pill-pension-bg)", fg: "var(--pill-pension-fg)" },
  paper: { bg: "var(--pill-paper-bg)", fg: "var(--pill-paper-fg)" },
  accent: { bg: "var(--accent-soft)", fg: "var(--accent-press)" },
  gain: { bg: "var(--gain-soft)", fg: "var(--gain)" },
  loss: { bg: "var(--loss-soft)", fg: "var(--loss)" },
  warn: { bg: "var(--warn-soft)", fg: "var(--warn)" },
};

export function Pill({
  tone = "paper",
  size = "md",
  children,
}: {
  tone?: PillTone;
  size?: PillSize;
  children: ReactNode;
}) {
  const t = PILL_TONES[tone];
  const s: CSSProperties =
    size === "sm"
      ? { padding: "1px 6px", fontSize: 10, borderRadius: 5 }
      : { padding: "2px 8px", fontSize: 11, borderRadius: 6 };
  return (
    <span
      data-tone={tone}
      data-size={size}
      style={{ ...s, background: t.bg, color: t.fg, fontWeight: 600, display: "inline-block" }}
    >
      {children}
    </span>
  );
}

export type ButtonVariant = "primary" | "secondary" | "ghost" | "danger";
export type ButtonSize = "sm" | "md" | "lg";

const BUTTON_SIZES: Record<ButtonSize, CSSProperties> = {
  sm: { padding: "6px 11px", fontSize: 13, borderRadius: 8 },
  md: { padding: "9px 14px", fontSize: 14, borderRadius: 10 },
  lg: { padding: "12px 18px", fontSize: 15, borderRadius: 12 },
};

const BUTTON_VARIANTS: Record<ButtonVariant, CSSProperties> = {
  primary: { background: "var(--accent)", color: "var(--fg-on-accent)" },
  secondary: { background: "var(--surface-2)", color: "var(--fg-1)" },
  ghost: { background: "transparent", color: "var(--fg-2)" },
  danger: { background: "var(--danger)", color: "var(--fg-on-accent)" },
};

export function Button({
  variant = "primary",
  size = "md",
  children,
  style,
  ...rest
}: ButtonHTMLAttributes<HTMLButtonElement> & { variant?: ButtonVariant; size?: ButtonSize }) {
  return (
    <button
      style={{
        fontFamily: "inherit",
        fontWeight: 600,
        cursor: rest.disabled ? "not-allowed" : "pointer",
        border: "none",
        display: "inline-flex",
        alignItems: "center",
        gap: 6,
        whiteSpace: "nowrap",
        flexShrink: 0,
        transition: "all 120ms cubic-bezier(0.2,0,0,1)",
        opacity: rest.disabled ? 0.4 : 1,
        ...BUTTON_SIZES[size],
        ...BUTTON_VARIANTS[variant],
        ...style,
      }}
      {...rest}
    >
      {children}
    </button>
  );
}

export function Card({
  children,
  padded = true,
  soft = false,
  style,
  ...rest
}: HTMLAttributes<HTMLDivElement> & { padded?: boolean; soft?: boolean }) {
  return (
    <div
      data-soft={soft || undefined}
      style={{
        background: soft ? "var(--surface-2)" : "var(--surface)",
        border: soft ? "none" : "1px solid var(--border)",
        borderRadius: 16,
        boxShadow: soft ? "none" : "var(--shadow-1)",
        padding: padded ? 20 : 0,
        ...style,
      }}
      {...rest}
    >
      {children}
    </div>
  );
}

export function Hairline({ style, ...rest }: HTMLAttributes<HTMLDivElement>) {
  return <div style={{ height: 1, background: "var(--divider)", ...style }} {...rest} />;
}

export type Direction = "up" | "down" | "mixed" | "flat";

const ARROW_MAP: Record<Direction, { glyph: string; color: string }> = {
  up: { glyph: "▲", color: "var(--gain)" },
  down: { glyph: "▼", color: "var(--loss)" },
  mixed: { glyph: "◆", color: "var(--warn)" },
  flat: { glyph: "·", color: "var(--flat)" },
};

export function Arrow({ dir, ...rest }: { dir: Direction } & HTMLAttributes<HTMLSpanElement>) {
  const { glyph, color } = ARROW_MAP[dir];
  return (
    <span style={{ color }} {...rest}>
      {glyph}
    </span>
  );
}

export function PL({
  value,
  pct,
  krw = true,
  size = 13,
}: {
  value: number;
  pct: number;
  krw?: boolean;
  size?: number;
}) {
  const dir: Direction = value > 0 ? "up" : value < 0 ? "down" : "flat";
  const color =
    dir === "up" ? "var(--gain)" : dir === "down" ? "var(--loss)" : "var(--flat)";
  const sign = value > 0 ? "+" : value < 0 ? "−" : "";
  const abs = Math.abs(value);
  const formatted = krw
    ? `${sign}${abs.toLocaleString("ko-KR")}`
    : `${sign}${abs.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
  return (
    <span
      data-testid="pl"
      data-dir={dir}
      style={{ color, fontWeight: 600, fontFeatureSettings: '"tnum"', fontSize: size }}
    >
      <Arrow dir={dir} /> {formatted} · {sign}
      {Math.abs(pct).toFixed(2)}%
    </span>
  );
}

export function Krw({
  v,
  size = 14,
  weight = 600,
}: {
  v: number | null | undefined;
  size?: number;
  weight?: number;
}) {
  return (
    <span style={{ fontFeatureSettings: '"tnum"', fontWeight: weight, fontSize: size }}>
      {v == null ? "−" : `₩${Math.round(v).toLocaleString("ko-KR")}`}
    </span>
  );
}

export function Usd({
  v,
  size = 14,
  weight = 600,
}: {
  v: number | null | undefined;
  size?: number;
  weight?: number;
}) {
  return (
    <span style={{ fontFeatureSettings: '"tnum"', fontWeight: weight, fontSize: size }}>
      {v == null
        ? "−"
        : `$${v.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`}
    </span>
  );
}

export function Sparkline({
  points,
  color,
  height = 36,
  width = 120,
}: {
  points: number[];
  color: string;
  height?: number;
  width?: number;
}) {
  if (points.length < 2) return <svg width={width} height={height} />;
  const max = Math.max(...points);
  const min = Math.min(...points);
  const range = max - min || 1;
  const step = width / (points.length - 1);
  const path = points
    .map((p, i) => `${i * step},${height - ((p - min) / range) * (height - 4) - 2}`)
    .join(" ");
  return (
    <svg
      width={width}
      height={height}
      viewBox={`0 0 ${width} ${height}`}
      preserveAspectRatio="none"
      style={{ display: "block" }}
    >
      <polyline
        fill="none"
        stroke={color}
        strokeWidth="2"
        strokeLinecap="round"
        strokeLinejoin="round"
        points={path}
      />
    </svg>
  );
}

export type IconName =
  | "home"
  | "bell"
  | "chart"
  | "search"
  | "calendar"
  | "chev"
  | "arrowOut"
  | "info"
  | "settings"
  | "plus"
  | "refresh"
  | "flash"
  | "person"
  | "sun"
  | "moon"
  | "monitor"
  | "heart"
  | "clock"
  | "expandLeft";

const ICON_PATHS: Record<IconName, ReactNode> = {
  home: <path d="M3 9.5L12 3l9 6.5V20a1 1 0 0 1-1 1h-5v-7H9v7H4a1 1 0 0 1-1-1V9.5z" />,
  bell: (
    <>
      <path d="M18 8a6 6 0 1 0-12 0c0 7-3 9-3 9h18s-3-2-3-9" />
      <path d="M13.7 21a2 2 0 0 1-3.4 0" />
    </>
  ),
  chart: <path d="M18 20V10M12 20V4M6 20v-6" />,
  search: (
    <>
      <circle cx="11" cy="11" r="7" />
      <path d="M21 21l-4.3-4.3" />
    </>
  ),
  calendar: (
    <>
      <rect x="3" y="4" width="18" height="18" rx="2" />
      <path d="M16 2v4M8 2v4M3 10h18" />
    </>
  ),
  chev: <path d="M9 18l6-6-6-6" />,
  arrowOut: <path d="M7 17L17 7M9 7h8v8" />,
  info: (
    <>
      <circle cx="12" cy="12" r="9" />
      <path d="M12 11v5M12 8v.01" />
    </>
  ),
  settings: (
    <>
      <circle cx="12" cy="12" r="3" />
      <path d="M19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 0 1-4 0v-.1a1.7 1.7 0 0 0-1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 0 1 0-4h.1a1.7 1.7 0 0 0 1.5-1 1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3h.1a1.7 1.7 0 0 0 1-1.5V3a2 2 0 0 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8v.1a1.7 1.7 0 0 0 1.5 1H21a2 2 0 0 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z" />
    </>
  ),
  plus: <path d="M12 5v14M5 12h14" />,
  refresh: (
    <>
      <path d="M21 12a9 9 0 1 1-3-6.7L21 8" />
      <path d="M21 3v5h-5" />
    </>
  ),
  flash: <path d="M13 2L3 14h7l-1 8 10-12h-7l1-8z" />,
  person: (
    <>
      <circle cx="12" cy="8" r="4" />
      <path d="M4 20c0-4 3.6-7 8-7s8 3 8 7" />
    </>
  ),
  sun: (
    <>
      <circle cx="12" cy="12" r="4" />
      <path d="M12 2v2M12 20v2M4.93 4.93l1.41 1.41M17.66 17.66l1.41 1.41M2 12h2M20 12h2M4.93 19.07l1.41-1.41M17.66 6.34l1.41-1.41" />
    </>
  ),
  moon: <path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z" />,
  monitor: (
    <>
      <rect x="2" y="4" width="20" height="14" rx="2" />
      <path d="M8 21h8M12 18v3" />
    </>
  ),
  heart: (
    <path d="M20.84 4.61a5.5 5.5 0 0 0-7.78 0L12 5.67l-1.06-1.06a5.5 5.5 0 0 0-7.78 7.78l1.06 1.06L12 21.23l7.78-7.78 1.06-1.06a5.5 5.5 0 0 0 0-7.78z" />
  ),
  clock: (
    <>
      <circle cx="12" cy="12" r="9" />
      <path d="M12 7v5l3 2" />
    </>
  ),
  expandLeft: <path d="M13 6l-6 6 6 6M19 6l-6 6 6 6" />,
};

export function Icon({ name, size = 20 }: { name: IconName; size?: number }) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
      style={{ flex: "0 0 auto" }}
    >
      {ICON_PATHS[name]}
    </svg>
  );
}
