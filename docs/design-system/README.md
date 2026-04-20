# Auto Trader Design System

A cohesive visual + interaction language for **Auto Trader**, an AI-assisted
multi-market (KR stocks / US equities / crypto) trading workspace. This
system unifies the data-dense fintech workflows already shipping in
`screener_dashboard.html`, `portfolio_dashboard.html`, and
`pending_orders_dashboard.html`, and gives older Bootstrap-y pages
(`login.html`, `register.html`, `admin_users.html`, legacy `nav.html`) a
clear migration target.

## Sources

- **GitHub repo:** `mgh3326/auto_trader` (main branch)
- **Newer, target-direction templates** (warm neutral, operator-first):
  - `app/templates/screener_dashboard.html`
  - `app/templates/portfolio_dashboard.html`
  - `app/templates/pending_orders_dashboard.html`
  - `app/templates/screener/_filters.html`, `_report_panel.html`,
    `_order_panel.html`, `_table.html`, `_layout.html`
  - `app/templates/portfolio_position_detail.html`,
    `screener_report_detail.html`
- **Legacy templates** (to migrate):
  - `app/templates/base.html`, `nav.html`, `login.html`, `register.html`,
    `error.html`, `admin_users.html`
- **Reference file (out of scope for this pass):** `design/screener.pen`

---

## Index

- `README.md` — this file
- `colors_and_type.css` — all tokens + semantic classes (drop-in)
- `SKILL.md` — portable skill entrypoint
- `preview/` — component / token preview cards that populate the
  Design System tab

## Repo Placement

This repo keeps design-system documentation and previews under
`docs/design-system/`, and serves runtime tokens from
`app/static/css/colors_and_type.css`.

---

## Product context

Auto Trader is a single-operator workspace combining:

1. **Screener** — filter KR / US / crypto universes by volume, RSI,
   market cap, change rate; generate AI (Gemini) reports per symbol.
2. **Portfolio** — aggregated view across KIS (KR/US) accounts + Upbit
   (crypto) + manual holdings (e.g. Toss).
3. **Pending orders** — live list of open orders across markets with
   price-gap indicators + auto-refresh.
4. **Report detail** — drill-down into an AI decision (buy/hold/sell,
   confidence %, reasons, 4 price ranges).
5. **Order panel** — limit/market buy + sell with a dry-run toggle.
6. **Auth / admin** — username-password login, register, user mgmt.

Desktop-first, Korean + English copy, responsive fallback for
table-heavy views (tables → stacked cards under ~760px).

---

## Design tokens (summary)

See `colors_and_type.css` for the full list. Highlights:

| Group | Token | Value |
|---|---|---|
| Surface | `--bg-page` | `#F5F3EF` |
| Surface | `--bg-card` | `#FFFFFF` |
| Surface | `--bg-panel` | `#FCFBF8` |
| Surface | `--bg-subtle` | `#F4F2EC` (table head) |
| Dark chrome | `--bg-dark` → `--bg-dark-3` | `#1A1A1A` → `#45352E` |
| Border | `--border-light` | `#D1CCC4` |
| Text | `--text-primary` / `--text-secondary` | `#242220` / `#4F4A44` |
| Brand | `--accent` | `#C05A3C` (terracotta) |
| Semantic | `--success` / `--error` / `--warn` / `--info` | `#4A7C59` / `#B54A4A` / `#8D6E2F` / `#1D5F7A` |
| Market | `--mkt-kr` / `--mkt-us` / `--mkt-crypto` | `#225F9C` / `#2F7A4F` / `#80592F` |
| Direction (KR) | `--profit-pos` / `--profit-neg` | `#9A1C1C` / `#0F4C9C` |
| Radius | `--r-md` / `--r-lg` / `--r-xl` / `--r-2xl` | `0.5rem` / `0.6rem` / `0.75rem` / `1rem` |
| Shadow | `--elev-card` | `0 18px 40px rgba(24,19,16,.08)` |
| Font | `--font-sans` | Pretendard Variable, Pretendard, Noto Sans KR |
| Font | `--font-mono` | ui-monospace stack |

---

## Component inventory

All specs are drawn from the three newer dashboards — see
`preview/` for visual cards, and `colors_and_type.css` for token refs.

### Buttons

| Variant | Class in code | Use |
|---|---|---|
| **Primary** (dark) | `.btn.btn-dark.btn-sm` | Search, Submit Order, Generate Report |
| **Secondary** (outline dark) | `.btn.btn-outline-dark.btn-sm` | Refresh, Stop Polling, row "Report" |
| **Accent** (terracotta, soft) | `.btn-primary-soft` | Portfolio header CTA (refresh) |
| **Neutral outline** | `.btn-outline-soft` / `.btn-outline-secondary` | Cancel, secondary links ("Back to Dashboard") |

Sizing: `sm` everywhere (`0.45rem 0.72rem` padding, `0.82rem` font).
Radius `--r-lg` (0.6rem). Accent hover = `translateY(-1px)` +
`--elev-cta`. No gradient fills.

### Inputs & selects

- `border: 1px solid var(--border-light)`, `radius: var(--r-md)`,
  `padding: 0.5rem 0.55rem`, `font-size: var(--fs-base)`.
- Labels above, `--fs-sm`, `--text-secondary`.
- Focus → `border-color: var(--accent)`, no outline-ring pill.
- Checkboxes inline with `.subtle` caption ("Execute real order
  (unchecked = dry run)").

### Tabs / segmented controls

- **Market tabs** (pending orders): `.market-tab` pill — white bg →
  `--bg-dark` bg when `.active`.
- **Side tabs** (order panel Buy/Sell): `.btn-group-sm` pair of
  `btn-outline-dark` — active one gets `.active` class.

### Badges

| Type | Example | Style |
|---|---|---|
| **Status** (pill) | queued / running / completed / failed | `--r-pill`, soft bg + matching text color + 1px ring. queued=`--warn`, running=`--info`, completed=`--success`, failed=`--error`. |
| **Side** (order) | BUY / SELL | `--r-sm` rect, uppercase, 0.75rem. buy=`#fee2e2/#dc2626`, sell=`#dbeafe/#2563eb`. |
| **Market** | KR / US / CRYPTO | `--r-pill`, solid `--mkt-*` bg, white text. |
| **Indicator** | RSI14, MA20 | `--r-sm`, mono, `--bg-subtle` bg. |

### Cards / panels

- **Shell** (outer frame): white bg, `--border-light`, `--r-2xl`,
  `--elev-card`, `overflow: hidden`.
- **Header band**: `linear-gradient(120deg, #1A1A1A 0%, #2F2B28 50%,
  #45352E 100%)`, padding `1.2rem 1.4rem`, `--on-dark` text.
- **Panel** (inside shell): `--bg-panel` bg, `--border-light`, `--r-xl`,
  `--sp-5` padding.
- **KPI / summary card**: `--bg-panel`, center-aligned; eyebrow label
  (`.at-eyebrow`) above `.at-kpi` number.
- **Result card** (mobile-stack fallback for tables): white bg,
  `--r-lg`, title row (symbol + mono ticker) + 2-col `<dl>` metric grid.

### Tables

- Header: `--bg-subtle` (portfolio uses `--bg-elev`), `--fs-xs`
  tracking-label, sticky top.
- Body: `0.48rem 0.45rem` padding, `border-bottom: 1px solid
  --border-dash`, hover row → `--bg-hover`.
- Numeric cells right-aligned; symbol / name cells left-aligned; symbol
  uses `--font-mono`.
- Below ~760px the table is hidden and a `.result-card` / `.order-card`
  stack renders instead.

### Drawers / modals / side panels

The codebase uses **side panels in a CSS grid**, not true modals:
- Screener: `grid-template-columns: minmax(0,1fr) clamp(320px,28vw,380px)`
  — report + order panels live in the right rail.
- Portfolio: a `.secondary-grid` of 3 equal panels under the main table.
- On `<=1280px` the rail collapses to single-column.

### Empty / loading / error states

- **Empty**: `.empty-note` — 1px dashed `#D8D0C6`, centered secondary
  text, "No rows found for the current filters."
- **Loading skeleton**: `.skeleton-line` / `.skeleton-card` —
  `linear-gradient(90deg, #ECE7DF 25%, #F7F3ED 50%, #ECE7DF 75%)` with
  1.2s shimmer keyframes.
- **Error**: `.error-text` paragraph in `--error`; inline report errors
  show inside the report panel, not as a toast.
- **Last-updated chip**: `.at-muted` text right-aligned in panel header.

---

## Page patterns

### Screener (`screener_dashboard.html`)

```
shell
 ├─ header (dark band: "OpenClaw Screener Dashboard")
 └─ page-grid   ← 2-col: primary 1fr / sidebar clamp(320,28vw,380)
     ├─ primary
     │   ├─ panel: Filters (6-field grid: market / sort_by / order /
     │   │        max_rsi / min_volume / limit  → Search + Refresh)
     │   └─ panel: Results table (sticky head, per-row "Report" btn)
     └─ sidebar
         ├─ panel: Report status + decision/confidence/reasons + price
         │        analysis table
         └─ panel: Order form (symbol, side tabs, type, qty/price/amt,
                  reason, dry-run checkbox, Submit)
```

Polling: visible `queued → running → completed/failed` badge, Stop
Polling button; `Job: <mono job_id>` under the status.

### Portfolio (`portfolio_dashboard.html`)

```
shell
 ├─ header (dark band: "통합 포트폴리오")
 └─ stack
     ├─ panel: Controls (account filter + market filter + refresh CTA)
     ├─ panel: Portfolio table (market badge / name / qty / avg / last /
     │        P&L / weight — gain red / loss blue)
     └─ secondary-grid (3 cols)
         ├─ Cash & Balances
         ├─ Summary (totals, weights)
         └─ Warnings (error-soft list)
```

### Pending orders (`pending_orders_dashboard.html`)

```
shell
 ├─ header (dark band: "미체결 주문 현황")
 └─ content
     ├─ summary-grid (KPI cards: 총/매수/매도/…)
     ├─ controls row (market tabs + auto-refresh toggle + last-updated)
     ├─ table-container (side badge, price, gap-indicator bar, indicators)
     └─ orders-cards (mobile fallback, same fields stacked)
```

Gap indicator: 60×6 bar with fill colored `near|medium|far` →
`--success|--warn|--error`.

### Report detail + position detail

Same shell + dark header pattern; two panels side-by-side (status /
report) using the sub-includes `_report_panel.html` and
`_order_panel.html` with `report_mode = "detail"` / `order_mode =
"detail"`.

### Auth / admin (legacy — to migrate)

`login.html`, `register.html`, `error.html`, `admin_users.html` still
use Bootstrap primary blues + a `#667eea → #764ba2` gradient button,
`#e0e0e0` inputs, rounded-20 auth card. **These do not match the warm
neutral direction and are the primary migration target.**

---

## Migration plan (legacy → warm neutral fintech)

Ranked by bang-per-buck; all steps are additive — page-by-page, no
rewrite.

1. **Adopt tokens globally.** Import `colors_and_type.css` into
   `base.html` (before the Bootstrap sheet) and remove the per-page
   `:root` duplicates. Single source of truth.

2. **Retire the indigo gradient CTA.** Replace
   `background: linear-gradient(135deg, #667eea, #764ba2)` (login,
   register) with `.btn.btn-dark.btn-sm` or `.btn-primary-soft` for
   brand-tinted primary actions. Lose `translateY(-2px)` +
   indigo shadow in favor of `--elev-cta`.

3. **Rehome the auth card.** Swap `auth-card` chrome:
   - bg `#fff` + `--elev-card` instead of `0 20px 60px rgba(0,0,0,.3)`
   - border `1px solid --border-light`, radius `--r-xl` (0.75rem) —
     not 20px. 20px pills look app-store-ish; our shell is 1rem.
   - inputs → `border: 1px solid --border-light`, radius `--r-md`,
     focus `--border-focus`. Drop `#667eea` focus.
   - error banner → `.error-text` + `--error-soft` background.

4. **Rebrand the nav.** `nav.html` uses
   `navbar-light bg-light` + `text-primary` (Bootstrap blue) brand.
   Replace with the dark header band used on the dashboards, moved to
   full-bleed above the shell:
   - `bg` = `--bg-dark` gradient, brand lockup + links = `--on-dark`
   - active link underline in `--accent`
   - badge next to username → `.badge-status` variants, not
     `.badge.bg-primary`.

5. **Map Bootstrap utility → semantic tokens.** A grep-and-replace
   migration table:

   | Bootstrap | Replace with |
   |---|---|
   | `bg-light` | `--bg-subtle` or `--bg-panel` |
   | `bg-white` | `--bg-card` |
   | `text-primary` | `--accent` (links) or `--text-primary` (copy) |
   | `text-muted` | `--text-secondary` |
   | `btn-primary` | `.btn.btn-dark.btn-sm` |
   | `btn-outline-primary` | `.btn.btn-outline-dark.btn-sm` |
   | `btn-danger` | `.btn-primary-soft` reversed to `--error` (rare) |
   | `.card` | `.panel` (keep border + radius) |
   | `.badge.bg-success/.bg-danger/.bg-warning` | `.badge-status.completed / .failed / .queued` |

6. **Standardize tables.** All tables use the pattern already in
   `screener_dashboard.html` + `portfolio_dashboard.html`: sticky
   `--bg-subtle` header, `--fs-xs` tracking-label th, `--border-dash`
   row separators, right-aligned numerics, mobile `.result-card` stack.

7. **Unify page chrome.** Every page wrapped in
   `<main class="shell"><header class="header">…</header>…</main>`,
   centered at `max-width` 1360–1480px. No more page-level bootstrap
   containers.

8. **Port KR gain/loss convention everywhere.** Portfolio already uses
   `profit-positive` = `#9A1C1C` red, `profit-negative` = `#0F4C9C`
   blue. Apply the same to change_rate cells in screener + any
   future P&L column.

9. **Loading + empty states.** Replace any Bootstrap spinners with
   `.skeleton-line/.skeleton-card`. Replace empty table messages with
   `.empty-note`.

10. **Page-by-page cutover order** (lowest risk first):
    `error.html` → `login.html` → `register.html` → `nav.html` →
    `admin_users.html` → `base.html` scripts cleanup.

---

## Content fundamentals

**Languages.** Korean-first for user-facing chrome (페이지 제목,
버튼, 빈 상태, 에러); English-first for technical / operator chrome
(filter labels, job IDs, API terms). Don't machine-translate one
into the other — the bilingual split is intentional: operators live
with both.

Examples pulled directly from code:

- Dark header (Korean):
  - `통합 포트폴리오` · `미체결 주문 현황`
  - subline: `전체 시장의 미체결 주문을 실시간으로 확인하세요.`
- Dark header (English/operator):
  - `OpenClaw Screener Dashboard`
  - subline: `Run filters, generate symbol reports, and execute
    orders from one Jinja page.`
- Nav items: `스크리너 · 미체결 주문 · 포트폴리오 · 관리자`
- Buttons: `Search` · `Refresh Cache` · `Generate Report` ·
  `Stop Polling` · `Submit Order` · `로그인` · `회원가입` ·
  `로그아웃`
- KPI eyebrow labels: `총 주문 수`, `매수 주문`, `매도 주문`
- Helper copy (always `--subtle`): `Select a row and generate a
  report.` · `No rows found for the current filters.` ·
  `Execute real order (unchecked = dry run)`
- Status vocabulary (lowercase, machine-facing): `queued`,
  `running`, `completed`, `failed`, `idle`, `not-set`.

**Tone.** Operator-first, literal, calm. We describe what a click
does, not why you should click. Never hype, never exclamation marks,
never emoji-as-decoration. The product is infrastructure for the
person's own money — overstatement corrodes trust.

**Person.** No "we" / "you" in instructional copy. Use imperative
verbs — "Select a symbol first.", "Waiting for report payload."

**Casing.**
- English chrome: Title Case for buttons / panel titles
  ("Generate Report", "Price Analysis"); lowercase for status
  tokens ("queued"); mono-uppercase for tickers ("AAPL", "BRK.B").
- Korean: 문장식(문장형) for most copy; button labels drop the
  final 조사 ("로그인", not "로그인하기").

**Numbers.** Always `Number.toLocaleString()` with
`maximumFractionDigits: 2` (prices up to 4). Currency symbol
precedes value (`$123.45`, `₩1,234,000`). Percent strings include
the `%` sign (`confidence: 72%`). Monospace for all numeric cells.

**Emoji.** None outside Bootstrap Icons glyphs used in nav. If a
glyph is needed, use the icon, not an emoji.

---

## Visual foundations

**Motifs.** Warm-neutral paper + dark structural chrome. Think:
a broker's printed deal ticket, reinterpreted for a web workspace.
The background is warm (`#F5F3EF`), the shell is white, the header
band is near-black with a subtle warm gradient. Accent is a
restrained terracotta (`#C05A3C`) — it appears on CTAs and focus,
never on large fills.

**Background.** `radial-gradient(circle at top right, #F9F7F3 0%,
#F5F3EF 45%, #ECE8DF 100%)` on `<body>`. No full-bleed imagery, no
photography, no repeating patterns, no illustrations. The gradient
is calm enough that the eye parks on the shell.

**Shell.** Every page is `<main class="shell">`, centered, max
1360–1480px, rounded `1rem`, bordered `1px --border-light`, drop
shadow `0 18px 40px rgba(24,19,16,.08)`, `overflow: hidden` so the
dark header clips cleanly.

**Dark header band.** `linear-gradient(120deg, #1A1A1A 0%, #2F2B28
50%, #45352E 100%)`, `1.2rem 1.4rem` padding, title `--fs-xl`,
subline `--fs-base` in `--on-dark-muted`. This is the only gradient
we use.

**Panels.** Nested inside the shell. Off-white `#FCFBF8`, radius
`.75rem`, `.9rem` padding, 1px `--border-light` border, no
shadow. Panels carry all mid-density content (filters, tables,
forms).

**Cards & elevation.** We use only two elevation levels: shell
(`--elev-card`) and CTA hover (`--elev-cta`). Panels and KPI cards
do not have shadows — they separate by background + border alone.

**Corner radii.** Escalating: 0.25 (side badge) → 0.5 (input) →
0.6 (button, small panel) → 0.75 (panel) → 1.0 (shell) → 999
(status pill). Pick by container size; never mix two radii on the
same element.

**Type.** Pretendard Variable → Pretendard → Noto Sans KR → Apple SD
Gothic Neo → system. The variable file ships with the system at
`fonts/PretendardVariable.ttf` and is loaded via `@font-face` in
`colors_and_type.css`, so weight 100–900 is available without a
network round-trip. Pretendard has excellent Hangul + Latin metrics
and is the visual DNA. Mono stack is plain `ui-monospace` — we want
the OS mono, not a bespoke geometric mono. Display feels fintech
because weights sit at
500/600 and tracking is tight (`0.02em`), not because of a decorative
face.

**Animation.** Restrained. `transition: all .18s ease` on buttons,
`.15s` on tabs, `.3s` on gap-bar fills, `.2s` on toggle switches.
One keyframe animation exists (`skeleton-shimmer`, 1.2s linear). No
bounces, no springy overshoot, no parallax, no fade-on-scroll.

**Hover / press.**
- Primary (dark) button: no color change — cursor + focus ring is
  enough.
- Accent soft button: `translateY(-1px)` + `--elev-cta`.
- Outline button: no transform; background nudges to `--bg-subtle`.
- Table row: bg → `--bg-hover`. No cursor change unless clickable.
- Press state is implicit via native `:active`; we don't override.

**Borders & dividers.** Two tones only: `--border-light` (`#D1CCC4`)
for structural borders, `--border-dash` (`#ECE7DF`) for intra-panel
row separators (often `1px dashed`). No shadowy inner rings.

**Transparency & blur.** Used only in status-badge backgrounds
(`rgba(…, 0.08)`) and ring borders (`rgba(…, 0.35)`). No backdrop
blur, no glassmorphism anywhere.

**Imagery vibe.** There's no product imagery. If a future page
needs one, it should be warm (slight sepia), low-saturation, no
screenshot glare. Avoid cool/blue stock photos.

**Layout rules.**
- Desktop-first, single `shell` centered on the warm background.
- Below 1280px: any 2-col `page-grid` collapses to 1 col.
- Below 768–760px: tables hide, `.result-card` / `.order-card`
  stacks appear; summary grids drop to 2 cols; header band stays.
- The shell loses its rounded corners and margin below 760px and
  becomes full-bleed (`margin: 0; border-radius: 0; min-height:
  100vh`).
- Fixed elements: none. No sticky side nav, no floating action
  buttons. Sticky only applies to table `<th>` inside scroll
  containers.

**Information density.** Body is 13.6px (`--fs-base`). Tables go
12.8px. Page titles cap at 20–24px. We do not inflate for
"accessibility optics" — this is a tool, and the operator prefers
seeing more at once. All color pairs hit WCAG AA on paper
background.

---

## Iconography

**System.** [Bootstrap Icons 1.11.3](https://icons.getbootstrap.com/),
loaded via CDN in the legacy nav:

```html
<link rel="stylesheet"
      href="https://cdn.jsdelivr.net/npm/bootstrap-icons@1.11.3/font/bootstrap-icons.css">
```

Icons are rendered as `<i class="bi bi-…">` alongside the label,
never standalone. Current usages observed:

- `bi-rocket-takeoff-fill` — brand lockup
- `bi-search` — 스크리너
- `bi-clock-history` — 미체결 주문 / dashboard header
- `bi-wallet2` — 포트폴리오
- `bi-people-fill` — 관리자
- `bi-box-arrow-right` — 로그아웃

**Weight / style.** Bootstrap Icons are 1.5–2px-stroke outline with
`-fill` solid variants. Stay consistent: outline in chrome, fill for
the brand/emphasis glyph only.

**Emoji.** Not used and should not be introduced.

**Unicode-as-icon.** Not used.

**Custom SVGs.** None in the templates. The `.gap-bar` indicator is
built from two `<div>`s, not an SVG.

**Substitution note.** The repo ships no proprietary icon assets —
Bootstrap Icons is the entire system. If a future screen needs a
glyph that BI doesn't cover, fall back to
[Lucide](https://lucide.dev/) at 1.5px stroke (the closest visual
match), and document the swap here.

**Logo.** There is currently no bitmap or SVG logo asset. The brand
lockup is `bi-rocket-takeoff-fill` + the wordmark “Auto Trader”, set
in Pretendard Variable. When a dedicated logo file becomes available, add it
as a new preview card and reference it from this section.

---

## Open items

- **Supplemental reference file.** `design/screener.pen` is listed in
  Sources as a supplementary reference and is not incorporated into
  this version of the system. If it contains additional motifs —
  iconography or layout variants — fold them into the Iconography
  and Visual Foundations sections in a follow-up pass.
- **Typography distribution.** Pretendard Variable (weights 100–900) is
  bundled at `fonts/PretendardVariable.ttf` and wired through
  `@font-face` in `colors_and_type.css`. Noto Sans KR and Apple SD
  Gothic Neo remain as system-level fallbacks; no additional webfont
  files are required.
- **Brand logo.** See Iconography → Logo. A dedicated logo asset is
  not yet part of the system.
- **Legacy pages.** `login`, `register`, `admin_users`, `nav`,
  `base`, and `error` still carry Bootstrap-legacy styles. They are
  documented as the migration target, not as part of the system.
