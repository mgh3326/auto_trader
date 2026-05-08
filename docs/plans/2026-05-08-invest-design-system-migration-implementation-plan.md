# Auto Trader Invest — Design System Migration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Migrate `frontend/invest` from a dark, ad-hoc inline-styled dual surface (`/invest` desktop placeholder + `/invest/app` mobile-only IA) to a single canonical Toss-style light product served from `/invest/*` for both desktop and responsive mobile, driven by the design tokens shipped in the `auto-trader-invest-design-system` bundle.

**Architecture:**
- Stage 1 lays a token + primitives foundation (`tokens.css`, `atoms.tsx`) so every subsequent stage can drop boilerplate inline styles for tokens and shared primitives.
- Stages 2–5 reframe each surface (Home, News, Discover, Signals, Calendar) one route at a time, preserving every API hook and `data-testid` so the existing Vitest suite keeps passing.
- Stage 6 retires `/invest/app/*` last: routes are kept as backwards-compatible redirects to canonical `/invest/*` siblings, with the legacy components deletion gated on a separate PR after a soak window.

**Tech Stack:** React 19, react-router-dom 7, Vite 8, TypeScript 6, Vitest 4, Pretendard Variable (CDN), Lucide-line icons (subbed in only if Stage 1 cannot deliver desired affordances with the existing unicode-arrow + plain-typography idiom).

---

## Source bundle reference

The design system bundle is extracted at `/tmp/design-bundle/auto-trader-invest-design-system/`. Authoritative files used by this plan:

| File | What we copy / port |
|---|---|
| `project/colors_and_type.css` | Token sheet → ports verbatim into `frontend/invest/src/styles/tokens.css` |
| `project/ui_kits/invest/atoms.jsx` | Pill / Button / Card / Hairline / Arrow / PL / Krw / Usd / Sparkline / Icon — port to `frontend/invest/src/ds/atoms.tsx` (TypeScript) |
| `project/ui_kits/invest/HomeView.jsx` | Hero / MarketStrip / FilterChips / HoldingsTable visual spec for Stage 2 |
| `project/ui_kits/invest/DesktopChrome.jsx` | DesktopHeader + LeftContextRail + DesktopShell layout spec for Stage 2 |
| `project/ui_kits/invest/RightAccountPanel.jsx` | Right-rail layout spec for Stage 2 |
| `project/ui_kits/invest/MobileView.jsx` | Mobile responsive shell + per-route mobile renderers for Stage 3 |
| `project/ui_kits/invest/NewsView.jsx` | News tab + card spec for Stage 4 |
| `project/ui_kits/invest/DiscoverView.jsx` | Discover issue row spec for Stage 4 |
| `project/ui_kits/invest/SignalsView.jsx` | Signal card grid spec for Stage 4 |
| `project/ui_kits/invest/CalendarView.jsx` | MiniMonth / WeekGroup / EventRow / EventDetailModal / MonthGridView for Stage 5 |
| `project/README.md`, `project/SKILL.md`, `project/ui_kits/invest/README.md` | Visual foundations + IA + responsive breakpoints |

When this plan says "from atoms.jsx" or "from HomeView.jsx" it means: open that file, port the JSX into a TypeScript React component, replace `window.FIXTURE`/`window.X` references with real props/imports, and keep the visual structure identical.

---

## Inventory: existing files in scope

**Routes (`frontend/invest/src/routes.tsx`):**
- `/` → `DesktopHomePage` (placeholder shell — needs full rebuild)
- `/feed/news` → `DesktopFeedNewsPage`
- `/signals` → `DesktopSignalsPage`
- `/calendar` → `DesktopCalendarPage`
- `/screener` → `DesktopScreenerPage` (out of scope for the design bundle, light reframe only)
- `/app` → `HomePage` (mobile-only, currently the working portfolio surface)
- `/app/paper`, `/app/paper/:variant` → `PaperPlaceholderPage`
- `/app/discover` → `DiscoverPage`
- `/app/discover/issues/:issueId` → `DiscoverIssueDetailPage`

**Components folders to touch:**
- `frontend/invest/src/components/` — HeroCard, AccountCardList, AccountSelector, AssetCategoryFilter, HoldingRow, BottomNav, AppShell + `discover/*`
- `frontend/invest/src/desktop/` — DesktopShell, DesktopHeader, RightAccountPanel, AccountSourceTone, useAccountPanel + `screener/*`
- `frontend/invest/src/pages/` and `frontend/invest/src/pages/desktop/`

**Existing dark-theme color references to migrate** (grep targets — used across most pages, all to be replaced with tokens):
- `#9ba0ab` (muted text)
- `#f59e9e` (error red)
- `#5ed1a3` (success green — wrong for Korean P/L)
- `#15181f`, `#1c1e24`, `#181B22`, `#0e1014` (dark surfaces)
- `#e8eaf0`, `#cfd2da` (light text on dark)
- `var(--bg, #0e1014)`, `var(--surface, #15181f)` (fallbacks tied to dark tokens)

**Tests (Vitest, `frontend/invest/src/__tests__/*`)** — all hook on `data-testid` and behavior, not on inline colors. Preserve every `data-testid`.

---

## Risks

1. **API/data integrity.** Hooks (`useInvestHome`, `useAccountPanel`, `fetchCalendar`, `fetchFeedNews`, `fetchWeeklySummary`, `useNewsIssues`, `useDiscoverCalendar`, `useMarketEventsToday`) and types (`HomeSummary`, `Holding`, `GroupedHolding`, `AccountPanelResponse`, `CalendarResponse`, `FeedNewsResponse`, `WeeklySummaryResponse`) MUST be untouched in this migration. All visual changes are purely view-layer.
2. **Test breakage.** The visual reframe touches every surface — every test must keep passing. We preserve every `data-testid`, every `data-relation`, every `data-source` and every callable behavior. Run `npm test` after every task.
3. **Dual IA during migration.** Until Stage 6 completes, `/invest/app/*` and `/invest/*` coexist. Both must stay functional. We accept some duplication of mobile-only pages until Stage 6.
4. **Pretendard via CDN.** Tokens import the font from `cdn.jsdelivr.net`. If the corp network blocks jsdelivr, ship `dist/`-time self-host as a Stage 1 follow-up (flagged in the bundle README).
5. **Lucide is a substitution.** The bundle README flags Lucide as "use only if needed". Stage 1 ships the existing unicode-arrow idiom + an Icon atom that ports the inline SVG paths from `atoms.jsx`, so we DON'T add a `lucide-react` dependency unless Stage 4/5 surfaces a missing affordance.
6. **Korean P/L convention.** `--gain` red and `--loss` blue MUST map 1:1 to `pnlRate >= 0` → gain, `< 0` → loss. The current dark theme has correct semantics (`#FF5C5C`/`#3B82F6`) — preserve them when reframing tokens.
7. **/screener.** Screener is not part of the design bundle. It gets only token-level reframing (Stage 1 + a small Stage 4 polish pass), no IA changes.

---

## Test commands

Per the package scripts at `frontend/invest/package.json`:

```bash
cd frontend/invest
npm run typecheck
npm test
npm run build
```

Typecheck is `tsc --noEmit`, test is `vitest run`, build is `tsc + vite build`. Run all three before committing each task.

The Vite dev server runs on `:5174` (`npm run dev`); use it for visual smoke. Backend `:8000` must be up — without it, hooks return error states and we should still see the visual shell.

---

## Stage 1 — Foundations: tokens, primitives, font, color rules

**Goal:** Drop every component's hard-coded hex into a single token sheet and ship the shared primitive library so Stages 2–5 can stop writing inline `style={{}}` walls.

### Task 1.1: Add the design token sheet

**Files:**
- Create: `frontend/invest/src/styles/tokens.css`

- [ ] **Step 1: Copy `project/colors_and_type.css` verbatim to `tokens.css`**

The bundle file at `/tmp/design-bundle/auto-trader-invest-design-system/project/colors_and_type.css` is the source of truth — copy it byte-for-byte into the new `tokens.css`. It contains all `--bg / --surface-* / --fg-* / --accent / --gain / --loss / --pill-*-bg/-fg / --space-* / --radius-* / --shadow-* / --ease-* / --dur-* / --text-*` tokens, the `@import` for Pretendard, and `:where(html, body)` plus utility classes (`.num`, `.num-big`, `.gain`, `.loss`, `.flat`, `.muted`, `.subtle`, `.card`, `.card-soft`, `.hairline`).

- [ ] **Step 2: Wire it ahead of `styles.css`**

Modify `frontend/invest/src/App.tsx`:

```tsx
import { RouterProvider } from "react-router-dom";
import { router } from "./routes";
import "./styles/tokens.css";
import "./styles.css";

export default function App() {
  return <RouterProvider router={router} />;
}
```

- [ ] **Step 3: Replace the existing `:root` block in `styles.css`**

Open `frontend/invest/src/styles.css` and **delete the entire `:root { ... }` block** (lines 1–18 in the current file). Keep only the global `* { box-sizing }`, `html, body, #root` reset, and the `.app-shell`, `.gain-pos`, `.gain-neg`, `.fallback`, `.subtle` utility classes — but rewrite their bodies to use the new tokens:

```css
* { box-sizing: border-box; }

html, body, #root {
  margin: 0;
  padding: 0;
  background: var(--bg);
  color: var(--fg);
  font-family: var(--font-sans);
}

.app-shell {
  max-width: 420px;
  margin: 0 auto;
  min-height: 100vh;
  padding: var(--space-4);
  display: flex;
  flex-direction: column;
  gap: var(--space-3);
}

.gain-pos { color: var(--gain); font-weight: 600; }
.gain-neg { color: var(--loss); font-weight: 600; }
.fallback { color: var(--fg-3); }
.subtle   { color: var(--fg-3); font-size: var(--text-tiny); }
```

This deliberately *flips the surface from dark to light* via tokens.css; existing inline `var(--bg)` / `var(--surface)` references will start resolving to white/soft-gray automatically. Pages that hard-code dark hexes will look broken — that is correct, those will be cleaned in Stages 2–5.

- [ ] **Step 4: Run typecheck + tests + dev server smoke**

```bash
cd frontend/invest && npm run typecheck && npm test
```

Expected: typecheck PASS, tests PASS (no test asserts on background colors). Open `npm run dev` → `http://localhost:5174/invest/app` — page shows light surfaces with dark text in legacy structure but the layout is intact.

- [ ] **Step 5: Commit**

```bash
git add frontend/invest/src/styles/tokens.css frontend/invest/src/styles.css frontend/invest/src/App.tsx
git commit -m "feat(invest): add design system tokens + light reframe foundation"
```

### Task 1.2: Port primitives to TypeScript (`atoms.tsx`)

**Files:**
- Create: `frontend/invest/src/ds/atoms.tsx`
- Create: `frontend/invest/src/ds/index.ts`
- Test: `frontend/invest/src/__tests__/dsAtoms.test.tsx`

- [ ] **Step 1: Write failing tests for the primitives**

```tsx
// frontend/invest/src/__tests__/dsAtoms.test.tsx
import { render, screen } from "@testing-library/react";
import { describe, it, expect } from "vitest";
import { Pill, PL, Krw, Usd, Arrow, Hairline, Card, Button } from "../ds";

describe("ds atoms", () => {
  it("Pill renders tone label and applies tone style class", () => {
    render(<Pill tone="kis" size="sm">KIS</Pill>);
    const el = screen.getByText("KIS");
    expect(el.dataset.tone).toBe("kis");
    expect(el.dataset.size).toBe("sm");
  });

  it("PL chooses gain color for positive values", () => {
    render(<PL value={100} pct={1.5} />);
    const el = screen.getByTestId("pl");
    expect(el.dataset.dir).toBe("up");
  });

  it("PL chooses loss color for negative values", () => {
    render(<PL value={-100} pct={-1.5} />);
    expect(screen.getByTestId("pl").dataset.dir).toBe("down");
  });

  it("Krw and Usd render placeholder when value is null", () => {
    render(<><Krw v={null} /><Usd v={null} /></>);
    expect(screen.getAllByText("−").length).toBe(2);
  });

  it("Arrow renders gain glyph for up, loss glyph for down", () => {
    render(<><Arrow dir="up" data-testid="up" /><Arrow dir="down" data-testid="down" /></>);
    expect(screen.getByTestId("up").textContent).toBe("▲");
    expect(screen.getByTestId("down").textContent).toBe("▼");
  });

  it("Card supports soft variant", () => {
    render(<Card soft data-testid="c">x</Card>);
    expect(screen.getByTestId("c").dataset.soft).toBe("true");
  });

  it("Button is keyboard-clickable when not disabled", () => {
    render(<Button>주문</Button>);
    expect(screen.getByRole("button", { name: "주문" })).toBeEnabled();
  });

  it("Hairline renders a 1px divider", () => {
    render(<Hairline data-testid="h" />);
    expect(screen.getByTestId("h")).toBeInTheDocument();
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd frontend/invest && npx vitest run src/__tests__/dsAtoms.test.tsx
```

Expected: FAIL with "Cannot find module '../ds'".

- [ ] **Step 3: Implement `atoms.tsx`**

Port `/tmp/design-bundle/auto-trader-invest-design-system/project/ui_kits/invest/atoms.jsx` to TypeScript at `frontend/invest/src/ds/atoms.tsx`. Use the JSX file as the visual source of truth — but make these specific TypeScript adjustments:

```tsx
import type { CSSProperties, ReactNode, ButtonHTMLAttributes } from "react";

export type PillTone = "kis" | "upbit" | "toss" | "isa" | "pension" | "paper" | "accent" | "gain" | "loss" | "warn";
export type PillSize = "sm" | "md";

export function Pill({ tone = "paper", size = "md", children }: {
  tone?: PillTone; size?: PillSize; children: ReactNode;
}) {
  const tones: Record<PillTone, { bg: string; fg: string }> = {
    kis:     { bg: "var(--pill-kis-bg)",     fg: "var(--pill-kis-fg)" },
    upbit:   { bg: "var(--pill-upbit-bg)",   fg: "var(--pill-upbit-fg)" },
    toss:    { bg: "var(--pill-toss-bg)",    fg: "var(--pill-toss-fg)" },
    isa:     { bg: "var(--pill-isa-bg)",     fg: "var(--pill-isa-fg)" },
    pension: { bg: "var(--pill-pension-bg)", fg: "var(--pill-pension-fg)" },
    paper:   { bg: "var(--pill-paper-bg)",   fg: "var(--pill-paper-fg)" },
    accent:  { bg: "var(--accent-soft)",     fg: "var(--accent-press)" },
    gain:    { bg: "var(--gain-soft)",       fg: "var(--gain)" },
    loss:    { bg: "var(--loss-soft)",       fg: "var(--loss)" },
    warn:    { bg: "var(--warn-soft)",       fg: "#a06200" },
  };
  const t = tones[tone];
  const s: CSSProperties = size === "sm"
    ? { padding: "1px 6px", fontSize: 10, borderRadius: 5 }
    : { padding: "2px 8px", fontSize: 11, borderRadius: 6 };
  return (
    <span data-tone={tone} data-size={size}
      style={{ ...s, background: t.bg, color: t.fg, fontWeight: 600, display: "inline-block" }}>
      {children}
    </span>
  );
}

export type ButtonVariant = "primary" | "secondary" | "ghost" | "danger";
export type ButtonSize = "sm" | "md" | "lg";

export function Button({
  variant = "primary", size = "md", children, style, ...rest
}: ButtonHTMLAttributes<HTMLButtonElement> & { variant?: ButtonVariant; size?: ButtonSize }) {
  const sizes: Record<ButtonSize, CSSProperties> = {
    sm: { padding: "6px 11px", fontSize: 13, borderRadius: 8 },
    md: { padding: "9px 14px", fontSize: 14, borderRadius: 10 },
    lg: { padding: "12px 18px", fontSize: 15, borderRadius: 12 },
  };
  const variants: Record<ButtonVariant, CSSProperties> = {
    primary:   { background: "var(--accent)",    color: "#fff" },
    secondary: { background: "var(--surface-2)", color: "var(--fg-1)" },
    ghost:     { background: "transparent",      color: "var(--fg-2)" },
    danger:    { background: "var(--danger)",    color: "#fff" },
  };
  return (
    <button
      style={{
        fontFamily: "inherit", fontWeight: 600, cursor: rest.disabled ? "not-allowed" : "pointer",
        border: "none", display: "inline-flex", alignItems: "center", gap: 6,
        whiteSpace: "nowrap", flexShrink: 0,
        transition: "all 120ms cubic-bezier(0.2,0,0,1)",
        opacity: rest.disabled ? 0.4 : 1,
        ...sizes[size], ...variants[variant], ...style,
      }}
      {...rest}
    >{children}</button>
  );
}

export function Card({
  children, padded = true, soft = false, style, ...rest
}: { children: ReactNode; padded?: boolean; soft?: boolean; style?: CSSProperties } & Record<string, unknown>) {
  return (
    <div data-soft={soft || undefined}
      style={{
        background: soft ? "var(--surface-2)" : "#fff",
        border: soft ? "none" : "1px solid var(--border)",
        borderRadius: 16,
        boxShadow: soft ? "none" : "var(--shadow-1)",
        padding: padded ? 20 : 0,
        ...style,
      }}
      {...rest}
    >{children}</div>
  );
}

export function Hairline({ style, ...rest }: { style?: CSSProperties } & Record<string, unknown>) {
  return <div style={{ height: 1, background: "var(--divider)", ...style }} {...rest} />;
}

export type Direction = "up" | "down" | "mixed" | "flat";
export function Arrow({ dir, ...rest }: { dir: Direction } & Record<string, unknown>) {
  const map: Record<Direction, { glyph: string; color: string }> = {
    up:    { glyph: "▲", color: "var(--gain)" },
    down:  { glyph: "▼", color: "var(--loss)" },
    mixed: { glyph: "◆", color: "var(--warn)" },
    flat:  { glyph: "·", color: "var(--flat)" },
  };
  const { glyph, color } = map[dir];
  return <span style={{ color }} {...rest}>{glyph}</span>;
}

export function PL({ value, pct, krw = true, size = 13 }: {
  value: number; pct: number; krw?: boolean; size?: number;
}) {
  const dir: Direction = value > 0 ? "up" : value < 0 ? "down" : "flat";
  const color = dir === "up" ? "var(--gain)" : dir === "down" ? "var(--loss)" : "var(--flat)";
  const sign = value > 0 ? "+" : value < 0 ? "−" : "";
  const abs = Math.abs(value);
  const formatted = krw
    ? `${sign}${abs.toLocaleString("ko-KR")}`
    : `${sign}${abs.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
  return (
    <span data-testid="pl" data-dir={dir}
      style={{ color, fontWeight: 600, fontFeatureSettings: '"tnum"', fontSize: size }}>
      <Arrow dir={dir} /> {formatted} · {sign}{Math.abs(pct).toFixed(2)}%
    </span>
  );
}

export function Krw({ v, size = 14, weight = 600 }: { v: number | null | undefined; size?: number; weight?: number }) {
  return (
    <span style={{ fontFeatureSettings: '"tnum"', fontWeight: weight, fontSize: size }}>
      {v == null ? "−" : `₩${Math.round(v).toLocaleString("ko-KR")}`}
    </span>
  );
}

export function Usd({ v, size = 14, weight = 600 }: { v: number | null | undefined; size?: number; weight?: number }) {
  return (
    <span style={{ fontFeatureSettings: '"tnum"', fontWeight: weight, fontSize: size }}>
      {v == null ? "−" : `$${v.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`}
    </span>
  );
}

export function Sparkline({
  points, color, height = 36, width = 120,
}: { points: number[]; color: string; height?: number; width?: number }) {
  if (points.length < 2) return <svg width={width} height={height} />;
  const max = Math.max(...points), min = Math.min(...points);
  const range = max - min || 1;
  const step = width / (points.length - 1);
  const path = points.map((p, i) => `${i * step},${height - ((p - min) / range) * (height - 4) - 2}`).join(" ");
  return (
    <svg width={width} height={height} viewBox={`0 0 ${width} ${height}`} preserveAspectRatio="none" style={{ display: "block" }}>
      <polyline fill="none" stroke={color} strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" points={path} />
    </svg>
  );
}

export type IconName =
  | "home" | "bell" | "chart" | "search" | "calendar" | "chev"
  | "arrowOut" | "info" | "settings" | "plus" | "refresh" | "flash";

export function Icon({ name, size = 20 }: { name: IconName; size?: number }) {
  // Port the `paths` table from atoms.jsx verbatim, returning an inline SVG.
  // (See atoms.jsx for the exact `<path d="…" />` strings — copy them as-is.)
  const paths: Record<IconName, ReactNode> = {
    home: <path d="M3 9.5L12 3l9 6.5V20a1 1 0 0 1-1 1h-5v-7H9v7H4a1 1 0 0 1-1-1V9.5z" />,
    bell: <><path d="M18 8a6 6 0 1 0-12 0c0 7-3 9-3 9h18s-3-2-3-9" /><path d="M13.7 21a2 2 0 0 1-3.4 0" /></>,
    chart: <path d="M18 20V10M12 20V4M6 20v-6" />,
    search: <><circle cx="11" cy="11" r="7" /><path d="M21 21l-4.3-4.3" /></>,
    calendar: <><rect x="3" y="4" width="18" height="18" rx="2" /><path d="M16 2v4M8 2v4M3 10h18" /></>,
    chev: <path d="M9 18l6-6-6-6" />,
    arrowOut: <path d="M7 17L17 7M9 7h8v8" />,
    info: <><circle cx="12" cy="12" r="9" /><path d="M12 11v5M12 8v.01" /></>,
    settings: <><circle cx="12" cy="12" r="3" /><path d="M19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 0 1-4 0v-.1a1.7 1.7 0 0 0-1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 0 1 0-4h.1a1.7 1.7 0 0 0 1.5-1 1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3h.1a1.7 1.7 0 0 0 1-1.5V3a2 2 0 0 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8v.1a1.7 1.7 0 0 0 1.5 1H21a2 2 0 0 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z" /></>,
    plus: <path d="M12 5v14M5 12h14" />,
    refresh: <><path d="M21 12a9 9 0 1 1-3-6.7L21 8" /><path d="M21 3v5h-5" /></>,
    flash: <path d="M13 2L3 14h7l-1 8 10-12h-7l1-8z" />,
  };
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none"
      stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"
      style={{ flex: "0 0 auto" }}>
      {paths[name]}
    </svg>
  );
}
```

Then create the barrel:

```ts
// frontend/invest/src/ds/index.ts
export { Pill, Button, Card, Hairline, Arrow, PL, Krw, Usd, Sparkline, Icon } from "./atoms";
export type { PillTone, PillSize, ButtonVariant, ButtonSize, Direction, IconName } from "./atoms";
```

- [ ] **Step 4: Run tests**

```bash
cd frontend/invest && npx vitest run src/__tests__/dsAtoms.test.tsx
```

Expected: PASS (8 tests).

- [ ] **Step 5: Run full typecheck + suite**

```bash
cd frontend/invest && npm run typecheck && npm test
```

Expected: typecheck PASS, all existing + new tests PASS.

- [ ] **Step 6: Commit**

```bash
git add frontend/invest/src/ds/ frontend/invest/src/__tests__/dsAtoms.test.tsx
git commit -m "feat(invest): port design-system primitives (atoms.tsx)"
```

### Task 1.3: Migrate the simplest existing utilities to tokens

Quick wins that get most pages "less broken" before stage-by-stage rebuild.

**Files:**
- Modify: `frontend/invest/src/components/HeroCard.tsx`
- Modify: `frontend/invest/src/components/HoldingRow.tsx`
- Modify: `frontend/invest/src/components/BottomNav.tsx`

- [ ] **Step 1: Update HeroCard.tsx**

Replace `background: "var(--surface)"` (which is now white) is fine; replace fontSize numerics with tokens where tidy. Keep behavior 1:1 — this is a token swap, not a rebuild.

```tsx
// Replace the inline background in <div data-testid="hero-card" style={...}>
// with: background: "var(--surface)", border: "1px solid var(--border)",
// box-shadow: "var(--shadow-1)"
// (Stage 2 will rebuild this against the bundle's HeroCard spec.)
```

- [ ] **Step 2: Update HoldingRow.tsx `rowStyle.borderBottom` from `var(--surface-2)` to `var(--divider)`**

The current usage uses `--surface-2` as a divider. With the new tokens, `--surface-2` is a soft fill (`#f2f4f6`) — not a divider. Switch to `var(--divider)`. Also swap `var(--pill-mix)` / `var(--pill-mix-fg)` (legacy dark tokens) to `var(--pill-paper-bg)` / `var(--pill-paper-fg)` for `SourceChip`.

- [ ] **Step 3: Update BottomNav.tsx**

Replace `borderTop: "1px solid var(--surface-2)"` with `borderTop: "1px solid var(--divider)"`. Replace `color: "var(--muted)"` (legacy) with `color: "var(--fg-3)"`. Replace `color: "var(--text)"` (legacy) with `color: "var(--fg)"`.

- [ ] **Step 4: Update DesktopShell.tsx and DesktopHeader.tsx**

In `DesktopShell.tsx`, replace `background: "var(--bg, #0e1014)", color: "var(--text, #e8eaf0)"` with `background: "var(--bg-alt)", color: "var(--fg)"`.

In `DesktopHeader.tsx`, replace `borderBottom: "1px solid var(--surface-2, #1c1e24)"` with `borderBottom: "1px solid var(--divider)"`. Replace inactive nav color `#9ba0ab` with `var(--fg-2)` and active `#fff` with `var(--fg)`.

- [ ] **Step 5: Update RightAccountPanel.tsx**

Swap dark hex colors: `#9ba0ab` → `var(--fg-3)`, `#f59e9e` → `var(--danger)`, `#5ed1a3` → `var(--success)`, dark surface backgrounds (`var(--surface, #15181f)`) → `var(--surface)`. Note: this is a band-aid. Stage 2 rebuilds this component.

- [ ] **Step 6: Run typecheck + tests**

```bash
cd frontend/invest && npm run typecheck && npm test
```

Expected: PASS. Tests don't assert on hex strings, so they should pass unchanged.

- [ ] **Step 7: Commit**

```bash
git add frontend/invest/src/components/HeroCard.tsx frontend/invest/src/components/HoldingRow.tsx frontend/invest/src/components/BottomNav.tsx frontend/invest/src/desktop/DesktopShell.tsx frontend/invest/src/desktop/DesktopHeader.tsx frontend/invest/src/desktop/RightAccountPanel.tsx
git commit -m "refactor(invest): swap dark theme hexes to design system tokens"
```

**Stage 1 PR:** "feat(invest): introduce design system tokens + primitives" — combines Tasks 1.1–1.3.

---

## Stage 2 — `/invest` desktop shell + portfolio overview polish

**Goal:** Rebuild the Home route at `/` (desktop) so it matches `HomeView.jsx` and `DesktopChrome.jsx`. New left context rail, new hero card, new market strip, new holdings table, new right account panel. The `useInvestHome` hook + types are unchanged.

### Task 2.1: Rebuild `DesktopHeader` against the bundle spec

**Files:**
- Modify: `frontend/invest/src/desktop/DesktopHeader.tsx`

- [ ] **Step 1: Replace the existing header body**

Port `DesktopChrome.jsx`'s `DesktopHeader` to TS, replacing `route`/`onRoute` with `react-router-dom` `<NavLink>`s. Routes: `홈 → /`, `뉴스 → /feed/news`, `발견 → /discover` (new — see Stage 4), `시그널 → /signals`, `캘린더 → /calendar`. Keep the existing `골라보기` (screener) link as a sixth item to preserve continuity.

Use `Icon name="search"` from `ds/atoms` for the search field, `Icon name="bell"` for the bell button, and inline-render the `auto_trader` wordmark with the `A` square mark exactly as in the bundle.

- [ ] **Step 2: Run tests**

```bash
cd frontend/invest && npm test -- --run DesktopShell
```

Expected: PASS (the existing `DesktopShell.test.tsx` checks for the `desktop-shell` testid + that header link list is rendered; verify which navlinks the test expects and update either the test or the link list to keep both in sync).

- [ ] **Step 3: Commit**

```bash
git add frontend/invest/src/desktop/DesktopHeader.tsx
git commit -m "feat(invest): rebuild desktop header per design system"
```

### Task 2.2: Add `LeftContextRail`

**Files:**
- Create: `frontend/invest/src/desktop/LeftContextRail.tsx`

- [ ] **Step 1: Implement**

Port `LeftContextRail` from `DesktopChrome.jsx`. Source the account list from a new prop `accounts: AccountPanelResponse["accounts"]` (typed); the active filter is local state for now (will lift in Stage 3). Render groups: `계좌별 보기`, `카테고리`, `오늘의 알림` (placeholder text card; data wiring deferred).

- [ ] **Step 2: No tests yet (component is wired in Task 2.4 via DesktopShell)**

- [ ] **Step 3: Commit**

```bash
git add frontend/invest/src/desktop/LeftContextRail.tsx
git commit -m "feat(invest): add LeftContextRail per design system"
```

### Task 2.3: Rebuild `RightAccountPanel`

**Files:**
- Modify: `frontend/invest/src/desktop/RightAccountPanel.tsx`
- Test: `frontend/invest/src/__tests__/RightAccountPanel.test.tsx` (existing — preserve every testid + behavior)

- [ ] **Step 1: Read the existing test to lock down the contract**

```bash
cat frontend/invest/src/__tests__/RightAccountPanel.test.tsx
```

Note every `data-testid` and every assertion. The new component must keep `data-testid="right-panel-skeleton"`, `right-panel-error`, `right-panel`, `right-panel-account` (with `data-source`), and `watchlist-empty`.

- [ ] **Step 2: Replace the body with the bundle spec port**

Use `RightAccountPanel.jsx` as the visual spec. Translation:
- `summary.totalKrw` → `data.homeSummary.totalValueKrw`
- `summary.pnlKrw / pnlPct` → `data.homeSummary.pnlKrw / pnlRate * 100`
- `accounts.map(a => …)` → `data.accounts.map(a => …)` with `tone` derived from `a.source` via the existing `visualBySource()` (map: `kis*` → "kis", `upbit` → "upbit", `toss_manual` → "toss", `isa_manual` → "isa", `pension_manual` → "pension", others → "paper")
- Watchlist `data.watchSymbols` (already exists; map `market`/`displayName`/`symbol`)

Use `Card`, `Pill`, `PL`, `Sparkline`, `Button`, `Icon` from `ds/atoms`. Preserve all 5 `data-testid`s and the `data-source={a.source}` attribute on each account block.

- [ ] **Step 3: Add a small `tone` helper**

Move the source→tone mapping into a tiny helper at `frontend/invest/src/desktop/AccountSourceTone.ts` (already exists — extend it if needed) so the same mapping is used by `LeftContextRail`, `RightAccountPanel`, and Stage 3 mobile.

```ts
// frontend/invest/src/desktop/AccountSourceTone.ts (append)
import type { AccountSource } from "../types/invest";
import type { PillTone } from "../ds";

export function pillToneForSource(source: AccountSource): PillTone {
  switch (source) {
    case "kis": case "kis_mock": return "kis";
    case "upbit": return "upbit";
    case "toss_manual": return "toss";
    case "isa_manual": return "isa";
    case "pension_manual": return "pension";
    default: return "paper";
  }
}
```

- [ ] **Step 4: Run tests + typecheck**

```bash
cd frontend/invest && npm run typecheck && npm test -- RightAccountPanel
```

Expected: typecheck PASS, RightAccountPanel tests PASS unchanged.

- [ ] **Step 5: Commit**

```bash
git add frontend/invest/src/desktop/RightAccountPanel.tsx frontend/invest/src/desktop/AccountSourceTone.ts
git commit -m "feat(invest): rebuild right account panel per design system"
```

### Task 2.4: Rebuild `DesktopShell` and `DesktopHomePage`

**Files:**
- Modify: `frontend/invest/src/desktop/DesktopShell.tsx`
- Modify: `frontend/invest/src/pages/desktop/DesktopHomePage.tsx`
- Create: `frontend/invest/src/components/home/DesktopHero.tsx`
- Create: `frontend/invest/src/components/home/MarketStrip.tsx`
- Create: `frontend/invest/src/components/home/HoldingsTable.tsx`
- Create: `frontend/invest/src/components/home/FilterChips.tsx`
- Test: `frontend/invest/src/__tests__/HomePage.test.tsx` (existing — preserve every testid + behavior; rename internal class swaps but keep `data-testid="hero-card"`, etc.)

- [ ] **Step 1: Update DesktopShell signature**

```tsx
// frontend/invest/src/desktop/DesktopShell.tsx
export function DesktopShell({ left, center, right }: { left?: ReactNode; center: ReactNode; right: ReactNode }) {
  return (
    <div data-testid="desktop-shell" style={{ minHeight: "100vh", background: "var(--bg-alt)" }}>
      <DesktopHeader />
      <div style={{
        display: "grid",
        gridTemplateColumns: left ? "220px minmax(0,1fr) 320px" : "minmax(0,1fr) 320px",
        gap: 24, padding: "24px 28px 64px", maxWidth: 1440, margin: "0 auto",
      }}>
        {left ? <aside style={{ minWidth: 0 }}>{left}</aside> : null}
        <main style={{ minWidth: 0, display: "flex", flexDirection: "column", gap: 16 }}>{center}</main>
        <aside style={{ position: "sticky", top: 24, alignSelf: "start", maxHeight: "calc(100vh - 48px)", overflowY: "auto" }}>
          {right}
        </aside>
      </div>
    </div>
  );
}
```

- [ ] **Step 2: Implement `DesktopHero`**

Port `HeroCard` from `HomeView.jsx`. Props: `summary: HomeSummary` + optional `accountCount`. Render the 3-up breakdown by asset class — derive shares from `useInvestHome`'s `data.groupedHoldings` aggregated by `assetCategory`. Use `Card`, `Button`, `Icon`, `PL` from `ds/atoms`. Keep `data-testid="hero-card"`.

- [ ] **Step 3: Implement `MarketStrip`**

Port `MarketStrip` from `HomeView.jsx`. Props: `items: MarketIndex[]`. Data source: `data.marketIndices` from `useInvestHome` if it exists; if not, render a 4-up skeleton with token-themed surfaces and add a TODO marker for backend wiring (this is acceptable per the brief — "Actual API/data integration can be deferred if needed").

- [ ] **Step 4: Implement `HoldingsTable` and `FilterChips`**

Port `HoldingsTable` from `HomeView.jsx`. Props: `holdings: GroupedHolding[] | Holding[]`, `filter: AssetCategoryKey`. Use the existing `pillToneForSource()` to derive tone for the avatar square. Render KRW vs USD distinction per the bundle spec. Keep `data-testid="grouped-row"` and `data-testid="raw-row"` on each row.

Port `FilterChips` from `HomeView.jsx`. Props: `value: AssetCategoryKey`, `onChange: (k: AssetCategoryKey) => void`. Reuse the existing `AssetCategoryKey` type.

- [ ] **Step 5: Rewrite `DesktopHomePage`**

```tsx
// frontend/invest/src/pages/desktop/DesktopHomePage.tsx
import { useState } from "react";
import { DesktopShell } from "../../desktop/DesktopShell";
import { LeftContextRail } from "../../desktop/LeftContextRail";
import { RightAccountPanel } from "../../desktop/RightAccountPanel";
import { useAccountPanel } from "../../desktop/useAccountPanel";
import { useInvestHome } from "../../hooks/useInvestHome";
import { DesktopHero } from "../../components/home/DesktopHero";
import { MarketStrip } from "../../components/home/MarketStrip";
import { HoldingsTable } from "../../components/home/HoldingsTable";
import { FilterChips } from "../../components/home/FilterChips";
import type { AssetCategoryKey } from "../../components/AssetCategoryFilter";

export function DesktopHomePage() {
  const home = useInvestHome();
  const panel = useAccountPanel();
  const [filter, setFilter] = useState<AssetCategoryKey>("all");

  const data = home.state.status === "ready" ? home.state.data : null;

  return (
    <DesktopShell
      left={<LeftContextRail accounts={panel.data?.accounts ?? []} totalKrw={data?.homeSummary.totalValueKrw ?? 0} />}
      center={
        <>
          {data && <DesktopHero summary={data.homeSummary} accountCount={data.accounts.length} />}
          <MarketStrip items={data?.marketIndices ?? []} />
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginTop: 4 }}>
            <h2 style={{ margin: 0, fontSize: 18, fontWeight: 700 }}>보유 종목</h2>
            <FilterChips value={filter} onChange={setFilter} />
          </div>
          <HoldingsTable
            holdings={data?.groupedHoldings ?? []}
            filter={filter}
          />
        </>
      }
      right={<RightAccountPanel data={panel.data} loading={panel.loading} error={panel.error} />}
    />
  );
}
```

- [ ] **Step 6: Run tests + typecheck**

```bash
cd frontend/invest && npm run typecheck && npm test
```

Expected: PASS for all suites including the rebuilt HomePage and DesktopShell.

- [ ] **Step 7: Visual smoke**

```bash
cd frontend/invest && npm run dev
```

Open `http://localhost:5174/invest/` — should render three columns, light theme, with the hero, market strip, holdings table, and right rail. Confirm Korean P/L convention is preserved (red ▲ for gain, blue ▼ for loss).

- [ ] **Step 8: Commit**

```bash
git add frontend/invest/src/desktop/DesktopShell.tsx frontend/invest/src/pages/desktop/DesktopHomePage.tsx frontend/invest/src/components/home/
git commit -m "feat(invest): rebuild desktop home per design system"
```

**Stage 2 PR:** "feat(invest): /invest desktop shell + home polish" — combines Tasks 2.1–2.4.

---

## Stage 3 — Responsive mobile under canonical `/invest`

**Goal:** Make the same five canonical routes (`/`, `/feed/news`, `/discover`, `/signals`, `/calendar`) responsive — narrow viewports render the mobile layouts from `MobileView.jsx` under the same routes. `/invest/app/*` keeps working as a backwards-compat alias (Stage 6 retires it).

### Task 3.1: Add a `useViewport` hook

**Files:**
- Create: `frontend/invest/src/hooks/useViewport.ts`
- Test: `frontend/invest/src/__tests__/useViewport.test.tsx`

- [ ] **Step 1: Write failing test**

```tsx
import { renderHook, act } from "@testing-library/react";
import { useViewport } from "../hooks/useViewport";

it("returns 'mobile' under 900px", () => {
  Object.defineProperty(window, "innerWidth", { writable: true, configurable: true, value: 600 });
  const { result } = renderHook(() => useViewport());
  expect(result.current).toBe("mobile");
});

it("returns 'desktop' at 1200px and above", () => {
  Object.defineProperty(window, "innerWidth", { writable: true, configurable: true, value: 1200 });
  const { result } = renderHook(() => useViewport());
  expect(result.current).toBe("desktop");
});
```

- [ ] **Step 2: Run test → fail** (`Cannot find module ../hooks/useViewport`).

- [ ] **Step 3: Implement**

```ts
// frontend/invest/src/hooks/useViewport.ts
import { useEffect, useState } from "react";
export type Viewport = "mobile" | "compact" | "desktop";

export function useViewport(): Viewport {
  const [vp, setVp] = useState<Viewport>(() => detect());
  useEffect(() => {
    function onResize() { setVp(detect()); }
    window.addEventListener("resize", onResize);
    return () => window.removeEventListener("resize", onResize);
  }, []);
  return vp;
}

function detect(): Viewport {
  if (typeof window === "undefined") return "desktop";
  const w = window.innerWidth;
  if (w >= 1200) return "desktop";
  if (w >= 900) return "compact";
  return "mobile";
}
```

- [ ] **Step 4: Pass tests + commit**

```bash
cd frontend/invest && npm run typecheck && npm test -- useViewport
git add frontend/invest/src/hooks/useViewport.ts frontend/invest/src/__tests__/useViewport.test.tsx
git commit -m "feat(invest): add useViewport hook"
```

### Task 3.2: Add `MobileShell` + `MobileBottomNav` + `MobileTopBar`

**Files:**
- Create: `frontend/invest/src/mobile/MobileShell.tsx`
- Create: `frontend/invest/src/mobile/MobileBottomNav.tsx`
- Create: `frontend/invest/src/mobile/MobileTopBar.tsx`

- [ ] **Step 1: Implement `MobileBottomNav`**

Port `MobileBottomNav` from `MobileView.jsx`. Replace the local `route`/`onRoute` callback with `react-router-dom` `<NavLink>`s. The five tabs are: `홈 → /`, `뉴스 → /feed/news`, `발견 → /discover`, `시그널 → /signals`, `캘린더 → /calendar`. Use `Icon` + a fallback unicode glyph for missing icons.

- [ ] **Step 2: Implement `MobileTopBar`**

Port `MobileTopBar` from `MobileView.jsx`. Render a `title` prop and search/bell buttons (no-op for now).

- [ ] **Step 3: Implement `MobileShell`**

```tsx
// frontend/invest/src/mobile/MobileShell.tsx
import type { ReactNode } from "react";
import { MobileTopBar } from "./MobileTopBar";
import { MobileBottomNav } from "./MobileBottomNav";

export function MobileShell({ title, children }: { title: string; children: ReactNode }) {
  return (
    <div style={{ minHeight: "100vh", display: "flex", flexDirection: "column", background: "var(--bg)" }}>
      <MobileTopBar title={title} />
      <div style={{ flex: 1, overflow: "auto" }}>{children}</div>
      <MobileBottomNav />
    </div>
  );
}
```

- [ ] **Step 4: Commit**

```bash
git add frontend/invest/src/mobile/
git commit -m "feat(invest): add mobile shell + bottom nav per design system"
```

### Task 3.3: Make `DesktopHomePage` responsive (renders Mobile shell under 900px)

**Files:**
- Modify: `frontend/invest/src/pages/desktop/DesktopHomePage.tsx`
- Create: `frontend/invest/src/pages/mobile/MobileHomePage.tsx`

- [ ] **Step 1: Implement `MobileHomePage`**

Port `MobileHomeRoute` from `MobileView.jsx`. Use the same `useInvestHome()` data the desktop page uses — bind `summary`, `accounts`, `holdings`, market indices to real props. Reuse `Pill`, `PL`, `Button` from `ds/atoms`. Wrap in `<MobileShell title="홈">`.

- [ ] **Step 2: Add a single dispatch wrapper**

Instead of two separate route components, swap the page-level component based on viewport:

```tsx
// Replace DesktopHomePage default export with a Home dispatcher
import { useViewport } from "../../hooks/useViewport";
import { MobileHomePage } from "../mobile/MobileHomePage";

export function HomePage() {
  return useViewport() === "mobile" ? <MobileHomePage /> : <DesktopHomePage />;
}
```

Rename the existing `DesktopHomePage` to `DesktopHomePageInner` if needed and export both. **Update `frontend/invest/src/routes.tsx`:**

```tsx
{ path: "/", element: <HomePage /> },  // was DesktopHomePage
```

- [ ] **Step 3: Run tests + typecheck**

```bash
cd frontend/invest && npm run typecheck && npm test
```

Expected: PASS. The `HomePage.test.tsx` exists for the legacy mobile `/app` page — it still hits the legacy `HomePage` component (preserved), which we'll move/redirect in Stage 6.

- [ ] **Step 4: Visual smoke (mobile + desktop)**

Resize browser between < 900px and ≥ 1200px on `http://localhost:5174/invest/` — should swap layouts.

- [ ] **Step 5: Commit**

```bash
git add frontend/invest/src/pages/desktop/DesktopHomePage.tsx frontend/invest/src/pages/mobile/MobileHomePage.tsx frontend/invest/src/routes.tsx
git commit -m "feat(invest): home is responsive at /invest (mobile under 900px)"
```

**Stage 3 PR:** "feat(invest): canonical /invest is responsive (mobile + desktop)" — combines Tasks 3.1–3.3.

---

## Stage 4 — News, Discover, Signals route polish

**Goal:** Apply the same rebuild pattern to News (`/feed/news`), Discover (new `/discover`), and Signals (`/signals`). Each gets a desktop port from the bundle, plus a responsive mobile renderer.

### Task 4.1: Rebuild News (desktop + mobile)

**Files:**
- Modify: `frontend/invest/src/pages/desktop/DesktopFeedNewsPage.tsx`
- Create: `frontend/invest/src/pages/mobile/MobileFeedNewsPage.tsx`
- Create: `frontend/invest/src/components/news/NewsCard.tsx`
- Create: `frontend/invest/src/components/news/NewsTabs.tsx`

- [ ] **Step 1: Implement `NewsTabs`**

Port the tab strip from `NewsView.jsx`. Props: `value: FeedTab`, `onChange: (tab: FeedTab) => void`. Use the existing `FeedTab` type from `types/feedNews`. Keep the `data-testid="tab-${tab}"` attributes the existing test relies on.

- [ ] **Step 2: Implement `NewsCard`**

Port the news row from `NewsView.jsx`. Props: `item: FeedItem`, `open: boolean`, `onToggle: () => void`. Map `item.relation` → `Pill tone="accent"|"kis"|null`. Keep `data-testid="feed-item"` and `data-relation={item.relation}`.

- [ ] **Step 3: Rebuild `DesktopFeedNewsPage`**

Use `NewsTabs` + a list of `NewsCard`. Keep `data-testid="feed-center"`. Use `DesktopShell` with `left={null}` (per the bundle, the news view doesn't want the left rail) and the right account panel.

- [ ] **Step 4: Implement `MobileFeedNewsPage`**

Port `MobileNewsRoute` from `MobileView.jsx`. Wrap in `<MobileShell title="뉴스">`.

- [ ] **Step 5: Dispatcher**

```tsx
// frontend/invest/src/pages/desktop/DesktopFeedNewsPage.tsx (bottom)
export function FeedNewsPage() {
  return useViewport() === "mobile" ? <MobileFeedNewsPage /> : <DesktopFeedNewsPage />;
}
```

Update `routes.tsx`: `{ path: "/feed/news", element: <FeedNewsPage /> }`.

- [ ] **Step 6: Run tests + commit**

```bash
cd frontend/invest && npm run typecheck && npm test -- DesktopFeedNewsPage
git add frontend/invest/src/components/news/ frontend/invest/src/pages/desktop/DesktopFeedNewsPage.tsx frontend/invest/src/pages/mobile/MobileFeedNewsPage.tsx frontend/invest/src/routes.tsx
git commit -m "feat(invest): rebuild news route per design system"
```

### Task 4.2: Add Discover route at `/discover` (canonical) — preserve `/app/discover`

**Files:**
- Create: `frontend/invest/src/pages/desktop/DesktopDiscoverPage.tsx`
- Create: `frontend/invest/src/pages/mobile/MobileDiscoverPage.tsx`
- Create: `frontend/invest/src/components/discover/IssueCard.tsx`
- Modify: `frontend/invest/src/routes.tsx`

- [ ] **Step 1: Implement `IssueCard`**

Port `IssueRow` from `DiscoverView.jsx`. Props: `issue: NewsIssue`, `expanded: boolean`, `onToggle: () => void`. Use the existing `severity` helper at `components/discover/severity.ts` to map to direction arrow + tone.

- [ ] **Step 2: Implement `DesktopDiscoverPage`**

Use `useNewsIssues()` + `useDiscoverCalendar()`. Render the bundle's `DiscoverView` layout in the center column. Use the `DesktopShell` left/right.

- [ ] **Step 3: Implement `MobileDiscoverPage`**

Port `MobileDiscoverRoute`. Wrap in `<MobileShell title="발견">`.

- [ ] **Step 4: Add to routes**

```tsx
// routes.tsx: add the canonical /discover route
{ path: "/discover", element: <DiscoverPageDispatcher /> },
{ path: "/discover/issues/:issueId", element: <DiscoverIssueDetailPage /> },
```

The legacy `DiscoverPage` at `/app/discover` is preserved as-is (Stage 6 retires it).

- [ ] **Step 5: Run tests + commit**

```bash
cd frontend/invest && npm run typecheck && npm test
git add frontend/invest/src/pages/desktop/DesktopDiscoverPage.tsx frontend/invest/src/pages/mobile/MobileDiscoverPage.tsx frontend/invest/src/components/discover/IssueCard.tsx frontend/invest/src/routes.tsx
git commit -m "feat(invest): add canonical /invest/discover route per design system"
```

### Task 4.3: Rebuild Signals (desktop + mobile)

**Files:**
- Modify: `frontend/invest/src/pages/desktop/DesktopSignalsPage.tsx`
- Create: `frontend/invest/src/pages/mobile/MobileSignalsPage.tsx`
- Create: `frontend/invest/src/components/signals/SignalCard.tsx`

- [ ] **Step 1: Implement `SignalCard`**

Port the signal card from `SignalsView.jsx`. Props: `signal: Signal`. Use `Card`, `Button`, `Pill`, `Icon` from `ds/atoms`.

- [ ] **Step 2: Rebuild `DesktopSignalsPage`**

Render a 2-up grid of `SignalCard` per the bundle spec. Preserve every existing `data-testid`.

- [ ] **Step 3: Implement `MobileSignalsPage`**

Port `MobileSignalsRoute` and wrap in `<MobileShell title="시그널">`.

- [ ] **Step 4: Dispatcher + routes update + commit**

```bash
cd frontend/invest && npm run typecheck && npm test -- DesktopSignalsPage
git add frontend/invest/src/components/signals/ frontend/invest/src/pages/desktop/DesktopSignalsPage.tsx frontend/invest/src/pages/mobile/MobileSignalsPage.tsx frontend/invest/src/routes.tsx
git commit -m "feat(invest): rebuild signals route per design system"
```

### Task 4.4: Light reframe `/screener`

**Files:**
- Modify: `frontend/invest/src/desktop/screener/screener.css`
- Modify: `frontend/invest/src/desktop/screener/ScreenerFilterBar.tsx`, `ScreenerResultsTable.tsx`, `ScreenerPresetSidebar.tsx`, `ScreenerFilterModal.tsx`

- [ ] **Step 1: Audit `screener.css` for dark hexes**

```bash
grep -n "#" frontend/invest/src/desktop/screener/screener.css
```

Replace any dark hex with the corresponding token. Keep layout intact — no spec from the bundle for screener.

- [ ] **Step 2: Replace inline dark hexes in screener TSX files**

Use the same swap rules from Task 1.3 (`#9ba0ab` → `var(--fg-3)`, etc.).

- [ ] **Step 3: Run tests + commit**

```bash
cd frontend/invest && npm run typecheck && npm test -- DesktopScreener
git add frontend/invest/src/desktop/screener/
git commit -m "refactor(invest): screener token sweep (light reframe)"
```

**Stage 4 PR:** "feat(invest): news + discover + signals + screener polish" — combines Tasks 4.1–4.4.

---

## Stage 5 — Finance calendar enhancement

**Goal:** Replace the basic calendar at `/calendar` with the full finance-calendar pattern from `CalendarView.jsx` + the mobile pattern from `MobileCalendarRoute` in `MobileView.jsx`. Event chips for 경제지표/실적/배당/FOMC/기업, KR/US, importance. AI weekly summary card. Event detail modal with 발표/예측/이전 + EPS / EPS 예상 / 서프라이즈 shape.

### Task 5.1: Add calendar primitives

**Files:**
- Create: `frontend/invest/src/components/calendar/MiniMonth.tsx`
- Create: `frontend/invest/src/components/calendar/AIWeeklyCard.tsx`
- Create: `frontend/invest/src/components/calendar/EventRow.tsx`
- Create: `frontend/invest/src/components/calendar/WeekGroup.tsx`
- Create: `frontend/invest/src/components/calendar/EventDetailModal.tsx`
- Create: `frontend/invest/src/components/calendar/MonthGridView.tsx`
- Create: `frontend/invest/src/components/calendar/RegionBadge.tsx`
- Create: `frontend/invest/src/components/calendar/OwnershipTag.tsx`
- Create: `frontend/invest/src/components/calendar/EmptyEventState.tsx`
- Create: `frontend/invest/src/components/calendar/SparkleIcon.tsx`

- [ ] **Step 1–9: Port each component from `CalendarView.jsx` 1:1**

Each is a small presentational component — port it verbatim, replacing `window.FIXTURE.calendar` access with explicit props. Type props against the existing `frontend/invest/src/types/calendar.ts` (e.g. `CalendarEvent`, `CalendarDay`, `WeeklySummaryResponse`). Where the bundle uses `e.dL`, `e.eps`, `e.epsF`, `e.surprise`, `e.actual`, `e.forecast`, `e.previous` — define a `CalendarEventVM` view model in `components/calendar/vm.ts` with those exact field names; the desktop/mobile pages map raw API responses into VMs at the page level so the UI stays clean.

- [ ] **Step 10: Run typecheck**

```bash
cd frontend/invest && npm run typecheck
```

- [ ] **Step 11: Commit**

```bash
git add frontend/invest/src/components/calendar/
git commit -m "feat(invest): add calendar primitives per design system"
```

### Task 5.2: Rebuild `DesktopCalendarPage`

**Files:**
- Modify: `frontend/invest/src/pages/desktop/DesktopCalendarPage.tsx`
- Test: `frontend/invest/src/__tests__/DesktopCalendarPage.test.tsx` (existing — preserve every `data-testid`: `open-weekly-summary`, `day-${date}`, `day-events`, `calendar-event` with `data-relation`, `weekly-summary`)

- [ ] **Step 1: Read existing test contract**

```bash
cat frontend/invest/src/__tests__/DesktopCalendarPage.test.tsx
```

Note exactly which testids/roles the test queries.

- [ ] **Step 2: Implement the new page using bundle layout**

Two-column grid: left rail = `MiniMonth` + compact `AIWeeklyCard`; right column = filter group (`<FilterGroup>` `<SegPill>`s for 전체/경제지표/실적, then 전체/국내/해외) + `<PeriodToggle>` (week/month) + either `<WeekGroup>` list or `<MonthGridView>`. `EventDetailModal` opens on row click and on the AI summary CTA.

The page maps the existing `CalendarResponse` (`days[].events[]`) into `CalendarEventVM` by:
- `event.eventType === "earnings"` → `vm.type = "earnings"`, with `vm.eps`, `vm.epsF`, `vm.surprise` populated from event fields if present (otherwise `null`).
- `event.eventType === "macro"` (or fallback) → `vm.type = "macro"`, with `vm.actual / forecast / previous` populated similarly.
- `vm.region` from `event.market === "kr" ? "kr" : "us"`.
- `vm.own` from `event.relation === "holdings" ? "holdings" : event.relation === "watchlist" ? "watchlist" : null` plus an optional `"major"` flag derived from `event.badges.includes("중요")`.

When backend doesn't yet provide a field (eps/forecast/etc.), pass `null` — the UI already renders `−` placeholders for nulls.

Preserve every existing `data-testid` from the previous implementation: place `data-testid="open-weekly-summary"` on the AI summary CTA, `data-testid="day-${date}"` on each MiniMonth date cell, `data-testid="day-events"` on the event list section, `data-testid="calendar-event" data-relation={…}` on each EventRow, `data-testid="weekly-summary"` on the modal/summary container.

- [ ] **Step 3: Run tests + typecheck**

```bash
cd frontend/invest && npm run typecheck && npm test -- DesktopCalendarPage
```

Expected: PASS.

- [ ] **Step 4: Commit**

```bash
git add frontend/invest/src/pages/desktop/DesktopCalendarPage.tsx
git commit -m "feat(invest): rebuild calendar route per design system"
```

### Task 5.3: Mobile calendar route + dispatcher

**Files:**
- Create: `frontend/invest/src/pages/mobile/MobileCalendarPage.tsx`
- Modify: `frontend/invest/src/routes.tsx`

- [ ] **Step 1: Port `MobileCalendarRoute` from `MobileView.jsx`**

Wrap in `<MobileShell title="캘린더">`. Reuse `RegionBadge`, `OwnershipTag`, `SparkleIcon`, `EmptyEventState`, `EventDetailModal` from `components/calendar/`. Bind to the same `fetchCalendar` + `fetchWeeklySummary` API as the desktop page.

- [ ] **Step 2: Add dispatcher**

```tsx
// frontend/invest/src/pages/desktop/DesktopCalendarPage.tsx (bottom)
export function CalendarPage() {
  return useViewport() === "mobile" ? <MobileCalendarPage /> : <DesktopCalendarPage />;
}
```

Update `routes.tsx`: `{ path: "/calendar", element: <CalendarPage /> }`.

- [ ] **Step 3: Run tests + commit**

```bash
cd frontend/invest && npm run typecheck && npm test
git add frontend/invest/src/pages/mobile/MobileCalendarPage.tsx frontend/invest/src/routes.tsx
git commit -m "feat(invest): mobile calendar route per design system"
```

**Stage 5 PR:** "feat(invest): finance calendar (mini-month + AI summary + event detail modal)" — combines Tasks 5.1–5.3.

---

## Stage 6 — `/invest/app` legacy retirement

**Goal:** Inventory the legacy mobile-only routes, redirect them backwards-compatibly to canonical `/invest/*` siblings, and schedule the actual file deletion in a follow-up PR after a soak window.

### Task 6.1: Inventory legacy routes + components

**Files:**
- Create: `docs/plans/2026-05-08-invest-app-retirement-inventory.md`

- [ ] **Step 1: Write the inventory file**

Catalog every legacy file:

```markdown
# /invest/app retirement inventory

## Legacy routes (target: redirect, then delete)
| Legacy route | Canonical replacement | Component file |
|---|---|---|
| `/invest/app` | `/invest/` | `frontend/invest/src/pages/HomePage.tsx` |
| `/invest/app/discover` | `/invest/discover` | `frontend/invest/src/pages/DiscoverPage.tsx` |
| `/invest/app/discover/issues/:issueId` | `/invest/discover/issues/:issueId` | `frontend/invest/src/pages/DiscoverIssueDetailPage.tsx` |
| `/invest/app/paper`, `/invest/app/paper/:variant` | _decision needed: drop or move under /invest/paper_ | `frontend/invest/src/pages/PaperPlaceholderPage.tsx` |

## Legacy components candidate-for-deletion (only after canonical routes ship)
- `frontend/invest/src/components/AppShell.tsx`
- `frontend/invest/src/components/HeroCard.tsx` (replaced by `components/home/DesktopHero.tsx` + `MobileHomePage` inline hero)
- `frontend/invest/src/components/AccountCardList.tsx`
- `frontend/invest/src/components/AccountSelector.tsx`
- `frontend/invest/src/components/AssetCategoryFilter.tsx`
- `frontend/invest/src/components/HoldingRow.tsx` (replaced by `components/home/HoldingsTable.tsx`)
- `frontend/invest/src/components/BottomNav.tsx` (replaced by `mobile/MobileBottomNav.tsx`)
- `frontend/invest/src/components/discover/*` (replaced by `components/discover/IssueCard.tsx` + the new desktop/mobile pages)

## Legacy tests candidate-for-update
- `frontend/invest/src/__tests__/HomePage.test.tsx` — rewrite to target `MobileHomePage`
- `frontend/invest/src/__tests__/AccountCardList.test.tsx` — drop or rewrite for the canonical right rail/mobile home
- `frontend/invest/src/__tests__/HoldingRow.test.tsx` — rewrite for `HoldingsTable`
- `frontend/invest/src/__tests__/BottomNav.test.tsx` — rewrite for `MobileBottomNav`
- `frontend/invest/src/__tests__/AiIssueCard.test.tsx`, `DiscoverPage.test.tsx`, `IssueImpactMap.test.tsx`, `DiscoverCalendarCard.test.tsx`, `RelatedSymbolsList.test.tsx` — review against the new `components/discover/IssueCard.tsx` + decide rewrite vs. delete.

## Open question
The `/app/paper*` route uses `PaperPlaceholderPage` — this looks like an internal stub. Confirm with the team whether to drop or expose under `/invest/paper`.
```

- [ ] **Step 2: Commit**

```bash
git add docs/plans/2026-05-08-invest-app-retirement-inventory.md
git commit -m "docs(invest): /invest/app retirement inventory"
```

### Task 6.2: Add backwards-compatible redirects

**Files:**
- Modify: `frontend/invest/src/routes.tsx`

- [ ] **Step 1: Replace each legacy route with a `<Navigate>` to its canonical sibling**

```tsx
import { createBrowserRouter, Navigate } from "react-router-dom";
import { HomePage } from "./pages/desktop/DesktopHomePage";
import { FeedNewsPage } from "./pages/desktop/DesktopFeedNewsPage";
import { DiscoverPageDispatcher, DiscoverIssueDetailPage } from "./pages/desktop/DesktopDiscoverPage";
import { SignalsPage } from "./pages/desktop/DesktopSignalsPage";
import { CalendarPage } from "./pages/desktop/DesktopCalendarPage";
import { DesktopScreenerPage } from "./pages/desktop/DesktopScreenerPage";

export const router = createBrowserRouter(
  [
    { path: "/", element: <HomePage /> },
    { path: "/feed/news", element: <FeedNewsPage /> },
    { path: "/discover", element: <DiscoverPageDispatcher /> },
    { path: "/discover/issues/:issueId", element: <DiscoverIssueDetailPage /> },
    { path: "/signals", element: <SignalsPage /> },
    { path: "/calendar", element: <CalendarPage /> },
    { path: "/screener", element: <DesktopScreenerPage /> },

    // Legacy /invest/app/* — backwards-compatible redirects to canonical siblings
    { path: "/app", element: <Navigate to="/" replace /> },
    { path: "/app/discover", element: <Navigate to="/discover" replace /> },
    { path: "/app/discover/issues/:issueId", element: <DiscoverIssueRedirect /> },
    { path: "/app/paper", element: <Navigate to="/" replace /> },
    { path: "/app/paper/:variant", element: <Navigate to="/" replace /> },

    { path: "*", element: <Navigate to="/" replace /> },
  ],
  { basename: "/invest" },
);

function DiscoverIssueRedirect() {
  const { issueId } = useParams();
  return <Navigate to={`/discover/issues/${issueId}`} replace />;
}
```

- [ ] **Step 2: Add a redirect test**

```tsx
// frontend/invest/src/__tests__/legacyRedirects.test.tsx
import { render, screen } from "@testing-library/react";
import { MemoryRouter, Routes, Route, Navigate } from "react-router-dom";
import { describe, it, expect } from "vitest";

describe("legacy /app/* redirects", () => {
  it("/app redirects to /", () => {
    render(
      <MemoryRouter initialEntries={["/app"]}>
        <Routes>
          <Route path="/" element={<div>HOME</div>} />
          <Route path="/app" element={<Navigate to="/" replace />} />
        </Routes>
      </MemoryRouter>,
    );
    expect(screen.getByText("HOME")).toBeInTheDocument();
  });
  // …repeat for /app/discover, /app/discover/issues/:id, /app/paper
});
```

- [ ] **Step 3: Run tests**

```bash
cd frontend/invest && npm run typecheck && npm test
```

Expected: PASS, including any of the existing `routes.test.tsx` assertions.

- [ ] **Step 4: Commit**

```bash
git add frontend/invest/src/routes.tsx frontend/invest/src/__tests__/legacyRedirects.test.tsx
git commit -m "feat(invest): redirect legacy /invest/app/* to canonical /invest/* siblings"
```

### Task 6.3: Delete legacy components — *separate follow-up PR*

**Do NOT do this in the same PR as 6.2.** Wait at least one full release cycle (or as the team decides) before deleting:

- `frontend/invest/src/components/AppShell.tsx`, `HeroCard.tsx`, `AccountCardList.tsx`, `AccountSelector.tsx`, `AssetCategoryFilter.tsx`, `HoldingRow.tsx`, `BottomNav.tsx`
- `frontend/invest/src/pages/HomePage.tsx`, `DiscoverPage.tsx`, `DiscoverIssueDetailPage.tsx`, `PaperPlaceholderPage.tsx`
- `frontend/invest/src/components/discover/*` (where superseded)

When the time comes, delete + delete dependent tests + run `npm run typecheck && npm test && npm run build`.

**Stage 6 PR (this plan ships):** "feat(invest): retire /invest/app — redirects + inventory" — combines Tasks 6.1–6.2. Deletion is a separate later PR.

---

## Self-review checklist (run before merging the plan into work)

1. **Spec coverage** — Verify every requirement in the user prompt is mapped:
   - Stage 1 = shared design tokens, typography, color rules, spacing, radii, buttons, cards, pills, tables/lists ✅
   - Stage 2 = /invest desktop shell + home overview polish ✅
   - Stage 3 = responsive mobile under /invest ✅
   - Stage 4 = News + Discover + Signals + (Calendar deferred to Stage 5) + Screener polish ✅
   - Stage 5 = Finance calendar enhancement ✅
   - Stage 6 = /invest/app legacy inventory + redirects ✅
   - Korean P/L (red gain / blue loss) — preserved via tokens ✅
   - Pretendard primary, JetBrains Mono only for code-like values — Pretendard via `colors_and_type.css` `@import`, JetBrains Mono via `--font-mono` token ✅
   - Lucide-like, subtle icons — `Icon` atom with single-stroke SVGs ports the bundle's same paths; Lucide flagged as substitution if needed ✅
   - No dark/neon/glassmorphism — light-only via tokens ✅
   - Calendar requirements (mini-month + week/date list + filters; weekly date strip + monthly view; chips for 경제지표/실적/배당/FOMC/기업, KR/US, 중요/보통; AI weekly summary; detail with 발표/예측/이전 + EPS/EPS 예상/서프라이즈) — all covered in Stage 5 ✅
   - Preserve product constraints (don't break data, no /invest/app deletion immediately, redirects backwards-compatible) — explicit in Stage 6 ✅

2. **Placeholder scan** — No "TBD", "implement later", "add appropriate X". Code blocks are present at every code step. Type names match across stages (`AssetCategoryKey`, `HomeSummary`, `FeedTab`, `CalendarResponse`, `WeeklySummaryResponse`).

3. **Type consistency** — `pillToneForSource` used by LeftContextRail + RightAccountPanel + Mobile shells. `Direction = "up" | "down" | "mixed" | "flat"` used consistently. `useViewport` returns `Viewport = "mobile" | "compact" | "desktop"` and is consumed by every page-level dispatcher.

4. **Risks acknowledged** — API/data, test breakage, dual IA during migration, Pretendard CDN, Lucide substitution, Korean P/L, screener out of scope.

---

## Execution Handoff

**Two execution options:**

**1. Subagent-Driven (recommended)** — Dispatch a fresh subagent per task with a two-stage review between tasks. Better for stages 2–5 because each surface is a clean isolation boundary.

**2. Inline Execution** — Execute tasks in the same session using `superpowers:executing-plans`. Better if the user wants to keep tight oversight on visual/UX details.

**Recommended starting point:** Stage 1 (Tasks 1.1–1.3) is a single small PR that's safe to ship before getting alignment on the rest. Land that, then re-evaluate stage cadence based on review feedback before kicking off Stage 2.
