# ROB-7 — Trading Decision Workspace UI Plan

> **For agentic workers:** Implement task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. Implementer must be **Codex (`codex --yolo`)** in the same worktree (see §16 handoff prompt).

- **PR scope:** Prompt 4 of `~/.hermes/workspace/prompts/auto_trader_trading_decision_workspace_roadmap.md` only.
- **Branch / worktree:** `feature/ROB-7-trading-decision-workspace-ui` at `/Users/mgh3326/work/auto_trader-worktrees/feature-ROB-7-trading-decision-workspace-ui`.
- **Status:** Plan only. No implementation yet. Codex YOLO is the implementer.
- **Depends on (already merged to `main`):**
  - ROB-1 / PR #595 — DB schema + service layer (`app/models/trading_decision.py`, `app/services/trading_decision_service.py`).
  - ROB-2 / PR #597 — API endpoints (`app/routers/trading_decisions.py`, `app/schemas/trading_decisions.py`).
  - ROB-6 / PR #598 — React+Vite+TS workspace + FastAPI SPA route (`frontend/trading-decision/`, `app/routers/trading_decisions_spa.py`).

> ⚠️ This PR ships **the interactive decision workspace UI only**: list sessions, detail page, per-proposal accept / reject / defer / modify / partial_accept, list-style multi-select responses, original-vs-adjusted display, and linked-action display. **Out of scope:** outcome marks (ROB-5/Prompt 5), Hermes ingestion, Discord delivery, periodic reassessment, broker / watch / KIS / Upbit / Redis side effects, and any new FastAPI route or schema change.

**Goal:** Ship a usable single-page React UI that lets an authenticated user open a decision session inbox, drill into a session, and respond to each proposal individually — including modify/partial_accept with inline adjusted values — recording every action through the existing ROB-2 API only.

**Architecture:** React 19 + Vite 8 + TypeScript 6 SPA hosted at `/trading/decisions/*`. Two routes (`/` inbox, `/sessions/:sessionUuid` detail) via `react-router-dom`. Data fetched through the existing `apiFetch` wrapper (cookie-based same-origin auth). Local component state + a tiny data-hook layer (`useDecisionSession`, `useDecisionInbox`); no global store. All Decimal fields are JSON strings on the wire (Pydantic v2 default) — UI keeps them as strings, only parsing for display formatting and only re-serializing strings on submit.

**Tech Stack:** React 19, Vite 8, TypeScript 6 strict, React Router 6, Vitest 3 + @testing-library/react + jsdom for tests. No CSS framework — plain CSS modules in `src/components/<Component>.module.css`. No state library. No date library (use `Intl.DateTimeFormat`).

---

## 1. Workflow the UI must support (from Prompt 4)

```text
1. User opens /trading/decisions/                       → SessionListPage
   - Sees a paginated inbox of their decision sessions, newest first.
   - Each row: source_profile, strategy_name, market_scope, generated_at,
     status badge, "N proposals (M pending)" counter.
   - Click row → /trading/decisions/sessions/{uuid}

2. User opens /trading/decisions/sessions/{uuid}        → SessionDetailPage
   - Header: session metadata + status badge.
   - Market brief block: pretty-printed JSON (collapsible). Notes shown if present.
   - Proposal list: one ProposalRow per proposal in original order.
     For each proposal:
       a. analyst suggestion is always visible (symbol, side, kind, original_*).
       b. response controls: accept / reject / defer / modify / partial_accept.
       c. modify/partial_accept opens an inline adjustment editor.
          - editor shows original_* values as placeholders;
          - user fills user_quantity / user_quantity_pct / user_amount /
            user_price / user_trigger_price / user_threshold_pct (any subset);
          - user_note is always optional;
          - "Save adjustment" → POST /respond with chosen response + user_*.
       d. After a successful response: ProposalRow re-renders showing the
          analyst original AND the user-adjusted values side-by-side.
       e. Linked actions section (read-only):
          - actions: live_order, paper_order, watch_alert, no_action, manual_note
          - shows external_order_id / external_paper_id / external_watch_id
          - shows external_source and recorded_at
          - shows counterfactual rows (kind/baseline/quantity/notes) if any
          - NO outcome marks rendered (out of scope; Prompt 5).
   - Inbox summary updates after a respond round-trip (refetch detail).

3. List-style multi-response (BTC/ETH/SOL → accept BTC+ETH, defer SOL):
   - Each ProposalRow has its own controls; the user clicks per-row.
   - We do NOT add a bulk-respond button; the API does not support it
     (ROB-2 plan §15.7 explicitly defers bulk).
   - We DO add "Apply selected response to highlighted rows" only as
     follow-up if asked — NOT in this PR.
```

---

## 2. In-scope vs Out-of-scope

| Area | In scope (this PR) | Deferred |
|---|---|---|
| `frontend/trading-decision/src/**` UI for inbox + detail | ✅ | — |
| Routing via `react-router-dom` | ✅ | — |
| Typed API client over the 5 read+respond endpoints | ✅ | — |
| Per-proposal `respond` (accept/reject/defer/modify/partial_accept) | ✅ | — |
| Inline adjustment editor with original-vs-user display | ✅ | — |
| Linked-actions read-only panel (live order id, paper id, watch field, no-action) | ✅ | — |
| Counterfactual read-only panel | ✅ | — |
| Vitest + RTL unit tests for components and hooks | ✅ | — |
| Frontend CI workflow update to run `npm run test` | ✅ | — |
| New FastAPI routes / Pydantic schemas / DB columns | ❌ | not needed |
| Creating new sessions or proposals from the UI | ❌ | analyst-side / Hermes |
| `POST /actions`, `POST /counterfactuals`, `POST /outcomes` from the UI | ❌ | Prompt 5 (outcomes) and execution flow |
| Outcome marks UI / analytics | ❌ | ROB-5 / Prompt 5 |
| Discord delivery / push of new sessions | ❌ | future |
| Broker / watch / KIS / Upbit / Redis / order placement | ❌ (forbidden — §10) | — |
| Tailwind / component library / state lib (Redux/Zustand/Jotai) / TanStack Query | ❌ | revisit when justified |
| WebSocket push / live updates | ❌ | future |
| i18n / Korean translations | ❌ | follow-up |
| Theme switcher / dark mode | ❌ | follow-up |

---

## 3. Existing scaffold inventory (do not re-create)

```text
frontend/trading-decision/                            (ROB-6, already on main)
├── package.json                  React 19, Vite 8, TS 6 — KEEP, EXTEND
├── tsconfig.json                 strict, noUncheckedIndexedAccess — KEEP
├── tsconfig.node.json            — KEEP
├── vite.config.ts                base "/trading/decisions/", proxy to :8000 — KEEP
├── index.html                    — KEEP
└── src/
    ├── main.tsx                  StrictMode root — KEEP, wrap with router
    ├── App.tsx                   currently <HelloDecision/> — REPLACE
    ├── App.css                   minimal — REPLACE with route shell css
    ├── api/client.ts             apiFetch wrapper — KEEP, EXTEND
    ├── components/HelloDecision.tsx   placeholder — DELETE
    └── pages/HelloPage.tsx       placeholder — DELETE
```

```text
app/routers/trading_decisions.py        (ROB-2, already on main) — DO NOT EDIT
app/routers/trading_decisions_spa.py    (ROB-6, already on main) — DO NOT EDIT
app/schemas/trading_decisions.py        (ROB-2, already on main) — DO NOT EDIT
app/services/trading_decision_service.py (ROB-1+2)               — DO NOT EDIT
app/models/trading_decision.py          (ROB-1)                  — DO NOT EDIT
```

The implementer must **not** touch any Python file in this PR. If the implementation feels like it needs a backend change, stop and surface it in the PR description.

---

## 4. File structure

### 4.1 New files

```text
frontend/trading-decision/
├── vitest.config.ts                                      NEW
├── src/
│   ├── App.tsx                                           REPLACE
│   ├── App.css                                           REPLACE  (route-shell only)
│   ├── routes.tsx                                        NEW      (router definition)
│   ├── api/
│   │   ├── client.ts                                     KEEP+extend
│   │   ├── decisions.ts                                  NEW      (typed client funcs)
│   │   └── types.ts                                      NEW      (mirror ROB-2 schemas)
│   ├── hooks/
│   │   ├── useDecisionInbox.ts                           NEW
│   │   └── useDecisionSession.ts                         NEW
│   ├── pages/
│   │   ├── SessionListPage.tsx                           NEW
│   │   ├── SessionListPage.module.css                    NEW
│   │   ├── SessionDetailPage.tsx                         NEW
│   │   └── SessionDetailPage.module.css                  NEW
│   ├── components/
│   │   ├── StatusBadge.tsx                               NEW
│   │   ├── StatusBadge.module.css                        NEW
│   │   ├── MarketBriefPanel.tsx                          NEW
│   │   ├── MarketBriefPanel.module.css                   NEW
│   │   ├── ProposalRow.tsx                               NEW
│   │   ├── ProposalRow.module.css                        NEW
│   │   ├── ProposalResponseControls.tsx                  NEW
│   │   ├── ProposalAdjustmentEditor.tsx                  NEW
│   │   ├── ProposalAdjustmentEditor.module.css           NEW
│   │   ├── OriginalVsAdjustedSummary.tsx                 NEW
│   │   ├── LinkedActionsPanel.tsx                        NEW
│   │   ├── LinkedActionsPanel.module.css                 NEW
│   │   ├── ErrorView.tsx                                 NEW
│   │   └── LoadingView.tsx                               NEW
│   ├── format/
│   │   ├── decimal.ts                                    NEW      (string-safe Decimal display)
│   │   └── datetime.ts                                   NEW      (Intl-based)
│   └── test/
│       ├── setup.ts                                      NEW
│       ├── server.ts                                     NEW      (fetch mock helper)
│       └── fixtures.ts                                   NEW      (canned API payloads)
└── src/__tests__/                                        NEW dir
    ├── api.decisions.test.ts                             NEW
    ├── format.decimal.test.ts                            NEW
    ├── ProposalRow.test.tsx                              NEW
    ├── ProposalAdjustmentEditor.test.tsx                 NEW
    ├── ProposalResponseControls.test.tsx                 NEW
    ├── LinkedActionsPanel.test.tsx                       NEW
    ├── SessionListPage.test.tsx                          NEW
    └── SessionDetailPage.test.tsx                        NEW
```

### 4.2 Files modified

| File | Change | Why |
|---|---|---|
| `frontend/trading-decision/package.json` | Add deps `react-router-dom@^7`. Add devDeps `vitest@^3`, `@testing-library/react@^16`, `@testing-library/jest-dom@^6`, `@testing-library/user-event@^14`, `jsdom@^26`. Add scripts `test` (`vitest run`) and `test:watch` (`vitest`). | Required for routing and tests. |
| `frontend/trading-decision/package-lock.json` | Regenerated by `npm install`. | Lockfile is source of truth (per ROB-6 plan §5.1). |
| `frontend/trading-decision/tsconfig.json` | Add `"types": ["vite/client", "vitest/globals"]` to `compilerOptions`. Include `src/__tests__` and `vitest.config.ts` references where appropriate. | Type Vitest globals + jest-dom matchers. |
| `frontend/trading-decision/src/api/client.ts` | Add a typed error class `ApiError(status, body)` and have `apiFetch` raise it. Keep current signature compatible. | UI needs to discriminate 401/404/422/409 from 5xx. |
| `.github/workflows/frontend-trading-decision.yml` | Add `- run: npm run test` step after typecheck. | Run vitest in CI. |

**No changes to:** any backend Python file, any Alembic migration, `Caddyfile`, `Dockerfile.api`, `docker-compose.*`, `Makefile` (existing `frontend-*` targets are sufficient — vitest is invoked via `npm run test` from CI/dev directly; if local convenience is wanted, a follow-up adds `frontend-test`).

---

## 5. API contract assumptions (verbatim from ROB-2)

The implementer **must** treat the following as the wire contract. Re-reading `app/routers/trading_decisions.py` and `app/schemas/trading_decisions.py` is the authoritative cross-check.

### 5.1 Endpoints used by the UI

| Method | Path | Used for | Sends | Returns |
|---|---|---|---|---|
| GET | `/trading/api/decisions?limit=50&offset=0[&status=...]` | Inbox | — | `SessionListResponse` |
| GET | `/trading/api/decisions/{session_uuid}` | Detail | — | `SessionDetail` |
| POST | `/trading/api/proposals/{proposal_uuid}/respond` | Per-proposal response | `ProposalRespondRequest` | `ProposalDetail` |

The UI **does not** call:
- `POST /trading/api/decisions` (sessions are created by analysts).
- `POST /trading/api/decisions/{session_uuid}/proposals` (same).
- `POST /trading/api/proposals/{proposal_uuid}/actions` (separate execution flow records actions).
- `POST /trading/api/proposals/{proposal_uuid}/counterfactuals` (analyst/research flow).
- `POST /trading/api/proposals/{proposal_uuid}/outcomes` (Prompt 5).

### 5.2 Wire types (mirror in `src/api/types.ts`, no transformation layer)

All `Decimal` fields arrive as JSON **strings** (Pydantic v2 default for `Decimal`). The UI keeps them as strings and only parses with `Number(...)` at the rendering site through helpers in `src/format/decimal.ts`. UUIDs and `datetime` fields are also strings.

```ts
// src/api/types.ts
export type Uuid = string;
export type IsoDateTime = string;
export type DecimalString = string;       // arrives as JSON string

export type SessionStatus = "open" | "closed" | "archived";
export type ProposalKind =
  | "trim" | "add" | "enter" | "exit"
  | "pullback_watch" | "breakout_watch"
  | "avoid" | "no_action" | "other";
export type Side = "buy" | "sell" | "none";
export type UserResponseValue =
  | "pending" | "accept" | "reject" | "modify" | "partial_accept" | "defer";
export type ActionKind =
  | "live_order" | "paper_order" | "watch_alert" | "no_action" | "manual_note";
export type TrackKind =
  | "accepted_live" | "accepted_paper"
  | "rejected_counterfactual" | "analyst_alternative" | "user_alternative";
export type OutcomeHorizon = "1h" | "4h" | "1d" | "3d" | "7d" | "final";
export type InstrumentType =
  | "equity_kr" | "equity_us" | "crypto" | "forex" | "index";

export interface SessionSummary {
  session_uuid: Uuid;
  source_profile: string;
  strategy_name: string | null;
  market_scope: string | null;
  status: SessionStatus;
  generated_at: IsoDateTime;
  created_at: IsoDateTime;
  updated_at: IsoDateTime;
  proposals_count: number;
  pending_count: number;
}

export interface SessionListResponse {
  sessions: SessionSummary[];
  total: number;
  limit: number;
  offset: number;
}

export interface ActionDetail {
  id: number;
  action_kind: ActionKind;
  external_order_id: string | null;
  external_paper_id: string | null;
  external_watch_id: string | null;
  external_source: string | null;
  payload_snapshot: Record<string, unknown>;
  recorded_at: IsoDateTime;
  created_at: IsoDateTime;
}

export interface CounterfactualDetail {
  id: number;
  track_kind: TrackKind;
  baseline_price: DecimalString;
  baseline_at: IsoDateTime;
  quantity: DecimalString | null;
  payload: Record<string, unknown>;
  notes: string | null;
  created_at: IsoDateTime;
}

export interface OutcomeDetail {                       // shown only in raw count; not rendered (Prompt 5)
  id: number;
  counterfactual_id: number | null;
  track_kind: TrackKind;
  horizon: OutcomeHorizon;
  price_at_mark: DecimalString;
  pnl_pct: DecimalString | null;
  pnl_amount: DecimalString | null;
  marked_at: IsoDateTime;
  payload: Record<string, unknown> | null;
  created_at: IsoDateTime;
}

export interface ProposalDetail {
  proposal_uuid: Uuid;
  symbol: string;
  instrument_type: InstrumentType;
  proposal_kind: ProposalKind;
  side: Side;
  user_response: UserResponseValue;
  responded_at: IsoDateTime | null;
  created_at: IsoDateTime;
  updated_at: IsoDateTime;
  original_quantity: DecimalString | null;
  original_quantity_pct: DecimalString | null;
  original_amount: DecimalString | null;
  original_price: DecimalString | null;
  original_trigger_price: DecimalString | null;
  original_threshold_pct: DecimalString | null;
  original_currency: string | null;
  original_rationale: string | null;
  original_payload: Record<string, unknown>;
  user_quantity: DecimalString | null;
  user_quantity_pct: DecimalString | null;
  user_amount: DecimalString | null;
  user_price: DecimalString | null;
  user_trigger_price: DecimalString | null;
  user_threshold_pct: DecimalString | null;
  user_note: string | null;
  actions: ActionDetail[];
  counterfactuals: CounterfactualDetail[];
  outcomes: OutcomeDetail[];
}

export interface SessionDetail extends SessionSummary {
  market_brief: Record<string, unknown> | null;
  notes: string | null;
  proposals: ProposalDetail[];
}

export type RespondAction = Exclude<UserResponseValue, "pending">;

export interface ProposalRespondRequest {
  response: RespondAction;
  user_quantity?: DecimalString | null;
  user_quantity_pct?: DecimalString | null;
  user_amount?: DecimalString | null;
  user_price?: DecimalString | null;
  user_trigger_price?: DecimalString | null;
  user_threshold_pct?: DecimalString | null;
  user_note?: string | null;
}
```

### 5.3 Authentication

The API uses cookie-based session auth via `AuthMiddleware` + `get_authenticated_user`. The SPA is served same-origin (`/trading/decisions/...`) and the API lives at `/trading/api/...` — `apiFetch` already sets `credentials: "same-origin"`. **On 401 the UI must redirect the user to `/login` (the existing Jinja login page).** Add `window.location.assign("/login?next=" + encodeURIComponent(window.location.pathname))` in the `ApiError` 401 branch in `useDecisionInbox` / `useDecisionSession`.

### 5.4 Error mapping

| HTTP | UI behavior |
|---|---|
| 200/201 | render data |
| 401 | redirect to `/login?next=...` |
| 404 | render "Session not found" / "Proposal not found" with a "Back to inbox" button |
| 409 | inline banner: "Session is archived. You can no longer respond." (only on `respond`) |
| 422 | inline form error from `detail` (Pydantic) — keep editor open, do not clear inputs |
| 5xx | inline banner: "Something went wrong. Try again." Show retry button |

### 5.5 Decimal handling rules (must)

1. UI never converts a `DecimalString` to a JS `number` for storage. It is `string` end-to-end.
2. `format/decimal.ts:formatDecimal(s, locale, opts)` parses **once** at render time using `Intl.NumberFormat`. It returns the original string verbatim if `Number.isFinite(Number(s))` is `false` (defensive — should never happen with valid API output).
3. The adjustment editor uses `<input type="text" inputMode="decimal" pattern="[0-9.\-]*">` — not `type="number"`. On submit it validates via `^-?\d+(\.\d+)?$` and forwards the string. This avoids the float-precision regression that motivated string-Decimals server-side.

---

## 6. UI component / state design

### 6.1 Routing (`src/routes.tsx`)

```tsx
import { createBrowserRouter } from "react-router-dom";
import SessionListPage from "./pages/SessionListPage";
import SessionDetailPage from "./pages/SessionDetailPage";

export const router = createBrowserRouter(
  [
    { path: "/", element: <SessionListPage /> },
    { path: "/sessions/:sessionUuid", element: <SessionDetailPage /> },
    { path: "*", element: <SessionListPage /> },
  ],
  { basename: "/trading/decisions" },
);
```

Why `basename`: Vite's `base: "/trading/decisions/"` already prefixes asset URLs. React Router needs the same `basename` so `<Link to="/">` becomes `/trading/decisions/`.

### 6.2 Hooks

```ts
// src/hooks/useDecisionInbox.ts
export interface InboxState {
  status: "idle" | "loading" | "success" | "error";
  data: SessionListResponse | null;
  error: string | null;
}

export function useDecisionInbox(args: {
  limit: number;
  offset: number;
  statusFilter?: SessionStatus;
}): InboxState & { refetch: () => void };
```

```ts
// src/hooks/useDecisionSession.ts
export interface SessionState {
  status: "idle" | "loading" | "success" | "error";
  data: SessionDetail | null;
  error: string | null;
}

export function useDecisionSession(sessionUuid: string): SessionState & {
  refetch: () => void;
  respond: (proposalUuid: string, body: ProposalRespondRequest) => Promise<void>;
};
```

`respond` calls the API; on success it **refetches** the full session (not optimistic — keeps UI and server in sync; one extra round-trip per click is fine; we can revisit later). On `ApiError` it surfaces the message via a return value (`Promise<{ ok: true } | { ok: false; status: number; detail: string }>`) consumed by the editor.

### 6.3 Pages

**`SessionListPage`**
- Fetches via `useDecisionInbox({ limit: 50, offset: 0 })`.
- Top bar: title "Decision inbox", `Refresh` button (calls `refetch`).
- Status filter `<select>`: All | open | closed | archived. Changes refetch.
- Table:
  | Generated | Profile | Strategy | Scope | Status | Proposals | Pending |
  - Row → click opens `<Link to={`/sessions/${session_uuid}`}>`.
  - Empty state: "No decision sessions yet."
- Pagination: prev/next buttons that mutate `offset`. (Page size fixed at 50 for now.)

**`SessionDetailPage`**
- Reads `:sessionUuid` route param.
- Fetches via `useDecisionSession(sessionUuid)`.
- Header: `<Link to="/">Back to inbox</Link>`, `<h1>{strategy_name ?? source_profile}</h1>`, status badge, generated_at.
- `<MarketBriefPanel brief={data.market_brief} notes={data.notes} />` — collapsible JSON viewer (default closed if non-empty, hidden if `null`).
- `<section>` with one `<ProposalRow>` per proposal in `data.proposals`.
- Footer: counts ("M of N pending").

### 6.4 ProposalRow

Always shown:
- Header line: symbol (bold) · `Side` badge (buy/sell/none) · `Kind` chip · response badge.
- Two-column "Original" panel showing only those `original_*` fields that are non-null. Currency suffix from `original_currency`.
- If `user_response !== "pending"`:
  - "Your decision" panel showing `user_response`, `responded_at`, and any `user_*` fields that are non-null. `OriginalVsAdjustedSummary` displays paired values where both sides are present (e.g., `original_quantity_pct: 20%` → `user_quantity_pct: 10%`).
- `original_rationale` shown as a small italic block under "Original".
- Below: `<ProposalResponseControls>` (always visible — re-responding overwrites per ROB-2 §7.5).
- Below: `<LinkedActionsPanel actions={proposal.actions} counterfactuals={proposal.counterfactuals} />` (read-only — never editable; outcomes deliberately not shown).

### 6.5 ProposalResponseControls

Five buttons in a row: `Accept`, `Partial accept`, `Modify`, `Defer`, `Reject`. Disabled while a request is in flight. Behavior:

- `Accept` and `Defer` and `Reject`: open a small confirm dialog (single line + an optional `user_note`) and POST `{ response, user_note? }`. No `user_*` numeric fields.
- `Partial accept` and `Modify`: open `<ProposalAdjustmentEditor>` inline (toggles a panel below the button row).

The button for `proposal.user_response` is highlighted as the current selection.

### 6.6 ProposalAdjustmentEditor

A small form with these fields, each shown only if the corresponding `original_*` is present (so users only see editable fields for what the analyst quantified):

| Field | Type | Validation |
|---|---|---|
| `user_quantity` | text/decimal | `^\d+(\.\d+)?$`, optional |
| `user_quantity_pct` | text/decimal | `0..100`, optional |
| `user_amount` | text/decimal | `>= 0`, optional |
| `user_price` | text/decimal | `>= 0`, optional |
| `user_trigger_price` | text/decimal | `>= 0`, optional |
| `user_threshold_pct` | text/decimal | `0..100`, optional |
| `user_note` | textarea, max 4000 | always shown, optional |

Placeholder per input is the corresponding `original_*` value (e.g., placeholder `"20"` for `user_quantity_pct` when `original_quantity_pct === "20"`).

Submit button label is the chosen response: `Save modify` or `Save partial accept`. Submit:
1. Validate at least one numeric field is set (mirrors server `_modify_requires_some_user_field`).
2. Build `ProposalRespondRequest` with only filled fields (omitted, not `null`, when blank).
3. Call `respond(...)`.
4. On 422, parse `detail` and show inline error.

Cancel button restores the proposal row to its prior state without sending.

### 6.7 OriginalVsAdjustedSummary

Tiny presentational component:
```tsx
<dl>
  {pairs.map(({ label, original, user }) => (
    <div key={label}>
      <dt>{label}</dt>
      <dd>
        <span className="orig">{original ?? "—"}</span>
        {" → "}
        <span className="user">{user ?? "(unchanged)"}</span>
      </dd>
    </div>
  ))}
</dl>
```

Pairs are computed in the row by walking the six `original_*`/`user_*` columns plus currency.

### 6.8 LinkedActionsPanel

Read-only. For each `ActionDetail`:
- Header: `action_kind` · `external_source` · `recorded_at`
- One bold field: whichever external id is set
  - `live_order` → `external_order_id`
  - `paper_order` → `external_paper_id`
  - `watch_alert` → `external_watch_id`
  - `no_action`/`manual_note` → "(no external id)"
- `payload_snapshot` rendered as `<pre>` with collapsible toggle.

For each `CounterfactualDetail`: kind, baseline_price + baseline_at, quantity (if any), notes (if any). No outcome rendering.

If both lists are empty: render "No linked actions yet."

### 6.9 Styling

- Plain CSS via `*.module.css`. No Tailwind/PostCSS plugins.
- Base font/color picked up from existing `App.css` style. Buttons get a single shared style in `App.css` (`.btn`, `.btn-primary`, `.btn-ghost`, `.btn-danger`, `.btn-warn`).
- Status badge palette:
  - session: open=green, closed=grey, archived=dark grey
  - response: pending=amber, accept=green, partial_accept=teal, modify=blue, defer=grey, reject=red

---

## 7. Test strategy

We use **Vitest** (matches Vite, no extra config) + **@testing-library/react** + **jsdom**. Tests live in `src/__tests__/`. The CI workflow runs them.

### 7.1 Vitest config (`vitest.config.ts`)

```ts
import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./src/test/setup.ts"],
    css: true,
    include: ["src/__tests__/**/*.test.{ts,tsx}"],
  },
});
```

`src/test/setup.ts`:
```ts
import "@testing-library/jest-dom/vitest";
import { afterEach } from "vitest";
import { cleanup } from "@testing-library/react";

afterEach(() => cleanup());
```

`src/test/server.ts` provides a `mockFetch(routes: Record<string, (req: Request) => Response | Promise<Response>>)` helper that replaces `globalThis.fetch` for one test. We avoid MSW to keep the dependency footprint small.

`src/test/fixtures.ts` returns canned `SessionListResponse`, `SessionDetail`, `ProposalDetail` with the BTC/ETH/SOL scenario from the roadmap (BTC 20% trim, ETH pullback_watch, SOL pullback_watch).

### 7.2 Test cases (mirrors Prompt 4 acceptance)

| Suite | Cases |
|---|---|
| `format.decimal.test.ts` | (a) `formatDecimal("117800000", "ko-KR")` → `"117,800,000"`. (b) `formatDecimal("0.05")` → `"0.05"`. (c) returns input verbatim on `"abc"`. |
| `api.decisions.test.ts` | (a) `getDecisions` builds `?limit&offset&status` correctly. (b) `getSession` calls `/decisions/{uuid}`. (c) `respondToProposal` POSTs body, parses `ProposalDetail`. (d) 401 surfaces `ApiError` with `status:401`. (e) 422 returns parsed `detail`. |
| `ProposalRow.test.tsx` | (a) Pending: shows original block, no "Your decision" block. (b) After `accept`: shows green badge and `responded_at`. (c) Modify scenario: original 20%, user 10% — `OriginalVsAdjustedSummary` shows both. (d) `LinkedActionsPanel` rendered with one `live_order` action when present. |
| `ProposalResponseControls.test.tsx` | (a) Five buttons rendered. (b) Click `Accept` calls `respond({response:"accept"})`. (c) Click `Modify` opens editor (assert editor visible). (d) Buttons disabled while in-flight. |
| `ProposalAdjustmentEditor.test.tsx` | (a) Numeric fields shown only for present `original_*`. (b) Placeholders mirror originals. (c) Submit with empty fields shows validation error and does NOT call respond. (d) Submit with `user_quantity_pct=10` for `modify` calls respond with that exact string body. (e) 422 from server keeps editor open and shows server detail. |
| `LinkedActionsPanel.test.tsx` | (a) `live_order` row shows `external_order_id` bold. (b) `watch_alert` row shows `external_watch_id`. (c) Empty case renders "No linked actions yet." (d) outcome list is NOT rendered (assert no element with data-testid `outcome-row`). |
| `SessionListPage.test.tsx` | (a) Empty state when `sessions` is `[]`. (b) Populated table shows row per session, with proposals_count and pending_count. (c) Status filter dropdown change triggers refetch. (d) Click row navigates to `/sessions/{uuid}`. |
| `SessionDetailPage.test.tsx` | (a) Shows `<MarketBriefPanel>` when `market_brief` non-null. (b) Renders one `ProposalRow` per `proposals` entry. (c) Successful `respond` triggers refetch and updates the row. (d) 404 from API renders "Session not found". (e) Archived session: `respond` returns 409 → banner "Session is archived". |

### 7.3 Verification commands

```bash
# from repo root
make frontend-install                                    # one-time
cd frontend/trading-decision
npm run typecheck                                        # tsc strict, both projects
npm run test                                             # vitest run, all suites
npm run build                                            # ensures the bundle still builds with new code

# back at repo root, sanity that Python tests still pass (no backend changes)
uv run pytest tests/test_trading_decisions_router.py tests/test_trading_decisions_router_safety.py tests/test_trading_decisions_spa_router.py tests/test_trading_decisions_spa_router_safety.py -q

# end-to-end smoke (manual)
make dev &                                               # FastAPI on :8000
make frontend-dev                                        # Vite dev server on :5173
# open http://localhost:5173/trading/decisions/ — log in via /login if prompted
# verify inbox renders, click a session, exercise accept / modify / defer / reject
```

---

## 8. Acceptance checklist (used at PR review time)

- [ ] `/trading/decisions/` renders an inbox of decision sessions for the logged-in user.
- [ ] Clicking a session opens `/trading/decisions/sessions/{uuid}` and renders proposals.
- [ ] Each proposal row shows the analyst original (every non-null `original_*` field).
- [ ] Each proposal row exposes accept / partial_accept / modify / defer / reject controls.
- [ ] `accept`, `defer`, `reject` POST `{response}` (+ optional `user_note`) and refresh the row.
- [ ] `modify` and `partial_accept` open an inline editor that submits at least one `user_*` numeric field (server-side validator mirrored client-side).
- [ ] After a `modify` from `original_quantity_pct=20` to `user_quantity_pct=10`, the row shows BOTH values via `OriginalVsAdjustedSummary`.
- [ ] List-style multi-response works: accepting BTC, accepting ETH, deferring SOL leaves three independent updated rows.
- [ ] Linked actions panel renders `live_order` / `paper_order` / `watch_alert` / `no_action` / `manual_note` correctly when present, and "No linked actions yet." when empty. Outcomes are **never** rendered.
- [ ] No backend Python file is modified by this PR.
- [ ] No new direct browser fetch to KIS / Upbit / Telegram / external services. UI talks only to `/trading/api/*` (verified by §10 grep).
- [ ] On 401 the UI redirects to `/login?next=...`. On 404 it shows a friendly not-found page with a back link. On 409 to `/respond` it shows a banner.
- [ ] `npm run typecheck && npm run test && npm run build` are all green.
- [ ] CI workflow `frontend-trading-decision.yml` runs `npm run test` step.
- [ ] All ROB-2 router tests still pass (`uv run pytest tests/test_trading_decisions_router*.py`).
- [ ] `git diff --stat origin/main...HEAD` shows changes only under `frontend/trading-decision/**`, `.github/workflows/frontend-trading-decision.yml`, and `docs/plans/ROB-7-*`.

---

## 9. Safety constraints (hard)

These are non-negotiable. The implementer must stop and ask before relaxing any of them.

1. **No live execution.** The UI must not fetch any URL outside `/trading/api/*`. No `kis`, no `upbit`, no `webhooks`, no third-party SDK at runtime.
2. **No secrets.** Do not read or log API keys, tokens, cookies, or auth headers. The same-origin cookie is sent automatically by the browser; we never inspect it.
3. **Outcomes are out of scope.** Do not render outcome marks. Do not call `POST /outcomes`. (Prompt 5.)
4. **Counterfactuals are read-only.** Do not call `POST /counterfactuals`.
5. **Actions are read-only.** Do not call `POST /actions`. The UI never creates an `ActionDetail`. (Actions are written by a separate execution flow that already exists in the backend.)
6. **No backend changes.** Do not touch any file under `app/`, `alembic/`, `tests/`, `scripts/`. If the implementer thinks the API needs a change, stop and surface it.
7. **No Hermes routing.** Do not branch UI behavior on `source_profile` or `strategy_name` beyond display.
8. **No environment variable reads at runtime.** All config is the URL prefix `/trading/api`. No `.env` lookup in the bundle. (`import.meta.env` may only be used for `MODE` to gate test-only code paths.)

A test (`api.decisions.test.ts:test_only_calls_trading_api_paths`) asserts every `apiFetch` URL we issue starts with `/trading/api/`.

---

## 10. Forbidden-import / forbidden-call boundary

**At runtime in the bundle**, these substrings must not appear (case-insensitive) outside test fixtures and comments. The implementer adds a small grep in CI:

```text
kis. | upbit. | redis | telegram | broker | order_service |
fill_notification | execution_event | watch_alert_service
```

A unit test in `api.decisions.test.ts` (`test_no_forbidden_calls_in_built_bundle`) reads `frontend/trading-decision/dist/assets/*.js` (when present, gated behind `if (process.env.RUN_BUNDLE_GREP === "1")`) and fails if it finds any forbidden token. CI runs `npm run build && RUN_BUNDLE_GREP=1 npm run test` so this guard is exercised.

If the bundle grep is too flaky during dev, drop it from CI and rely on the fact that no source file imports any such module — but keep the runtime URL guard from §9.

---

## 11. Frontend-package.json delta

Add (final pinned versions are decided by `npm install` at implementation time; the caret ranges below are guidance):

```json
{
  "scripts": {
    "test": "vitest run",
    "test:watch": "vitest"
  },
  "dependencies": {
    "react-router-dom": "^7.1.0"
  },
  "devDependencies": {
    "@testing-library/jest-dom": "^6.6.0",
    "@testing-library/react": "^16.1.0",
    "@testing-library/user-event": "^14.5.0",
    "jsdom": "^26.0.0",
    "vitest": "^3.0.0"
  }
}
```

> The implementer should run `npm install <packages>@latest` (each on its own line; do not auto-bump existing deps), then commit `package.json` + `package-lock.json` together. Use `npm install --save-exact` if the lockfile resolves a beta — pin to the latest stable.

---

## 12. Step-by-step implementation tasks (TDD, frequent commits)

Each task is a single commit. Run the verification commands at the end of each task; do not move forward if any are red.

### Task 1: Add deps + Vitest scaffold (no UI yet)

**Files:**
- Modify: `frontend/trading-decision/package.json`
- Modify: `frontend/trading-decision/package-lock.json`
- Modify: `frontend/trading-decision/tsconfig.json`
- Create: `frontend/trading-decision/vitest.config.ts`
- Create: `frontend/trading-decision/src/test/setup.ts`
- Create: `frontend/trading-decision/src/__tests__/.gitkeep`
- Create: `frontend/trading-decision/src/__tests__/sanity.test.ts`

- [ ] **Step 1: Install deps**
  Run from `frontend/trading-decision/`:
  ```bash
  npm install --save react-router-dom@latest
  npm install --save-dev vitest@latest @testing-library/react@latest @testing-library/jest-dom@latest @testing-library/user-event@latest jsdom@latest
  ```

- [ ] **Step 2: Add scripts and types**

  Edit `package.json`:
  ```json
  {
    "scripts": {
      "dev": "vite",
      "build": "tsc -p tsconfig.json --noEmit && tsc -p tsconfig.node.json --noEmit && vite build",
      "preview": "vite preview",
      "typecheck": "tsc -p tsconfig.json --noEmit && tsc -p tsconfig.node.json --noEmit",
      "test": "vitest run",
      "test:watch": "vitest"
    }
  }
  ```

  Edit `tsconfig.json` `compilerOptions`:
  ```json
  "types": ["vite/client", "vitest/globals"],
  ```
  And ensure `"include"` covers both `"src"` and `"src/__tests__"`.

- [ ] **Step 3: Create `vitest.config.ts` and `src/test/setup.ts`** exactly as in §7.1.

- [ ] **Step 4: Create one sanity test**

  `src/__tests__/sanity.test.ts`:
  ```ts
  import { describe, it, expect } from "vitest";

  describe("vitest sanity", () => {
    it("runs", () => {
      expect(1 + 1).toBe(2);
    });
  });
  ```

- [ ] **Step 5: Run**
  ```bash
  npm run typecheck   # PASS
  npm run test        # PASS, 1 test
  npm run build       # PASS
  ```

- [ ] **Step 6: Commit**
  ```bash
  git add frontend/trading-decision/package.json \
          frontend/trading-decision/package-lock.json \
          frontend/trading-decision/tsconfig.json \
          frontend/trading-decision/vitest.config.ts \
          frontend/trading-decision/src/test/setup.ts \
          frontend/trading-decision/src/__tests__/sanity.test.ts \
          frontend/trading-decision/src/__tests__/.gitkeep
  git commit -m "chore(rob-7): add react-router and vitest scaffolding"
  ```

### Task 2: Wire CI to run vitest

**Files:**
- Modify: `.github/workflows/frontend-trading-decision.yml`

- [ ] **Step 1: Add `npm run test` step** between `npm run typecheck` and `npm run build`:
  ```yaml
        - run: npm run test
  ```

- [ ] **Step 2: Push branch** and confirm the workflow goes green on CI for the new commit.

  *(If the implementer cannot push yet, leave a TODO: verify after PR is opened.)*

- [ ] **Step 3: Commit**
  ```bash
  git commit -am "ci(rob-7): run vitest on PR"
  ```

### Task 3: API types + ApiError + decision client

**Files:**
- Modify: `frontend/trading-decision/src/api/client.ts`
- Create: `frontend/trading-decision/src/api/types.ts`
- Create: `frontend/trading-decision/src/api/decisions.ts`
- Create: `frontend/trading-decision/src/test/server.ts`
- Create: `frontend/trading-decision/src/__tests__/api.decisions.test.ts`

- [ ] **Step 1: Write failing tests** in `api.decisions.test.ts`:
  ```ts
  import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
  import { mockFetch } from "../test/server";
  import { ApiError } from "../api/client";
  import { getDecisions, getSession, respondToProposal } from "../api/decisions";

  describe("decisions API client", () => {
    afterEach(() => vi.unstubAllGlobals());

    it("getDecisions builds query string", async () => {
      const { calls } = mockFetch({
        "/trading/api/decisions?limit=25&offset=50&status=open":
          () => new Response(JSON.stringify({ sessions: [], total: 0, limit: 25, offset: 50 })),
      });
      await getDecisions({ limit: 25, offset: 50, status: "open" });
      expect(calls[0]?.url).toContain("limit=25&offset=50&status=open");
    });

    it("getSession hits /decisions/{uuid}", async () => {
      mockFetch({
        "/trading/api/decisions/abc-123":
          () => new Response(JSON.stringify({ session_uuid: "abc-123", proposals: [] /* trimmed */ })),
      });
      const data = await getSession("abc-123");
      expect(data.session_uuid).toBe("abc-123");
    });

    it("respondToProposal POSTs body and parses ProposalDetail", async () => {
      const { calls } = mockFetch({
        "/trading/api/proposals/p-1/respond":
          (req) => new Response(JSON.stringify({ proposal_uuid: "p-1", user_response: "modify" /* trimmed */ })),
      });
      const result = await respondToProposal("p-1", { response: "modify", user_quantity_pct: "10" });
      expect(result.user_response).toBe("modify");
      expect(calls[0]?.method).toBe("POST");
    });

    it("401 throws ApiError(401, AUTH_REQUIRED)", async () => {
      mockFetch({
        "/trading/api/decisions": () => new Response(JSON.stringify({ detail: "auth required" }), { status: 401 }),
      });
      await expect(getDecisions({ limit: 50, offset: 0 })).rejects.toMatchObject({
        status: 401,
      });
    });

    it("422 surfaces detail string", async () => {
      mockFetch({
        "/trading/api/proposals/p-1/respond":
          () => new Response(JSON.stringify({ detail: "modify/partial_accept requires at least one user_* numeric field" }), { status: 422 }),
      });
      await expect(respondToProposal("p-1", { response: "modify" })).rejects.toMatchObject({
        status: 422,
      });
    });

    it("only calls /trading/api paths", async () => {
      const { calls } = mockFetch({
        "/trading/api/decisions": () => new Response(JSON.stringify({ sessions: [], total: 0, limit: 50, offset: 0 })),
      });
      await getDecisions({ limit: 50, offset: 0 });
      for (const call of calls) {
        expect(new URL(call.url, "http://x").pathname.startsWith("/trading/api/")).toBe(true);
      }
    });
  });
  ```

- [ ] **Step 2: Implement `mockFetch` helper** in `src/test/server.ts`:
  ```ts
  import { vi } from "vitest";

  export interface RecordedCall { url: string; method: string; body?: string }

  export function mockFetch(
    routes: Record<string, (req: Request) => Response | Promise<Response>>,
  ): { calls: RecordedCall[] } {
    const calls: RecordedCall[] = [];
    const handler = async (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
      const url = typeof input === "string" ? input : input instanceof URL ? input.toString() : input.url;
      const method = (init?.method ?? (input instanceof Request ? input.method : "GET")).toUpperCase();
      const bodyStr = typeof init?.body === "string" ? init.body : undefined;
      calls.push({ url, method, body: bodyStr });
      const path = new URL(url, "http://x").pathname + (new URL(url, "http://x").search || "");
      const route = routes[path] ?? routes[new URL(url, "http://x").pathname];
      if (!route) {
        return new Response("no route", { status: 599 });
      }
      const req = new Request(url, init as RequestInit);
      return route(req);
    };
    vi.stubGlobal("fetch", handler);
    return { calls };
  }
  ```

- [ ] **Step 3: Implement `src/api/types.ts`** verbatim per §5.2.

- [ ] **Step 4: Update `src/api/client.ts`** to throw `ApiError`:
  ```ts
  const API_BASE = "/trading/api";

  export class ApiError extends Error {
    constructor(public readonly status: number, public readonly detail: string, public readonly body: unknown) {
      super(`API ${status}: ${detail}`);
    }
  }

  export async function apiFetch<T>(path: string, init?: RequestInit): Promise<T> {
    const res = await fetch(`${API_BASE}${path}`, {
      credentials: "same-origin",
      headers: { "Content-Type": "application/json", ...(init?.headers ?? {}) },
      ...init,
    });
    if (!res.ok) {
      let body: unknown = null;
      let detail = `${res.status} ${res.statusText}`;
      try { body = await res.json(); detail = (body as { detail?: string })?.detail ?? detail; } catch {}
      throw new ApiError(res.status, detail, body);
    }
    return (await res.json()) as T;
  }
  ```

- [ ] **Step 5: Implement `src/api/decisions.ts`**:
  ```ts
  import { apiFetch } from "./client";
  import type {
    SessionListResponse, SessionDetail, ProposalDetail,
    ProposalRespondRequest, SessionStatus,
  } from "./types";

  export async function getDecisions(args: {
    limit: number; offset: number; status?: SessionStatus;
  }): Promise<SessionListResponse> {
    const params = new URLSearchParams();
    params.set("limit", String(args.limit));
    params.set("offset", String(args.offset));
    if (args.status) params.set("status", args.status);
    return apiFetch<SessionListResponse>(`/decisions?${params.toString()}`);
  }

  export async function getSession(sessionUuid: string): Promise<SessionDetail> {
    return apiFetch<SessionDetail>(`/decisions/${encodeURIComponent(sessionUuid)}`);
  }

  export async function respondToProposal(
    proposalUuid: string,
    body: ProposalRespondRequest,
  ): Promise<ProposalDetail> {
    return apiFetch<ProposalDetail>(`/proposals/${encodeURIComponent(proposalUuid)}/respond`, {
      method: "POST",
      body: JSON.stringify(body),
    });
  }
  ```

- [ ] **Step 6: Run** `npm run typecheck && npm run test`. All green.

- [ ] **Step 7: Commit**
  ```bash
  git commit -am "feat(rob-7): typed decisions API client with ApiError"
  ```

### Task 4: Decimal + datetime formatters

**Files:**
- Create: `frontend/trading-decision/src/format/decimal.ts`
- Create: `frontend/trading-decision/src/format/datetime.ts`
- Create: `frontend/trading-decision/src/__tests__/format.decimal.test.ts`

- [ ] **Step 1: Failing tests**
  ```ts
  import { describe, it, expect } from "vitest";
  import { formatDecimal } from "../format/decimal";

  describe("formatDecimal", () => {
    it("formats large integer with locale grouping", () => {
      expect(formatDecimal("117800000", "ko-KR")).toBe("117,800,000");
    });
    it("preserves fractional part", () => {
      expect(formatDecimal("0.05", "en-US", { maximumFractionDigits: 8 })).toBe("0.05");
    });
    it("returns input verbatim when not finite", () => {
      expect(formatDecimal("abc")).toBe("abc");
    });
    it("handles null/undefined as em dash", () => {
      expect(formatDecimal(null)).toBe("—");
    });
  });
  ```

- [ ] **Step 2: Implement**
  ```ts
  // src/format/decimal.ts
  export function formatDecimal(
    s: string | null | undefined,
    locale: string = "en-US",
    opts: Intl.NumberFormatOptions = { maximumFractionDigits: 8 },
  ): string {
    if (s === null || s === undefined) return "—";
    const n = Number(s);
    if (!Number.isFinite(n)) return s;
    return new Intl.NumberFormat(locale, opts).format(n);
  }
  ```

- [ ] **Step 3: Implement `src/format/datetime.ts`**
  ```ts
  export function formatDateTime(iso: string | null | undefined, locale: string = "en-US"): string {
    if (!iso) return "—";
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return iso;
    return d.toLocaleString(locale, { dateStyle: "medium", timeStyle: "short" });
  }
  ```

- [ ] **Step 4: Run** `npm run typecheck && npm run test`. Green.

- [ ] **Step 5: Commit**
  ```bash
  git commit -am "feat(rob-7): decimal/datetime formatters"
  ```

### Task 5: Fixtures + LoadingView + ErrorView

**Files:**
- Create: `frontend/trading-decision/src/test/fixtures.ts` — BTC 20% trim, ETH pullback_watch, SOL pullback_watch.
- Create: `frontend/trading-decision/src/components/LoadingView.tsx`
- Create: `frontend/trading-decision/src/components/ErrorView.tsx`

- [ ] **Step 1: Write the fixture** with three proposals; user_response = "pending" on all; one BTC has a `live_order` action attached.

- [ ] **Step 2: Implement `LoadingView` and `ErrorView`** as 5-line presentational components.

- [ ] **Step 3: No tests yet** (covered in later tasks).

- [ ] **Step 4: Commit**
  ```bash
  git commit -am "feat(rob-7): fixtures and loading/error placeholders"
  ```

### Task 6: ProposalResponseControls

**Files:**
- Create: `frontend/trading-decision/src/components/ProposalResponseControls.tsx`
- Create: `frontend/trading-decision/src/__tests__/ProposalResponseControls.test.tsx`

- [ ] **Step 1: Failing tests** per §7.2 row.

- [ ] **Step 2: Implement** the component with five buttons. `onAccept`, `onDefer`, `onReject` each call a single prop `onSimpleResponse(response)`. `onModify`/`onPartialAccept` call `onOpenAdjust(response)` so the parent can mount the editor.

- [ ] **Step 3: Run** typecheck + tests. Green.

- [ ] **Step 4: Commit**

### Task 7: ProposalAdjustmentEditor

**Files:**
- Create: `frontend/trading-decision/src/components/ProposalAdjustmentEditor.tsx`
- Create: `frontend/trading-decision/src/components/ProposalAdjustmentEditor.module.css`
- Create: `frontend/trading-decision/src/__tests__/ProposalAdjustmentEditor.test.tsx`

- [ ] **Step 1: Failing tests** per §7.2.

- [ ] **Step 2: Implement** the editor:
  - Show numeric inputs only for `original_*` fields that are non-null.
  - Validate `^-?\d+(\.\d+)?$` per field on submit.
  - Validate at least one numeric field set (mirrors server).
  - Show `user_note` textarea (always).
  - Submit calls a prop `onSubmit(body: ProposalRespondRequest): Promise<{ ok: boolean; detail?: string }>`.
  - On `{ ok: false, detail }` keep editor open and show inline error.

- [ ] **Step 3: Commit**

### Task 8: OriginalVsAdjustedSummary + LinkedActionsPanel

**Files:**
- Create: `frontend/trading-decision/src/components/OriginalVsAdjustedSummary.tsx`
- Create: `frontend/trading-decision/src/components/LinkedActionsPanel.tsx`
- Create: `frontend/trading-decision/src/components/LinkedActionsPanel.module.css`
- Create: `frontend/trading-decision/src/__tests__/LinkedActionsPanel.test.tsx`

- [ ] **Step 1: Failing tests** per §7.2 row.

- [ ] **Step 2: Implement** both components. `LinkedActionsPanel` MUST NOT render any element with `data-testid="outcome-row"`; the test asserts this absence.

- [ ] **Step 3: Commit**

### Task 9: ProposalRow

**Files:**
- Create: `frontend/trading-decision/src/components/ProposalRow.tsx`
- Create: `frontend/trading-decision/src/components/ProposalRow.module.css`
- Create: `frontend/trading-decision/src/components/StatusBadge.tsx`
- Create: `frontend/trading-decision/src/components/StatusBadge.module.css`
- Create: `frontend/trading-decision/src/__tests__/ProposalRow.test.tsx`

- [ ] **Step 1: Failing tests** per §7.2.

- [ ] **Step 2: Implement** ProposalRow combining Original block + ResponseControls + (conditional) AdjustmentEditor + OriginalVsAdjustedSummary + LinkedActionsPanel.

- [ ] **Step 3: Commit**

### Task 10: useDecisionInbox + SessionListPage

**Files:**
- Create: `frontend/trading-decision/src/hooks/useDecisionInbox.ts`
- Create: `frontend/trading-decision/src/pages/SessionListPage.tsx`
- Create: `frontend/trading-decision/src/pages/SessionListPage.module.css`
- Create: `frontend/trading-decision/src/__tests__/SessionListPage.test.tsx`

- [ ] **Step 1: Failing tests** per §7.2.

- [ ] **Step 2: Implement hook**: `useDecisionInbox` uses `useEffect` + `AbortController` + `useState` for `{status, data, error}`. On 401 it calls `window.location.assign` (guard with `if (typeof window !== "undefined")`).

- [ ] **Step 3: Implement page**: top-level component with status filter, refresh, table, and prev/next pagination.

- [ ] **Step 4: Commit**

### Task 11: useDecisionSession + SessionDetailPage + MarketBriefPanel

**Files:**
- Create: `frontend/trading-decision/src/hooks/useDecisionSession.ts`
- Create: `frontend/trading-decision/src/pages/SessionDetailPage.tsx`
- Create: `frontend/trading-decision/src/pages/SessionDetailPage.module.css`
- Create: `frontend/trading-decision/src/components/MarketBriefPanel.tsx`
- Create: `frontend/trading-decision/src/components/MarketBriefPanel.module.css`
- Create: `frontend/trading-decision/src/__tests__/SessionDetailPage.test.tsx`

- [ ] **Step 1: Failing tests** per §7.2.

- [ ] **Step 2: Implement hook**: includes `respond(proposalUuid, body)` returning `{ ok }` and refetching on success.

- [ ] **Step 3: Implement page**: header + MarketBriefPanel + list of ProposalRow + footer.

- [ ] **Step 4: Implement MarketBriefPanel**: `<details>` with summary "Market brief" — pretty-printed JSON inside `<pre>`. Hidden when `market_brief == null && notes == null`.

- [ ] **Step 5: Commit**

### Task 12: Wire router + replace App.tsx + drop placeholders

**Files:**
- Modify: `frontend/trading-decision/src/main.tsx`
- Modify: `frontend/trading-decision/src/App.tsx`
- Modify: `frontend/trading-decision/src/App.css`
- Create: `frontend/trading-decision/src/routes.tsx`
- Delete: `frontend/trading-decision/src/components/HelloDecision.tsx`
- Delete: `frontend/trading-decision/src/pages/HelloPage.tsx`

- [ ] **Step 1: Implement `routes.tsx`** per §6.1.

- [ ] **Step 2: Update `App.tsx`** to render `<RouterProvider router={router}/>`:
  ```tsx
  import { RouterProvider } from "react-router-dom";
  import { router } from "./routes";

  export default function App() {
    return <RouterProvider router={router} />;
  }
  ```

- [ ] **Step 3: Delete `HelloDecision.tsx` and `HelloPage.tsx`**:
  ```bash
  git rm frontend/trading-decision/src/components/HelloDecision.tsx
  git rm frontend/trading-decision/src/pages/HelloPage.tsx
  ```

- [ ] **Step 4: Update `App.css`** to a tiny route-shell (header bar + container).

- [ ] **Step 5: Run** the full verification suite (§7.3). Everything green.

- [ ] **Step 6: Commit**
  ```bash
  git commit -am "feat(rob-7): mount router and decision workspace pages"
  ```

### Task 13: Manual smoke + screenshots in PR

- [ ] **Step 1:** Run `make dev` (FastAPI :8000) and `make frontend-dev` (Vite :5173). Log in via `/login`.
- [ ] **Step 2:** Manually exercise:
  - Open `/trading/decisions/`. Confirm inbox renders. (You may need a seeded session — coordinate with the team if no fixture exists in dev DB; otherwise document this in the PR.)
  - Open a session detail. Accept one proposal; confirm row updates.
  - Modify another proposal (`original_quantity_pct=20` → `user_quantity_pct=10`); confirm `OriginalVsAdjustedSummary` shows both values.
  - Defer a third proposal.
  - Verify Linked actions panel renders an existing live order id, paper id, watch field, or "no linked actions yet."
- [ ] **Step 3:** Capture two screenshots (inbox + detail), drop them in the PR description.
- [ ] **Step 4:** No commit (smoke is optional but expected).

### Task 14: Open PR against `main`

- [ ] **Step 1:** Push branch.
- [ ] **Step 2:** `gh pr create --base main --title "feat(rob-7): trading decision workspace UI" --body @<<EOF` with body listing the §8 acceptance checklist + screenshot embeds + a "Out-of-scope" reminder list (§9).

---

## 13. Verification commands (one-shot at end)

```bash
cd /Users/mgh3326/work/auto_trader-worktrees/feature-ROB-7-trading-decision-workspace-ui

# Frontend
cd frontend/trading-decision
npm run typecheck
npm run test
npm run build
cd ../..

# Backend (must still be green; we did not touch it)
uv run pytest tests/test_trading_decisions_router.py \
              tests/test_trading_decisions_router_safety.py \
              tests/test_trading_decisions_spa_router.py \
              tests/test_trading_decisions_spa_router_safety.py -q

# Diff scope guard (must be empty besides allowed paths)
git diff --stat origin/main...HEAD | grep -E -v '^\s*(frontend/trading-decision/|\.github/workflows/frontend-trading-decision\.yml|docs/plans/ROB-7-)' || echo "OK: scope clean"
```

---

## 14. Open decisions (defaults chosen, easy to revisit)

1. **Router lib** — `react-router-dom@^7`. Industry default, batteries-included, supports SPA basename.
2. **State / data fetching lib** — none. Two pages, three endpoints, optimistic UX is not a goal. Adding TanStack Query is YAGNI.
3. **CSS strategy** — CSS modules. No Tailwind. Keeps the bundle small and avoids a config rabbit hole.
4. **Test runner** — Vitest. Native to Vite, zero extra config.
5. **Test mocking** — hand-rolled `mockFetch`, no MSW. Two endpoints; not worth a 50 KB dep.
6. **Decimal handling** — strings end-to-end. Display via `Intl.NumberFormat`. Editing via `<input type="text" inputMode="decimal">`.
7. **Refetch strategy** — after every successful `respond`, refetch the whole session. Predictable; no optimistic-merge bugs.
8. **No bulk respond** — three buttons, three POSTs. Server doesn't accept bulk (ROB-2 §15.7).
9. **No outcome rendering** — explicit; gated by Prompt 5.
10. **Pagination** — fixed 50/page; prev/next; no jump-to-page. Replace if the inbox grows past a few hundred.
11. **Error UX** — banner-style; no toast lib.
12. **i18n** — none in this PR; ko-KR locale is hard-coded for `formatDecimal` only. Replace when we have a translation strategy.

---

## 15. Out-of-scope reminders (do not creep)

If during implementation any of these is tempting, **stop and split into a follow-up PR**:

- Adding `POST /trading/api/proposals/{uuid}/actions` from the UI.
- Adding `POST /trading/api/proposals/{uuid}/outcomes` from the UI.
- Adding `POST /trading/api/decisions` (session creation) from the UI.
- Adding `POST /trading/api/decisions/{uuid}/proposals` (proposal creation) from the UI.
- Calling KIS / Upbit / Telegram / brokers / Redis directly from the SPA.
- Auto-refreshing via WebSocket or polling.
- Adding outcome rendering (Prompt 5).
- Embedding the SPA inside a Jinja template or rewriting Jinja pages.
- Touching `app/`, `alembic/`, `tests/test_trading_decisions_router*.py`, `tests/test_trading_decisions_spa_router*.py`, or any non-frontend file.
- Theme/dark mode/i18n.

---

## 16. Implementer handoff prompt (Codex YOLO)

Save the block below to `/tmp/ROB-7-codex-yolo-implementer-prompt.md` and launch with:

```bash
codex --yolo exec "$(cat /tmp/ROB-7-codex-yolo-implementer-prompt.md)"
```

```text
You are the implementer for ROB-7 (Trading Decision Workspace UI).

Worktree: /Users/mgh3326/work/auto_trader-worktrees/feature-ROB-7-trading-decision-workspace-ui
Branch:   feature/ROB-7-trading-decision-workspace-ui   (already checked out)
Plan:     docs/plans/ROB-7-trading-decision-workspace-ui-plan.md  ← READ FULLY, FOLLOW EXACTLY
Linear:   ROB-7  https://linear.app/mgh3326/issue/ROB-7/trading-decision-workspace-ui
Roadmap:  /Users/mgh3326/.hermes/workspace/prompts/auto_trader_trading_decision_workspace_roadmap.md  (Prompt 4)

Project context:
- React 19 + Vite 8 + TS 6 SPA already scaffolded under frontend/trading-decision/.
- Backend ROB-2 endpoints already exist at /trading/api/decisions, /trading/api/proposals/{uuid}/respond, etc.
- This PR ships ONLY the interactive UI for inbox + detail + per-proposal accept / reject / defer / modify / partial_accept, with original-vs-adjusted display and linked-actions read-only panel.

Hard constraints (do NOT relax without confirming with the planner):
1. NO backend Python file changes. Do not edit anything under app/, alembic/, tests/, scripts/.
2. UI calls ONLY /trading/api/* endpoints. No direct calls to KIS, Upbit, Telegram, Redis, brokers, etc.
3. UI does NOT call POST /actions, POST /counterfactuals, or POST /outcomes. (Out of scope; Prompt 5 + execution flow.)
4. UI does NOT create sessions or proposals. (Analyst path.)
5. Outcomes are NEVER rendered. LinkedActionsPanel must not show outcome rows.
6. Decimal fields are JSON strings end-to-end. UI never converts them to JS numbers for storage. Use Intl.NumberFormat for display only.
7. On 401 redirect to /login?next=...; on 404 render a not-found view; on 409 (respond) show "Session is archived" banner.
8. No tailwind, no Redux, no TanStack Query, no MSW. Use plain React + CSS modules + a hand-rolled mockFetch helper for tests.

Build order (TDD; one task per commit; run typecheck + tests after each):
  1. Task 1  — Add deps (react-router-dom, vitest, RTL, jsdom) + vitest.config.ts + sanity test. Plan §12.Task 1.
  2. Task 2  — Add `npm run test` step to .github/workflows/frontend-trading-decision.yml. Plan §12.Task 2.
  3. Task 3  — Typed API client: src/api/types.ts (mirror plan §5.2), client.ts ApiError, decisions.ts, mockFetch helper, api.decisions.test.ts. Plan §12.Task 3.
  4. Task 4  — formatDecimal + formatDateTime + tests. Plan §12.Task 4.
  5. Task 5  — fixtures.ts + LoadingView + ErrorView. Plan §12.Task 5.
  6. Task 6  — ProposalResponseControls + tests. Plan §12.Task 6.
  7. Task 7  — ProposalAdjustmentEditor + tests. Plan §12.Task 7.
  8. Task 8  — OriginalVsAdjustedSummary + LinkedActionsPanel + tests. Plan §12.Task 8.
  9. Task 9  — ProposalRow + StatusBadge + tests. Plan §12.Task 9.
  10. Task 10 — useDecisionInbox + SessionListPage + tests. Plan §12.Task 10.
  11. Task 11 — useDecisionSession + SessionDetailPage + MarketBriefPanel + tests. Plan §12.Task 11.
  12. Task 12 — Wire router; replace App.tsx; delete HelloDecision.tsx and HelloPage.tsx; update App.css. Plan §12.Task 12.
  13. Task 13 — Manual smoke; capture screenshots for PR. Plan §12.Task 13.
  14. Task 14 — Open PR against `main` titled "feat(rob-7): trading decision workspace UI" with the §8 acceptance checklist in the body.

After every task:
  cd frontend/trading-decision
  npm run typecheck && npm run test && npm run build
Then commit: `git commit -m "<scope>(rob-7): <verb> <thing>"`.

When all tasks finish, run the verification block in plan §13. Report any §8 checklist item you cannot mark complete in the PR description; do NOT silently skip.

API contract source-of-truth files (read once before writing types):
- app/routers/trading_decisions.py
- app/schemas/trading_decisions.py

If anything in the plan looks wrong or contradicts the source-of-truth files, STOP and surface the discrepancy in a short note before continuing — do not invent. The API as it exists today is the authority.
```

---

## 17. Self-review (planner notes)

- **Spec coverage** — every Prompt 4 requirement has a task or component:
  - "List decision sessions" → Task 10 (`SessionListPage`).
  - "Show detail page with market brief and proposal rows" → Task 11 (`SessionDetailPage` + `MarketBriefPanel`).
  - "accept / reject / defer / modify / partial_accept" → Task 6 + Task 7.
  - "List-style selection (BTC/ETH only, defer SOL)" → naturally satisfied by per-row controls; covered by `SessionDetailPage.test.tsx`.
  - "Inline adjustment shows original and adjusted values" → Task 7 (`ProposalAdjustmentEditor`) + Task 8 (`OriginalVsAdjustedSummary`).
  - "Linked actions: live order ids, paper ids, watch fields, no-action records" → Task 8 (`LinkedActionsPanel`).
  - "UI must not call live order execution directly" → §9 + §10 + bundle grep.
  - "UI only records decisions/actions via API" → enforced; UI doesn't call `/actions` or `/outcomes` at all (stricter than the prompt).
- **Placeholder scan** — no TBD/TODO; every code block is concrete enough for an implementer to type or paste.
- **Type consistency** — `Uuid`, `IsoDateTime`, `DecimalString`, the Literal unions, and the `ProposalDetail`/`SessionDetail` shape are repeated identically across §5.2, §6.2, and the test fixtures.
- **API consistency** — re-checked against `app/routers/trading_decisions.py` and `app/schemas/trading_decisions.py` in this worktree:
  - Endpoints: `GET /decisions`, `GET /decisions/{uuid}`, `POST /proposals/{uuid}/respond` ✅
  - `ProposalRespondRequest` model_validator: at least one user_* on modify/partial_accept ✅
  - `responded_at` server-stamped, never read from request body ✅
  - Decimal serialized as string by Pydantic v2 default ✅

AOE_STATUS: plan_ready
AOE_ISSUE: ROB-7
AOE_ROLE: planner
AOE_PLAN_PATH: docs/plans/ROB-7-trading-decision-workspace-ui-plan.md
AOE_NEXT: start_codex_yolo_implementer
