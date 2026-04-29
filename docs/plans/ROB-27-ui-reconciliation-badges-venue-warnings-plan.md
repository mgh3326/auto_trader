# ROB-27 — UI Reconciliation Badges & Venue Warnings (Implementation Plan)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

- **Linear:** ROB-27 — `[UI] Show reconciliation badges and venue warnings in Research Run and Decision Session`
- **Linear URL:** https://linear.app/mgh3326/issue/ROB-27/ui-show-reconciliation-badges-and-venue-warnings-in-research-run-and
- **Branch / worktree:** `feature/ROB-27-ui-reconciliation-badges-venue-warnings` at `/Users/mgh3326/work/auto_trader-worktrees/feature-ROB-27-ui-reconciliation-badges-venue-warnings`
- **Base:** `origin/main` at ROB-26 merge `33f5a37b7fbb9e2bca73f588fc8c7b06896f63f3`
- **Depends on (already merged):** ROB-22 (`pending_reconciliation_service`), ROB-23 (`nxt_classifier_service`), ROB-24 (`research_run` storage), ROB-25 (Decision Session `original_payload` carries reconciliation/NXT/venue fields), ROB-26 (scheduled refresh).

**Goal.** Surface the read-only reconciliation, NXT classification, and NXT venue eligibility metadata that ROB-22/23/25 already persist on each `TradingDecisionSession` proposal so the operator can: (a) tell at a glance whether a pending KR order is NXT-actionable, KR-broker-only, near-fill, or too-far; (b) see the live gap to current price and the nearest support / resistance; (c) read explicit warnings (`missing_kr_universe`, `non_nxt_venue`, `stale_quote`, `data_mismatch_*`, etc.) in the existing Decision Session UI.

**Architecture.** Pure frontend + a 1-line typing addition on the backend response shape. We add typed parsing of `proposal.original_payload` (which already contains `reconciliation_status`, `nxt_classification`, `nxt_eligible`, `venue_eligibility`, `decision_support`, `live_quote`, `pending_order_id`, `warnings`) on the React side and render new components: `ReconciliationBadge`, `NxtVenueBadge`, `WarningChips`, and a `ReconciliationDecisionSupportPanel` inside `ProposalRow`. We extend `MarketBriefPanel` to render the `reconciliation_summary` / `nxt_summary` counts as a structured summary instead of raw JSON. The Research Run "view" surface is the Decision Session that the run produced (no separate Research Run page exists today; the ROB-25 path persists the run summary into `session.market_brief.research_run_uuid` + `counts`), so this plan covers both surfaces by enhancing the existing `frontend/trading-decision` SPA.

**Tech stack.**
- React 19 + TypeScript 6 + Vite 8 + Vitest 4 + @testing-library/react (frontend; existing).
- No new runtime or test dependencies.
- No backend code changes except a *typing-only* additive Pydantic shape (`ProposalReconciliationPayload`) declared but **not** enforced at the response boundary — used only for keeping the JSON contract tested. The wire format is unchanged because `original_payload` is already a free-form `Record<string, unknown>` field that flows through `_to_proposal_detail`.

**Trading-safety guardrails (non-negotiable).** This issue is read-only / decision-support only.
- No new caller of `place_order`, `modify_order`, `cancel_order`, `manage_watch_alerts`, `paper_order_handler`, `kis_trading_service`, `kis_trading_contracts`, `fill_notification`, `execution_event`, `kis_websocket*`, `upbit_websocket`, broker `place_*`/`cancel_*`, dry-run / paper / live / advisory order placement.
- No outbound advisory call. TradingAgents output is **not** rendered through this UI work; we only display the persisted advisory_only payload.
- Decision Session creation is allowed only as decision-ledger persistence (existing endpoint), not as execution approval.
- No secrets / API keys / account numbers / order IDs from the broker are introduced into commits, logs, or test fixtures. Test fixtures use synthetic UUIDs and string symbols only (e.g. `005930`, `BRK.B`).
- `original_payload` is rendered as **typed parsed values** with `String(...)`/`Number(...)` coercion for known fields and a fallback that drops unknown keys; we never use `dangerouslySetInnerHTML` and never construct DOM from a server-supplied HTML string.

---

## File Structure

| Path | Status | Responsibility |
|------|--------|----------------|
| `frontend/trading-decision/src/api/reconciliation.ts` | create | Pure type definitions + `parseReconciliationPayload(original_payload)` parser. No React. No fetch. |
| `frontend/trading-decision/src/__tests__/api.reconciliation.test.ts` | create | Unit tests for the parser: typed extraction, missing-fields fallback, unknown-classification fallback, warning-token allowlist. |
| `frontend/trading-decision/src/api/types.ts` | modify | Add `original_payload` typed reading aid (no breaking change — keep `Record<string, unknown>` on `ProposalDetail.original_payload`). |
| `frontend/trading-decision/src/components/ReconciliationBadge.tsx` | create | Pure presentational badge for the reconciliation classification (`maintain` / `near_fill` / `too_far` / `chasing_risk` / `data_mismatch` / `kr_pending_non_nxt` / `unknown_venue` / `unknown` / null). |
| `frontend/trading-decision/src/components/ReconciliationBadge.module.css` | create | Color/state mapping per classification. |
| `frontend/trading-decision/src/components/NxtVenueBadge.tsx` | create | Pure presentational badge for NXT venue eligibility: `nxt-actionable`, `nxt-non-actionable`, `non-nxt`, `unknown`. Mirrors `nxt_eligible` + `nxt_classification` semantics from ROB-23. |
| `frontend/trading-decision/src/components/NxtVenueBadge.module.css` | create | Color/state mapping. |
| `frontend/trading-decision/src/components/WarningChips.tsx` | create | Pure presentational list of warning tokens. Allowlists known tokens; passes through `tokenLabel(token)` text only. |
| `frontend/trading-decision/src/components/WarningChips.module.css` | create | Style. |
| `frontend/trading-decision/src/components/ReconciliationDecisionSupportPanel.tsx` | create | Renders `gap_pct`, `signed_distance_to_fill`, nearest support / resistance, bid/ask spread, pending side / price / qty, `live_quote.price`, `live_quote.as_of`, `pending_order_id`. Pure presentational. |
| `frontend/trading-decision/src/components/ReconciliationDecisionSupportPanel.module.css` | create | Style. |
| `frontend/trading-decision/src/components/ProposalRow.tsx` | modify | Renders `ReconciliationBadge`, `NxtVenueBadge`, and `ReconciliationDecisionSupportPanel`. Marks the row "non-actionable" when `nxt_classification === "non_nxt_pending_ignore_for_nxt"` *or* `reconciliation_status === "kr_pending_non_nxt"`: hides the Accept primary CTA visually (greys it out, adds `aria-disabled` *but* keeps it functional for ledger-only "decline" responses) and adds an inline warning above the response controls. **Does not** change the existing `onRespond` plumbing or the operator's ability to record a decision. |
| `frontend/trading-decision/src/components/ProposalRow.module.css` | modify | Add `.nonActionable` row variant (subtle red/amber border + reduced background contrast). |
| `frontend/trading-decision/src/components/MarketBriefPanel.tsx` | modify | Render `research_run_uuid`, `refreshed_at`, structured `counts` and `reconciliation_summary` / `nxt_summary` as semantic lists when present, falling back to the existing JSON `<pre>` for unknown shapes. |
| `frontend/trading-decision/src/components/MarketBriefPanel.module.css` | modify | Add styles for the structured summary block. |
| `frontend/trading-decision/src/__tests__/ReconciliationBadge.test.tsx` | create | Renders one element per classification + null state; snapshot color class. |
| `frontend/trading-decision/src/__tests__/NxtVenueBadge.test.tsx` | create | Renders the four states; tests `aria-label` text. |
| `frontend/trading-decision/src/__tests__/WarningChips.test.tsx` | create | Renders warning tokens; unknown tokens are dropped (allowlist guard); innerHTML is never used. |
| `frontend/trading-decision/src/__tests__/ReconciliationDecisionSupportPanel.test.tsx` | create | Renders gap/distance/SR; missing values rendered as `—`. |
| `frontend/trading-decision/src/__tests__/ProposalRow.test.tsx` | modify | Adds: (a) near-fill renders green badge; (b) too-far renders red badge; (c) `kr_pending_non_nxt` renders the `non-actionable` row variant + `non_nxt_venue` warning chip; (d) `data_mismatch_requires_review` renders banner; (e) accepts pure-NXT `buy_pending_actionable` proposal does **not** show the non-actionable banner. |
| `frontend/trading-decision/src/__tests__/SessionDetailPage.test.tsx` | modify | Adds: structured market_brief summary renders `reconciliation_summary` counts and the `research_run_uuid` link line. |
| `frontend/trading-decision/src/test/fixtures.ts` | modify | Add fixture builders: `makeReconciliationPayload(overrides)`, plus extend `makeProposal` to optionally inject the payload into `original_payload`. Add `makeResearchRunMarketBrief(overrides)`. |
| `frontend/trading-decision/src/format/percent.ts` | create | `formatPercent(n: number \| string \| null \| undefined, fractionDigits = 2)` → `"-2.86%"`. |
| `frontend/trading-decision/src/__tests__/format.percent.test.ts` | create | Edge cases: `null`, `0`, negative, big number. |

**Backend (typing-only).** No runtime change. Add documentation on the JSON shape of `original_payload` in `app/schemas/research_run_decision_session.py` as a reference comment block; do not redefine `ProposalDetail.original_payload`. (Optional Task 12 below.)

**No changes to:** `app/services/pending_reconciliation_service.py`, `app/services/nxt_classifier_service.py`, `app/services/research_run_decision_session_service.py`, `app/routers/*`, `app/models/*`, `alembic/*`, server templates, `app/main.py`. **No** new env vars. **No** new DB columns or migrations. **No** broker/order modules touched.

---

## Data Contract Source for Reconciliation Fields

The fields rendered by this UI come from the proposal JSON returned by `GET /trading/api/decisions/{session_uuid}` (already implemented in `app/routers/trading_decisions.py:225`). Each `ProposalDetail.original_payload` is built in `app/services/research_run_decision_session_service.py:328-408` (`_proposal_payload`) with this shape (verbatim from the implementation):

```jsonc
{
  "advisory_only": true,
  "execution_allowed": false,
  "research_run_id": "<run_uuid>",
  "research_run_candidate_id": <int>,
  "refreshed_at": "<iso8601 utc>",
  "candidate_kind": "pending_order|holding|screener_hit|proposed|other",
  "pending_order_id": "<order_id>|null",
  "reconciliation_status": "maintain|near_fill|too_far|chasing_risk|data_mismatch|kr_pending_non_nxt|unknown_venue|unknown|null",
  "reconciliation_summary": "<comma-joined reasons>|null",
  "nxt_classification": "buy_pending_at_support|buy_pending_too_far|buy_pending_actionable|sell_pending_near_resistance|sell_pending_too_optimistic|sell_pending_actionable|non_nxt_pending_ignore_for_nxt|holding_watch_only|data_mismatch_requires_review|unknown|null",
  "nxt_summary": "<short string>|null",
  "nxt_eligible": true | false | null,
  "venue_eligibility": { "nxt": true | false | null, "regular": true | null },
  "live_quote": { "price": "<decimal-as-string>", "as_of": "<iso8601>" } | null,
  "decision_support": {
      "current_price": "<decimal-as-string>|null",
      "gap_pct": "<decimal-as-string>|null",
      "signed_distance_to_fill": "<decimal-as-string>|null",
      "nearest_support_price": "<decimal-as-string>|null",
      "nearest_support_distance_pct": "<decimal-as-string>|null",
      "nearest_resistance_price": "<decimal-as-string>|null",
      "nearest_resistance_distance_pct": "<decimal-as-string>|null",
      "bid_ask_spread_pct": "<decimal-as-string>|null"
  },
  "source_freshness": { ... } | null,
  "warnings": ["string", ...],
  "candidate_kind": "..."
}
```

The `ProposalCreate` row also carries `original_price`, `original_quantity`, `original_quantity_pct`, `side`, etc. (already typed in `frontend/trading-decision/src/api/types.ts`). For pending orders the implementer surfaces:
- **Pending side / price / qty** → `proposal.side` / `proposal.original_price` / `proposal.original_quantity` (already typed).
- **Distance to current price** → derived from `decision_support.gap_pct` (signed; positive means `current > ordered`).
- **Nearest support / resistance** → `decision_support.nearest_support_price` + `nearest_support_distance_pct` and the resistance pair.
- **`nxt_eligible` / `venue_eligibility`** → top-level keys.
- **`reconciliation_status`** → top-level key.
- **Warnings** → top-level `warnings: string[]` (allowlisted in the renderer).

The session-level `market_brief` (also a `Record<string, unknown>` returned as-is by `_to_session_detail`) is built in `_proposal_payload`'s sibling code in `research_run_decision_session_service` (search for `market_brief = {...}` in the same module) with `research_run_uuid`, `refreshed_at`, `counts`, `reconciliation_summary`, `nxt_summary`, `snapshot_warnings`, `source_warnings`. The implementer must read the *current* shape directly out of `research_run_decision_session_service.py` before writing tests (and update fixtures accordingly).

---

## Badge / Warning State Mapping

### `ReconciliationBadge` color & label table

| `reconciliation_status` | Label | Visual style | When | Why distinguishable from "too_far" / "non-NXT" |
|---|---|---|---|---|
| `near_fill` | "Near fill" | green pill, dark green text | Pending order's gap is within `near_fill_pct` (default 0.5%). | Bright green; the panel below shows a small `signed_distance_to_fill` close to 0. |
| `maintain` | "Maintain" | neutral grey pill | Default classification when within tolerance. | Grey, not green. |
| `chasing_risk` | "Chasing risk" | amber pill | Order is being left behind by the market and SR confirms. | Amber + a "near support/resistance" reason chip. |
| `too_far` | "Too far" | red pill | `signed_distance_to_fill < 0` and `\|gap_pct\| >= too_far_pct` (default 5%). | Red pill with a *negative* signed_distance_to_fill rendered next to the gap. |
| `data_mismatch` | "Data mismatch" | red-striped pill | Currency contradicts market, non-positive price/qty. | Red and dashed border. |
| `kr_pending_non_nxt` | "KR broker only" | grey-on-amber pill, "Non-actionable" sublabel | KR pending whose symbol has `nxt_eligible=false`. | Distinct amber-on-grey palette + accompanying `non_nxt_venue` warning chip. |
| `unknown_venue` | "Unknown venue" | red pill | Market or side parsed as something not in `{kr, us, crypto}` × `{buy, sell}`. | Red. |
| `unknown` | "Unknown" | neutral grey | Quote was missing; classification fell through. | Grey (different shade from `maintain`) + a `missing_quote` chip. |
| `null` (absent) | (no badge) | — | Proposal not derived from a research-run reconciliation. | Renders nothing; the row remains the existing styling. |

### `NxtVenueBadge` (KR only)

| `(market_scope, nxt_classification, nxt_eligible)` | Badge text | Style |
|---|---|---|
| `(kr, buy_pending_actionable | sell_pending_actionable | buy_pending_at_support | sell_pending_near_resistance, true)` | "NXT actionable" | green + ⚡-style icon (text only — no SVG to keep the change minimal; just the word). |
| `(kr, buy_pending_too_far | sell_pending_too_optimistic | non_nxt_pending_ignore_for_nxt, true)` | "NXT not actionable" | grey |
| `(kr, _, false)` | "Non-NXT (KR broker)" | amber |
| `(kr, _, null)` | "NXT eligibility unknown" | grey-striped |
| `(kr, data_mismatch_requires_review, _)` | "NXT review needed" | red |
| `(us | crypto, _, _)` | (no NXT badge) | — |

### Row-level "non-actionable" treatment in `ProposalRow`

A pending proposal is rendered with `.nonActionable` styling **only when both** of the following are true (so we never disable a row simply because a quote is stale):

1. `proposal.proposal_kind === "other"` (set by ROB-25 for `pending_order` candidates) **and** `original_payload.candidate_kind === "pending_order"`.
2. Either:
   - `original_payload.reconciliation_status === "kr_pending_non_nxt"`, **or**
   - `original_payload.nxt_classification === "non_nxt_pending_ignore_for_nxt"`, **or**
   - `original_payload.nxt_classification === "data_mismatch_requires_review"` (force review banner).

The `.nonActionable` variant: muted background (`#fff7ed`), amber 1px border, an inline `<p role="alert">` reading "Non-NXT pending order — KR broker routing only. Review before deciding." rendered **above** `ProposalResponseControls`, and the existing safety note reworded to "Recording a response on this row does not place or cancel a broker order." The Accept/Reject/etc. controls remain enabled because the operator must still be able to ledger their decision (e.g., "rejected" or "deferred"). They are **not** disabled.

This satisfies the AC "non-NXT pending orders visibly show a warning and are not styled as actionable" without removing the operator's ability to record a decision.

---

## Safe Rendering Guardrails

1. **No `dangerouslySetInnerHTML` anywhere.** Verified by Task 11's repo-grep test (`tests/grep_no_dangerous_html.test.ts`). Existing code never uses it; the test enforces this for new code too.
2. **Allowlisted classification & NXT label values.** Both badge components define a `KNOWN_<X>` set; anything outside the set falls through to the `unknown` label or to "(no badge)". Random server values cannot inject CSS classes by name because the badge component reads `styles[knownLabel]` only after lookup against the allowlist.
3. **Warning chip allowlist.** `WarningChips` accepts only tokens matching `^[a-z][a-z0-9_]{0,63}$` (mirrors `_WARNING_RE` from `app/schemas/research_run.py`). The renderer renders the **plain text token**; it never builds DOM with a token-derived element name or class. Unknown tokens still render as text but never as classes.
4. **String coercion for decimals.** Decimal payloads come back as strings; we render them through `formatDecimal` / `formatPercent`. We never `eval` or `Function`-construct.
5. **No raw HTML for the JSON market_brief fallback.** The fallback continues to use `<pre>{JSON.stringify(brief, null, 2)}</pre>`, which React text-encodes by default.

---

## Self-Review

### Spec coverage (acceptance criteria)
- "Add badges/columns/sections to relevant Research Run and/or Decision Session UI views" → Tasks 4 (`ReconciliationBadge`), 5 (`NxtVenueBadge`), 6 (`WarningChips`), 7 (`ReconciliationDecisionSupportPanel`), 8 (`ProposalRow` integration), 10 (`MarketBriefPanel` summary).
- "Show fields such as: pending side/price/qty, distance to current price, nearest support/resistance, `nxt_eligible`, `venue_eligibility`, `reconciliation_status`, warning messages" → Task 7 (decision-support panel) covers gap/distance/SR; Task 8 wires side/price/qty from existing `proposal.*` props; Tasks 4/5/6 cover badges + warnings.
- "Non-NXT pending orders visibly show a warning and are not styled as actionable" → Task 8 (`.nonActionable` row variant + alert banner).
- "Near-fill and too-far pending states are distinguishable" → Task 4 color table (green vs red) plus Task 8 ProposalRow tests.
- "Safe rendering: no unsafe `innerHTML` for untrusted payload fields" → Task 11 (lint-style test).
- "Tests/smoke cover rendering of warning states" → Tasks 4-10 each ship co-located vitest specs; Task 13 smoke runs `npm test`.

### Placeholder scan
No "TBD", "TODO", "implement later", or "similar to Task N" left in this plan. Each task contains exact code, exact paths, and exact test bodies.

### Type consistency
`ReconciliationStatus`, `NxtClassification`, `VenueEligibility`, `ReconciliationDecisionSupport`, `ReconciliationPayload` are defined once in Task 1 (`api/reconciliation.ts`) and referenced by name everywhere later. `parseReconciliationPayload` (Task 1) is the only entry point used by Tasks 4/5/6/7/8.

---

## Tasks

### Task 1 — Define `ReconciliationPayload` types and the parser

**Files:**
- Create: `frontend/trading-decision/src/api/reconciliation.ts`
- Create: `frontend/trading-decision/src/__tests__/api.reconciliation.test.ts`

- [ ] **Step 1: Write the failing tests.**

Write `frontend/trading-decision/src/__tests__/api.reconciliation.test.ts`:
```ts
import { describe, expect, it } from "vitest";
import {
  KNOWN_RECON_CLASSIFICATIONS,
  KNOWN_NXT_CLASSIFICATIONS,
  parseReconciliationPayload,
} from "../api/reconciliation";

describe("parseReconciliationPayload", () => {
  it("returns null when payload is missing core fields", () => {
    expect(parseReconciliationPayload(null)).toBeNull();
    expect(parseReconciliationPayload({})).toBeNull();
  });

  it("parses a happy-path KR pending order payload", () => {
    const parsed = parseReconciliationPayload({
      advisory_only: true,
      execution_allowed: false,
      research_run_id: "11111111-1111-1111-1111-111111111111",
      candidate_kind: "pending_order",
      pending_order_id: "ORD-1",
      reconciliation_status: "near_fill",
      reconciliation_summary: "gap_within_near_fill_pct",
      nxt_classification: "buy_pending_actionable",
      nxt_eligible: true,
      venue_eligibility: { nxt: true, regular: true },
      live_quote: { price: "70200", as_of: "2026-04-29T01:00:00Z" },
      decision_support: {
        current_price: "70200",
        gap_pct: "0.2857",
        signed_distance_to_fill: "-0.2857",
        nearest_support_price: null,
        nearest_support_distance_pct: null,
        nearest_resistance_price: null,
        nearest_resistance_distance_pct: null,
        bid_ask_spread_pct: null,
      },
      warnings: ["missing_orderbook"],
    });

    expect(parsed).not.toBeNull();
    expect(parsed?.reconciliation_status).toBe("near_fill");
    expect(parsed?.nxt_classification).toBe("buy_pending_actionable");
    expect(parsed?.nxt_eligible).toBe(true);
    expect(parsed?.venue_eligibility?.nxt).toBe(true);
    expect(parsed?.warnings).toEqual(["missing_orderbook"]);
    expect(parsed?.candidate_kind).toBe("pending_order");
    expect(parsed?.live_quote?.price).toBe("70200");
  });

  it("falls back to unknown for unrecognized classifications", () => {
    const parsed = parseReconciliationPayload({
      reconciliation_status: "<script>",
      nxt_classification: "EVIL",
      candidate_kind: "pending_order",
      warnings: [],
    });
    expect(parsed?.reconciliation_status).toBe("unknown");
    expect(parsed?.nxt_classification).toBe("unknown");
  });

  it("drops warning tokens that fail the allowlist", () => {
    const parsed = parseReconciliationPayload({
      candidate_kind: "pending_order",
      reconciliation_status: "maintain",
      warnings: [
        "missing_quote",
        "<script>alert(1)</script>",
        "Non_NXT_Venue",
        "non_nxt_venue",
      ],
    });
    expect(parsed?.warnings).toEqual(["missing_quote", "non_nxt_venue"]);
  });

  it("preserves null venue eligibility entries", () => {
    const parsed = parseReconciliationPayload({
      candidate_kind: "holding",
      reconciliation_status: null,
      nxt_classification: "holding_watch_only",
      nxt_eligible: null,
      venue_eligibility: { nxt: null, regular: true },
      warnings: [],
    });
    expect(parsed?.nxt_eligible).toBeNull();
    expect(parsed?.venue_eligibility?.nxt).toBeNull();
  });

  it("KNOWN sets are non-empty and stable", () => {
    expect(KNOWN_RECON_CLASSIFICATIONS).toContain("near_fill");
    expect(KNOWN_RECON_CLASSIFICATIONS).toContain("kr_pending_non_nxt");
    expect(KNOWN_NXT_CLASSIFICATIONS).toContain("non_nxt_pending_ignore_for_nxt");
    expect(KNOWN_NXT_CLASSIFICATIONS).toContain("data_mismatch_requires_review");
  });
});
```

- [ ] **Step 2: Run the tests; confirm they fail.**

Run from the worktree root:
```bash
cd frontend/trading-decision && npm test -- --run api.reconciliation.test.ts
```
Expected: FAIL — module `../api/reconciliation` not found.

- [ ] **Step 3: Create `frontend/trading-decision/src/api/reconciliation.ts`.**

```ts
export type ReconciliationStatus =
  | "maintain"
  | "near_fill"
  | "too_far"
  | "chasing_risk"
  | "data_mismatch"
  | "kr_pending_non_nxt"
  | "unknown_venue"
  | "unknown";

export type NxtClassification =
  | "buy_pending_at_support"
  | "buy_pending_too_far"
  | "buy_pending_actionable"
  | "sell_pending_near_resistance"
  | "sell_pending_too_optimistic"
  | "sell_pending_actionable"
  | "non_nxt_pending_ignore_for_nxt"
  | "holding_watch_only"
  | "data_mismatch_requires_review"
  | "unknown";

export type CandidateKind =
  | "pending_order"
  | "holding"
  | "screener_hit"
  | "proposed"
  | "other";

export const KNOWN_RECON_CLASSIFICATIONS: ReadonlyArray<ReconciliationStatus> = [
  "maintain",
  "near_fill",
  "too_far",
  "chasing_risk",
  "data_mismatch",
  "kr_pending_non_nxt",
  "unknown_venue",
  "unknown",
];

export const KNOWN_NXT_CLASSIFICATIONS: ReadonlyArray<NxtClassification> = [
  "buy_pending_at_support",
  "buy_pending_too_far",
  "buy_pending_actionable",
  "sell_pending_near_resistance",
  "sell_pending_too_optimistic",
  "sell_pending_actionable",
  "non_nxt_pending_ignore_for_nxt",
  "holding_watch_only",
  "data_mismatch_requires_review",
  "unknown",
];

const KNOWN_CANDIDATE_KINDS: ReadonlyArray<CandidateKind> = [
  "pending_order",
  "holding",
  "screener_hit",
  "proposed",
  "other",
];

const WARNING_TOKEN = /^[a-z][a-z0-9_]{0,63}$/;

export interface VenueEligibility {
  nxt: boolean | null;
  regular: boolean | null;
}

export interface ReconciliationDecisionSupport {
  current_price: string | null;
  gap_pct: string | null;
  signed_distance_to_fill: string | null;
  nearest_support_price: string | null;
  nearest_support_distance_pct: string | null;
  nearest_resistance_price: string | null;
  nearest_resistance_distance_pct: string | null;
  bid_ask_spread_pct: string | null;
}

export interface LiveQuote {
  price: string;
  as_of: string;
}

export interface ReconciliationPayload {
  research_run_id: string | null;
  candidate_kind: CandidateKind | null;
  pending_order_id: string | null;
  reconciliation_status: ReconciliationStatus | null;
  reconciliation_summary: string | null;
  nxt_classification: NxtClassification | null;
  nxt_summary: string | null;
  nxt_eligible: boolean | null;
  venue_eligibility: VenueEligibility | null;
  live_quote: LiveQuote | null;
  decision_support: ReconciliationDecisionSupport;
  warnings: string[];
  refreshed_at: string | null;
}

function pickString(v: unknown): string | null {
  return typeof v === "string" && v.length > 0 ? v : null;
}

function pickStringOrNumber(v: unknown): string | null {
  if (typeof v === "string" && v.length > 0) return v;
  if (typeof v === "number" && Number.isFinite(v)) return String(v);
  return null;
}

function pickBool(v: unknown): boolean | null {
  return typeof v === "boolean" ? v : null;
}

function pickClassification(
  v: unknown,
): ReconciliationStatus | null {
  if (typeof v !== "string") return null;
  const found = KNOWN_RECON_CLASSIFICATIONS.find((c) => c === v);
  return found ?? "unknown";
}

function pickNxtClassification(v: unknown): NxtClassification | null {
  if (typeof v !== "string") return null;
  const found = KNOWN_NXT_CLASSIFICATIONS.find((c) => c === v);
  return found ?? "unknown";
}

function pickCandidateKind(v: unknown): CandidateKind | null {
  if (typeof v !== "string") return null;
  const found = KNOWN_CANDIDATE_KINDS.find((c) => c === v);
  return found ?? null;
}

function pickWarnings(v: unknown): string[] {
  if (!Array.isArray(v)) return [];
  const out: string[] = [];
  for (const item of v) {
    if (typeof item === "string" && WARNING_TOKEN.test(item)) {
      out.push(item);
    }
  }
  return out;
}

function pickDecisionSupport(v: unknown): ReconciliationDecisionSupport {
  const blank: ReconciliationDecisionSupport = {
    current_price: null,
    gap_pct: null,
    signed_distance_to_fill: null,
    nearest_support_price: null,
    nearest_support_distance_pct: null,
    nearest_resistance_price: null,
    nearest_resistance_distance_pct: null,
    bid_ask_spread_pct: null,
  };
  if (!v || typeof v !== "object") return blank;
  const o = v as Record<string, unknown>;
  return {
    current_price: pickStringOrNumber(o.current_price),
    gap_pct: pickStringOrNumber(o.gap_pct),
    signed_distance_to_fill: pickStringOrNumber(o.signed_distance_to_fill),
    nearest_support_price: pickStringOrNumber(o.nearest_support_price),
    nearest_support_distance_pct: pickStringOrNumber(
      o.nearest_support_distance_pct,
    ),
    nearest_resistance_price: pickStringOrNumber(o.nearest_resistance_price),
    nearest_resistance_distance_pct: pickStringOrNumber(
      o.nearest_resistance_distance_pct,
    ),
    bid_ask_spread_pct: pickStringOrNumber(o.bid_ask_spread_pct),
  };
}

function pickVenueEligibility(v: unknown): VenueEligibility | null {
  if (!v || typeof v !== "object") return null;
  const o = v as Record<string, unknown>;
  if (!("nxt" in o)) return null;
  return {
    nxt: pickBool(o.nxt),
    regular: pickBool(o.regular),
  };
}

function pickLiveQuote(v: unknown): LiveQuote | null {
  if (!v || typeof v !== "object") return null;
  const o = v as Record<string, unknown>;
  const price = pickStringOrNumber(o.price);
  const asOf = pickString(o.as_of);
  if (!price || !asOf) return null;
  return { price, as_of: asOf };
}

const HAS_PAYLOAD_KEYS: ReadonlyArray<string> = [
  "reconciliation_status",
  "nxt_classification",
  "candidate_kind",
  "research_run_id",
  "venue_eligibility",
];

export function parseReconciliationPayload(
  raw: unknown,
): ReconciliationPayload | null {
  if (!raw || typeof raw !== "object") return null;
  const o = raw as Record<string, unknown>;
  const hasAny = HAS_PAYLOAD_KEYS.some((k) => k in o);
  if (!hasAny) return null;

  return {
    research_run_id: pickString(o.research_run_id),
    candidate_kind: pickCandidateKind(o.candidate_kind),
    pending_order_id: pickString(o.pending_order_id),
    reconciliation_status: pickClassification(o.reconciliation_status),
    reconciliation_summary: pickString(o.reconciliation_summary),
    nxt_classification: pickNxtClassification(o.nxt_classification),
    nxt_summary: pickString(o.nxt_summary),
    nxt_eligible: pickBool(o.nxt_eligible),
    venue_eligibility: pickVenueEligibility(o.venue_eligibility),
    live_quote: pickLiveQuote(o.live_quote),
    decision_support: pickDecisionSupport(o.decision_support),
    warnings: pickWarnings(o.warnings),
    refreshed_at: pickString(o.refreshed_at),
  };
}
```

- [ ] **Step 4: Run tests; confirm they pass.**

```bash
cd frontend/trading-decision && npm test -- --run api.reconciliation.test.ts
```
Expected: PASS (6 tests).

- [ ] **Step 5: Commit.**

```bash
git add frontend/trading-decision/src/api/reconciliation.ts \
        frontend/trading-decision/src/__tests__/api.reconciliation.test.ts
git commit -m "$(cat <<'EOF'
feat(rob-27): add reconciliation payload types and parser

Co-Authored-By: Paperclip <noreply@paperclip.ing>
EOF
)"
```

---

### Task 2 — Add `formatPercent` helper

**Files:**
- Create: `frontend/trading-decision/src/format/percent.ts`
- Create: `frontend/trading-decision/src/__tests__/format.percent.test.ts`

- [ ] **Step 1: Write the failing tests.**

Write `frontend/trading-decision/src/__tests__/format.percent.test.ts`:
```ts
import { describe, expect, it } from "vitest";
import { formatPercent } from "../format/percent";

describe("formatPercent", () => {
  it("returns em-dash on null/undefined", () => {
    expect(formatPercent(null)).toBe("—");
    expect(formatPercent(undefined)).toBe("—");
    expect(formatPercent("")).toBe("—");
  });

  it("formats positive and negative numbers with sign", () => {
    expect(formatPercent("0.5")).toBe("+0.50%");
    expect(formatPercent("-2.857")).toBe("-2.86%");
    expect(formatPercent(0)).toBe("0.00%");
  });

  it("falls back to raw string when not finite", () => {
    expect(formatPercent("not-a-number")).toBe("not-a-number");
  });

  it("respects fractionDigits override", () => {
    expect(formatPercent("1.23456", 4)).toBe("+1.2346%");
  });
});
```

- [ ] **Step 2: Run; confirm fail.**

```bash
cd frontend/trading-decision && npm test -- --run format.percent.test.ts
```
Expected: FAIL.

- [ ] **Step 3: Create `frontend/trading-decision/src/format/percent.ts`.**

```ts
export function formatPercent(
  v: string | number | null | undefined,
  fractionDigits = 2,
): string {
  if (v === null || v === undefined) return "—";
  if (typeof v === "string" && v.length === 0) return "—";
  const n = typeof v === "number" ? v : Number(v);
  if (!Number.isFinite(n)) return String(v);
  const absStr = Math.abs(n).toFixed(fractionDigits);
  if (n > 0) return `+${absStr}%`;
  if (n < 0) return `-${absStr}%`;
  return `${absStr}%`;
}
```

- [ ] **Step 4: Run; confirm pass.**

```bash
cd frontend/trading-decision && npm test -- --run format.percent.test.ts
```
Expected: PASS.

- [ ] **Step 5: Commit.**

```bash
git add frontend/trading-decision/src/format/percent.ts \
        frontend/trading-decision/src/__tests__/format.percent.test.ts
git commit -m "$(cat <<'EOF'
feat(rob-27): add formatPercent helper for signed percentage strings

Co-Authored-By: Paperclip <noreply@paperclip.ing>
EOF
)"
```

---

### Task 3 — Extend `test/fixtures.ts` with reconciliation builders

**Files:**
- Modify: `frontend/trading-decision/src/test/fixtures.ts`

- [ ] **Step 1: Add fixture builders at the bottom of `fixtures.ts`.**

Append:
```ts
import type {
  CandidateKind,
  NxtClassification,
  ReconciliationPayload,
  ReconciliationStatus,
} from "../api/reconciliation";

export function makeReconciliationPayload(
  overrides: Partial<ReconciliationPayload> = {},
): ReconciliationPayload {
  return {
    research_run_id: "11111111-1111-1111-1111-111111111111",
    candidate_kind: "pending_order" as CandidateKind,
    pending_order_id: "ORD-1",
    reconciliation_status: "near_fill" as ReconciliationStatus,
    reconciliation_summary: "gap_within_near_fill_pct",
    nxt_classification: "buy_pending_actionable" as NxtClassification,
    nxt_summary: "Pending fill within 0.5% of current price.",
    nxt_eligible: true,
    venue_eligibility: { nxt: true, regular: true },
    live_quote: { price: "70200", as_of: "2026-04-29T01:00:00Z" },
    decision_support: {
      current_price: "70200",
      gap_pct: "0.2857",
      signed_distance_to_fill: "-0.2857",
      nearest_support_price: "69500",
      nearest_support_distance_pct: "1.0",
      nearest_resistance_price: "71000",
      nearest_resistance_distance_pct: "1.14",
      bid_ask_spread_pct: "0.05",
    },
    warnings: [],
    refreshed_at: "2026-04-29T01:00:00Z",
    ...overrides,
  };
}

export function makeResearchRunMarketBrief(
  overrides: Record<string, unknown> = {},
): Record<string, unknown> {
  return {
    advisory_only: true,
    execution_allowed: false,
    research_run_uuid: "11111111-1111-1111-1111-111111111111",
    refreshed_at: "2026-04-29T01:00:00Z",
    counts: { candidates: 3, reconciliations: 1 },
    reconciliation_summary: {
      maintain: 1,
      near_fill: 1,
      too_far: 0,
      chasing_risk: 0,
      data_mismatch: 0,
      kr_pending_non_nxt: 1,
      unknown_venue: 0,
      unknown: 0,
    },
    nxt_summary: {
      actionable: 1,
      too_far: 0,
      non_nxt: 1,
      watch_only: 1,
      data_mismatch_requires_review: 0,
      unknown: 0,
    },
    snapshot_warnings: ["missing_orderbook"],
    source_warnings: [],
    ...overrides,
  };
}
```

- [ ] **Step 2: Type-check.**

Run:
```bash
cd frontend/trading-decision && npm run typecheck
```
Expected: clean.

- [ ] **Step 3: Commit.**

```bash
git add frontend/trading-decision/src/test/fixtures.ts
git commit -m "$(cat <<'EOF'
test(rob-27): add reconciliation payload + market brief fixture builders

Co-Authored-By: Paperclip <noreply@paperclip.ing>
EOF
)"
```

---

### Task 4 — `ReconciliationBadge` component

**Files:**
- Create: `frontend/trading-decision/src/components/ReconciliationBadge.tsx`
- Create: `frontend/trading-decision/src/components/ReconciliationBadge.module.css`
- Create: `frontend/trading-decision/src/__tests__/ReconciliationBadge.test.tsx`

- [ ] **Step 1: Write the failing tests.**

Write `ReconciliationBadge.test.tsx`:
```tsx
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import ReconciliationBadge from "../components/ReconciliationBadge";

describe("ReconciliationBadge", () => {
  it("renders a label for each known classification", () => {
    const cases: Array<[string, string]> = [
      ["maintain", "Maintain"],
      ["near_fill", "Near fill"],
      ["too_far", "Too far"],
      ["chasing_risk", "Chasing risk"],
      ["data_mismatch", "Data mismatch"],
      ["kr_pending_non_nxt", "KR broker only"],
      ["unknown_venue", "Unknown venue"],
      ["unknown", "Unknown"],
    ];
    for (const [value, label] of cases) {
      const { unmount } = render(
        <ReconciliationBadge
          // eslint-disable-next-line @typescript-eslint/no-explicit-any
          value={value as any}
        />,
      );
      expect(screen.getByText(label)).toBeInTheDocument();
      unmount();
    }
  });

  it("renders nothing when value is null", () => {
    const { container } = render(<ReconciliationBadge value={null} />);
    expect(container.firstChild).toBeNull();
  });

  it("renders an aria-label for accessibility", () => {
    render(<ReconciliationBadge value="too_far" />);
    expect(
      screen.getByLabelText("Reconciliation status: Too far"),
    ).toBeInTheDocument();
  });
});
```

- [ ] **Step 2: Run; confirm fail.**

```bash
cd frontend/trading-decision && npm test -- --run ReconciliationBadge.test.tsx
```
Expected: FAIL — module not found.

- [ ] **Step 3: Create the component and its CSS.**

`frontend/trading-decision/src/components/ReconciliationBadge.tsx`:
```tsx
import type { ReconciliationStatus } from "../api/reconciliation";
import styles from "./ReconciliationBadge.module.css";

interface Props {
  value: ReconciliationStatus | null;
}

const LABEL: Record<ReconciliationStatus, string> = {
  maintain: "Maintain",
  near_fill: "Near fill",
  too_far: "Too far",
  chasing_risk: "Chasing risk",
  data_mismatch: "Data mismatch",
  kr_pending_non_nxt: "KR broker only",
  unknown_venue: "Unknown venue",
  unknown: "Unknown",
};

export default function ReconciliationBadge({ value }: Props) {
  if (value === null) return null;
  const label = LABEL[value];
  return (
    <span
      aria-label={`Reconciliation status: ${label}`}
      className={`${styles.badge} ${styles[value]}`}
    >
      {label}
    </span>
  );
}
```

`frontend/trading-decision/src/components/ReconciliationBadge.module.css`:
```css
.badge {
  border-radius: 999px;
  display: inline-flex;
  font-size: 0.78rem;
  font-weight: 700;
  line-height: 1;
  padding: 5px 8px;
}

.maintain {
  background: #eef1f5;
  color: #495467;
}
.near_fill {
  background: #dff7e7;
  color: #176b35;
}
.too_far {
  background: #fde2e2;
  color: #9f2d2d;
}
.chasing_risk {
  background: #fdebcb;
  color: #8c5b00;
}
.data_mismatch {
  background: #fdebeb;
  border: 1px dashed #c44a4a;
  color: #8b1b1b;
}
.kr_pending_non_nxt {
  background: #fff3cd;
  color: #6b3f00;
}
.unknown_venue {
  background: #fde2e2;
  color: #6b1f1f;
}
.unknown {
  background: #e6e9ee;
  color: #4d5666;
}
```

- [ ] **Step 4: Run; confirm pass.**

```bash
cd frontend/trading-decision && npm test -- --run ReconciliationBadge.test.tsx
```
Expected: PASS.

- [ ] **Step 5: Commit.**

```bash
git add frontend/trading-decision/src/components/ReconciliationBadge.tsx \
        frontend/trading-decision/src/components/ReconciliationBadge.module.css \
        frontend/trading-decision/src/__tests__/ReconciliationBadge.test.tsx
git commit -m "$(cat <<'EOF'
feat(rob-27): add ReconciliationBadge component with classification → label/color map

Co-Authored-By: Paperclip <noreply@paperclip.ing>
EOF
)"
```

---

### Task 5 — `NxtVenueBadge` component

**Files:**
- Create: `frontend/trading-decision/src/components/NxtVenueBadge.tsx`
- Create: `frontend/trading-decision/src/components/NxtVenueBadge.module.css`
- Create: `frontend/trading-decision/src/__tests__/NxtVenueBadge.test.tsx`

- [ ] **Step 1: Write the failing tests.**

```tsx
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import NxtVenueBadge from "../components/NxtVenueBadge";

describe("NxtVenueBadge", () => {
  it("renders 'NXT actionable' for KR + actionable + nxt_eligible=true", () => {
    render(
      <NxtVenueBadge
        marketScope="kr"
        nxtClassification="buy_pending_actionable"
        nxtEligible={true}
      />,
    );
    expect(screen.getByText("NXT actionable")).toBeInTheDocument();
  });

  it("renders 'NXT not actionable' for too-far / ignore_for_nxt", () => {
    render(
      <NxtVenueBadge
        marketScope="kr"
        nxtClassification="buy_pending_too_far"
        nxtEligible={true}
      />,
    );
    expect(screen.getByText("NXT not actionable")).toBeInTheDocument();
  });

  it("renders 'Non-NXT (KR broker)' when nxt_eligible=false", () => {
    render(
      <NxtVenueBadge
        marketScope="kr"
        nxtClassification="non_nxt_pending_ignore_for_nxt"
        nxtEligible={false}
      />,
    );
    expect(screen.getByText("Non-NXT (KR broker)")).toBeInTheDocument();
  });

  it("renders 'NXT eligibility unknown' when nxt_eligible is null", () => {
    render(
      <NxtVenueBadge
        marketScope="kr"
        nxtClassification={null}
        nxtEligible={null}
      />,
    );
    expect(screen.getByText("NXT eligibility unknown")).toBeInTheDocument();
  });

  it("renders 'NXT review needed' for data_mismatch_requires_review", () => {
    render(
      <NxtVenueBadge
        marketScope="kr"
        nxtClassification="data_mismatch_requires_review"
        nxtEligible={true}
      />,
    );
    expect(screen.getByText("NXT review needed")).toBeInTheDocument();
  });

  it("renders nothing for non-KR markets", () => {
    const { container } = render(
      <NxtVenueBadge
        marketScope="us"
        nxtClassification={null}
        nxtEligible={null}
      />,
    );
    expect(container.firstChild).toBeNull();
  });
});
```

- [ ] **Step 2: Run; confirm fail.**

```bash
cd frontend/trading-decision && npm test -- --run NxtVenueBadge.test.tsx
```
Expected: FAIL.

- [ ] **Step 3: Create the component and its CSS.**

`NxtVenueBadge.tsx`:
```tsx
import type { NxtClassification } from "../api/reconciliation";
import styles from "./NxtVenueBadge.module.css";

interface Props {
  marketScope: string | null;
  nxtClassification: NxtClassification | null;
  nxtEligible: boolean | null;
}

const ACTIONABLE: ReadonlyArray<NxtClassification> = [
  "buy_pending_actionable",
  "sell_pending_actionable",
  "buy_pending_at_support",
  "sell_pending_near_resistance",
];

export default function NxtVenueBadge({
  marketScope,
  nxtClassification,
  nxtEligible,
}: Props) {
  if (marketScope !== "kr") return null;

  if (nxtClassification === "data_mismatch_requires_review") {
    return (
      <span className={`${styles.badge} ${styles.review}`}>
        NXT review needed
      </span>
    );
  }
  if (nxtEligible === false) {
    return (
      <span className={`${styles.badge} ${styles.nonNxt}`}>
        Non-NXT (KR broker)
      </span>
    );
  }
  if (nxtEligible === null) {
    return (
      <span className={`${styles.badge} ${styles.unknown}`}>
        NXT eligibility unknown
      </span>
    );
  }
  if (
    nxtClassification !== null &&
    ACTIONABLE.indexOf(nxtClassification) >= 0
  ) {
    return (
      <span className={`${styles.badge} ${styles.actionable}`}>
        NXT actionable
      </span>
    );
  }
  return (
    <span className={`${styles.badge} ${styles.notActionable}`}>
      NXT not actionable
    </span>
  );
}
```

`NxtVenueBadge.module.css`:
```css
.badge {
  border-radius: 999px;
  display: inline-flex;
  font-size: 0.78rem;
  font-weight: 700;
  line-height: 1;
  padding: 5px 8px;
}
.actionable {
  background: #dff7e7;
  color: #176b35;
}
.notActionable {
  background: #eef1f5;
  color: #495467;
}
.nonNxt {
  background: #fff3cd;
  color: #6b3f00;
}
.unknown {
  background: #e6e9ee;
  border: 1px dashed #939aa7;
  color: #4d5666;
}
.review {
  background: #fde2e2;
  color: #9f2d2d;
}
```

- [ ] **Step 4: Run; confirm pass.**

```bash
cd frontend/trading-decision && npm test -- --run NxtVenueBadge.test.tsx
```
Expected: PASS.

- [ ] **Step 5: Commit.**

```bash
git add frontend/trading-decision/src/components/NxtVenueBadge.tsx \
        frontend/trading-decision/src/components/NxtVenueBadge.module.css \
        frontend/trading-decision/src/__tests__/NxtVenueBadge.test.tsx
git commit -m "$(cat <<'EOF'
feat(rob-27): add NxtVenueBadge component for KR NXT eligibility states

Co-Authored-By: Paperclip <noreply@paperclip.ing>
EOF
)"
```

---

### Task 6 — `WarningChips` component

**Files:**
- Create: `frontend/trading-decision/src/components/WarningChips.tsx`
- Create: `frontend/trading-decision/src/components/WarningChips.module.css`
- Create: `frontend/trading-decision/src/__tests__/WarningChips.test.tsx`

- [ ] **Step 1: Write the failing tests.**

```tsx
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import WarningChips from "../components/WarningChips";

describe("WarningChips", () => {
  it("renders one chip per known token with friendly text", () => {
    render(
      <WarningChips
        tokens={[
          "missing_quote",
          "stale_quote",
          "missing_orderbook",
          "missing_support_resistance",
          "missing_kr_universe",
          "non_nxt_venue",
          "unknown_venue",
          "unknown_side",
        ]}
      />,
    );
    expect(screen.getByText("Quote missing")).toBeInTheDocument();
    expect(screen.getByText("Quote stale")).toBeInTheDocument();
    expect(screen.getByText("Orderbook missing")).toBeInTheDocument();
    expect(
      screen.getByText("Support / resistance unavailable"),
    ).toBeInTheDocument();
    expect(screen.getByText("KR universe row missing")).toBeInTheDocument();
    expect(screen.getByText("Non-NXT venue")).toBeInTheDocument();
    expect(screen.getByText("Unknown venue")).toBeInTheDocument();
    expect(screen.getByText("Unknown side")).toBeInTheDocument();
  });

  it("renders unknown-but-allowlist-shaped tokens verbatim as text", () => {
    render(<WarningChips tokens={["custom_warning_token"]} />);
    expect(screen.getByText("custom_warning_token")).toBeInTheDocument();
  });

  it("ignores tokens that fail the allowlist", () => {
    render(<WarningChips tokens={["<script>", "Foo Bar"]} />);
    expect(screen.queryByText("<script>")).not.toBeInTheDocument();
    expect(screen.queryByText("Foo Bar")).not.toBeInTheDocument();
  });

  it("returns null when there are no tokens", () => {
    const { container } = render(<WarningChips tokens={[]} />);
    expect(container.firstChild).toBeNull();
  });
});
```

- [ ] **Step 2: Run; confirm fail.**

```bash
cd frontend/trading-decision && npm test -- --run WarningChips.test.tsx
```
Expected: FAIL.

- [ ] **Step 3: Create the component and its CSS.**

`WarningChips.tsx`:
```tsx
import styles from "./WarningChips.module.css";

interface Props {
  tokens: string[];
}

const FRIENDLY: Record<string, string> = {
  missing_quote: "Quote missing",
  stale_quote: "Quote stale",
  missing_orderbook: "Orderbook missing",
  missing_support_resistance: "Support / resistance unavailable",
  missing_kr_universe: "KR universe row missing",
  non_nxt_venue: "Non-NXT venue",
  unknown_venue: "Unknown venue",
  unknown_side: "Unknown side",
};

const TOKEN_RE = /^[a-z][a-z0-9_]{0,63}$/;

export default function WarningChips({ tokens }: Props) {
  const safe = tokens.filter((t) => TOKEN_RE.test(t));
  if (safe.length === 0) return null;
  return (
    <ul aria-label="Warnings" className={styles.list}>
      {safe.map((token) => (
        <li
          aria-label={`Warning: ${FRIENDLY[token] ?? token}`}
          className={styles.chip}
          key={token}
        >
          {FRIENDLY[token] ?? token}
        </li>
      ))}
    </ul>
  );
}
```

`WarningChips.module.css`:
```css
.list {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  list-style: none;
  margin: 0;
  padding: 0;
}
.chip {
  background: #fff7ed;
  border: 1px solid #f5cf91;
  border-radius: 999px;
  color: #6b3f00;
  font-size: 0.78rem;
  font-weight: 600;
  padding: 4px 8px;
}
```

- [ ] **Step 4: Run; confirm pass.**

```bash
cd frontend/trading-decision && npm test -- --run WarningChips.test.tsx
```
Expected: PASS.

- [ ] **Step 5: Commit.**

```bash
git add frontend/trading-decision/src/components/WarningChips.tsx \
        frontend/trading-decision/src/components/WarningChips.module.css \
        frontend/trading-decision/src/__tests__/WarningChips.test.tsx
git commit -m "$(cat <<'EOF'
feat(rob-27): add WarningChips component with allowlisted token rendering

Co-Authored-By: Paperclip <noreply@paperclip.ing>
EOF
)"
```

---

### Task 7 — `ReconciliationDecisionSupportPanel` component

**Files:**
- Create: `frontend/trading-decision/src/components/ReconciliationDecisionSupportPanel.tsx`
- Create: `frontend/trading-decision/src/components/ReconciliationDecisionSupportPanel.module.css`
- Create: `frontend/trading-decision/src/__tests__/ReconciliationDecisionSupportPanel.test.tsx`

- [ ] **Step 1: Write the failing tests.**

```tsx
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import ReconciliationDecisionSupportPanel from "../components/ReconciliationDecisionSupportPanel";
import { makeReconciliationPayload } from "../test/fixtures";

describe("ReconciliationDecisionSupportPanel", () => {
  it("renders gap, distance to fill, support/resistance, spread, and live quote", () => {
    render(
      <ReconciliationDecisionSupportPanel
        side="buy"
        originalPrice="70000"
        originalQuantity="10"
        payload={makeReconciliationPayload()}
      />,
    );

    expect(screen.getByText(/Gap to current/)).toBeInTheDocument();
    expect(screen.getByText("+0.29%")).toBeInTheDocument();
    expect(screen.getByText(/Distance to fill/)).toBeInTheDocument();
    expect(screen.getByText("-0.29%")).toBeInTheDocument();
    expect(screen.getByText(/Nearest support/)).toBeInTheDocument();
    expect(screen.getByText(/69,500/)).toBeInTheDocument();
    expect(screen.getByText(/Nearest resistance/)).toBeInTheDocument();
    expect(screen.getByText(/Live quote/)).toBeInTheDocument();
    expect(screen.getByText(/70,200/)).toBeInTheDocument();
    expect(screen.getByText(/Pending order/)).toBeInTheDocument();
    expect(screen.getByText(/ORD-1/)).toBeInTheDocument();
  });

  it("renders em-dash for missing decimal fields", () => {
    render(
      <ReconciliationDecisionSupportPanel
        side="buy"
        originalPrice={null}
        originalQuantity={null}
        payload={makeReconciliationPayload({
          decision_support: {
            current_price: null,
            gap_pct: null,
            signed_distance_to_fill: null,
            nearest_support_price: null,
            nearest_support_distance_pct: null,
            nearest_resistance_price: null,
            nearest_resistance_distance_pct: null,
            bid_ask_spread_pct: null,
          },
          live_quote: null,
        })}
      />,
    );
    expect(screen.getAllByText("—").length).toBeGreaterThanOrEqual(4);
  });

  it("returns null when payload is null", () => {
    const { container } = render(
      <ReconciliationDecisionSupportPanel
        side="buy"
        originalPrice="70000"
        originalQuantity="10"
        payload={null}
      />,
    );
    expect(container.firstChild).toBeNull();
  });
});
```

- [ ] **Step 2: Run; confirm fail.**

```bash
cd frontend/trading-decision && npm test -- --run ReconciliationDecisionSupportPanel.test.tsx
```
Expected: FAIL.

- [ ] **Step 3: Create the component.**

`ReconciliationDecisionSupportPanel.tsx`:
```tsx
import type { ReconciliationPayload } from "../api/reconciliation";
import { formatDateTime } from "../format/datetime";
import { formatDecimal } from "../format/decimal";
import { formatPercent } from "../format/percent";
import styles from "./ReconciliationDecisionSupportPanel.module.css";

interface Props {
  side: string;
  originalPrice: string | null;
  originalQuantity: string | null;
  payload: ReconciliationPayload | null;
}

export default function ReconciliationDecisionSupportPanel({
  side,
  originalPrice,
  originalQuantity,
  payload,
}: Props) {
  if (payload === null) return null;
  const ds = payload.decision_support;
  return (
    <section
      aria-label="Reconciliation decision support"
      className={styles.panel}
    >
      <dl className={styles.list}>
        <Item label="Pending side" value={side} />
        <Item label="Pending price" value={formatDecimal(originalPrice)} />
        <Item label="Pending qty" value={formatDecimal(originalQuantity)} />
        <Item label="Pending order" value={payload.pending_order_id ?? "—"} />
        <Item
          label="Live quote"
          value={
            payload.live_quote === null
              ? "—"
              : `${formatDecimal(payload.live_quote.price)} (${formatDateTime(
                  payload.live_quote.as_of,
                )})`
          }
        />
        <Item label="Gap to current" value={formatPercent(ds.gap_pct)} />
        <Item
          label="Distance to fill"
          value={formatPercent(ds.signed_distance_to_fill)}
        />
        <Item
          label="Nearest support"
          value={
            ds.nearest_support_price === null
              ? "—"
              : `${formatDecimal(ds.nearest_support_price)} (${formatPercent(
                  ds.nearest_support_distance_pct,
                )})`
          }
        />
        <Item
          label="Nearest resistance"
          value={
            ds.nearest_resistance_price === null
              ? "—"
              : `${formatDecimal(ds.nearest_resistance_price)} (${formatPercent(
                  ds.nearest_resistance_distance_pct,
                )})`
          }
        />
        <Item
          label="Bid/ask spread"
          value={formatPercent(ds.bid_ask_spread_pct)}
        />
      </dl>
    </section>
  );
}

function Item({ label, value }: { label: string; value: string }) {
  return (
    <div className={styles.item}>
      <dt>{label}</dt>
      <dd>{value}</dd>
    </div>
  );
}
```

`ReconciliationDecisionSupportPanel.module.css`:
```css
.panel {
  background: #f8fafc;
  border: 1px solid #e2e8f0;
  border-radius: 8px;
  padding: 10px 12px;
}
.list {
  display: grid;
  gap: 6px 14px;
  grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
  margin: 0;
}
.item {
  display: flex;
  flex-direction: column;
  font-size: 0.86rem;
}
.item dt {
  color: #5d6b80;
  font-weight: 600;
}
.item dd {
  margin: 0;
  font-variant-numeric: tabular-nums;
}
```

- [ ] **Step 4: Run; confirm pass.**

```bash
cd frontend/trading-decision && npm test -- --run ReconciliationDecisionSupportPanel.test.tsx
```
Expected: PASS.

- [ ] **Step 5: Commit.**

```bash
git add frontend/trading-decision/src/components/ReconciliationDecisionSupportPanel.tsx \
        frontend/trading-decision/src/components/ReconciliationDecisionSupportPanel.module.css \
        frontend/trading-decision/src/__tests__/ReconciliationDecisionSupportPanel.test.tsx
git commit -m "$(cat <<'EOF'
feat(rob-27): add ReconciliationDecisionSupportPanel for gap/distance/SR rendering

Co-Authored-By: Paperclip <noreply@paperclip.ing>
EOF
)"
```

---

### Task 8 — Wire badges, decision-support panel, and `nonActionable` row variant into `ProposalRow`

**Files:**
- Modify: `frontend/trading-decision/src/components/ProposalRow.tsx`
- Modify: `frontend/trading-decision/src/components/ProposalRow.module.css`
- Modify: `frontend/trading-decision/src/__tests__/ProposalRow.test.tsx`

- [ ] **Step 1: Write the failing tests by appending to `ProposalRow.test.tsx`.**

```tsx
import { makeReconciliationPayload } from "../test/fixtures";

describe("ProposalRow — reconciliation/NXT badges", () => {
  it("renders the Near fill badge for near_fill", () => {
    render(
      <ProposalRow
        proposal={makeProposal({
          original_payload: makeReconciliationPayload({
            reconciliation_status: "near_fill",
            nxt_classification: "buy_pending_actionable",
          }) as unknown as Record<string, unknown>,
        })}
        onRecordOutcome={vi.fn()}
        onRespond={vi.fn()}
      />,
    );
    expect(screen.getByText("Near fill")).toBeInTheDocument();
    expect(screen.getByText("NXT actionable")).toBeInTheDocument();
  });

  it("renders the Too far badge for too_far", () => {
    render(
      <ProposalRow
        proposal={makeProposal({
          original_payload: makeReconciliationPayload({
            reconciliation_status: "too_far",
            nxt_classification: "buy_pending_too_far",
          }) as unknown as Record<string, unknown>,
        })}
        onRecordOutcome={vi.fn()}
        onRespond={vi.fn()}
      />,
    );
    expect(screen.getByText("Too far")).toBeInTheDocument();
    expect(screen.getByText("NXT not actionable")).toBeInTheDocument();
  });

  it("marks kr_pending_non_nxt rows non-actionable and shows non_nxt_venue chip", () => {
    render(
      <ProposalRow
        proposal={makeProposal({
          proposal_kind: "other",
          original_payload: makeReconciliationPayload({
            reconciliation_status: "kr_pending_non_nxt",
            nxt_classification: "non_nxt_pending_ignore_for_nxt",
            nxt_eligible: false,
            warnings: ["non_nxt_venue"],
          }) as unknown as Record<string, unknown>,
        })}
        onRecordOutcome={vi.fn()}
        onRespond={vi.fn()}
      />,
    );
    expect(screen.getByText("KR broker only")).toBeInTheDocument();
    expect(screen.getByText("Non-NXT (KR broker)")).toBeInTheDocument();
    expect(screen.getByText("Non-NXT venue")).toBeInTheDocument();
    expect(screen.getByRole("alert")).toHaveTextContent(
      /Non-NXT pending order/,
    );
  });

  it("renders review banner for data_mismatch_requires_review", () => {
    render(
      <ProposalRow
        proposal={makeProposal({
          proposal_kind: "other",
          original_payload: makeReconciliationPayload({
            reconciliation_status: "data_mismatch",
            nxt_classification: "data_mismatch_requires_review",
            nxt_eligible: true,
            warnings: ["missing_kr_universe"],
          }) as unknown as Record<string, unknown>,
        })}
        onRecordOutcome={vi.fn()}
        onRespond={vi.fn()}
      />,
    );
    expect(screen.getByText("NXT review needed")).toBeInTheDocument();
    expect(
      screen.getByText("KR universe row missing"),
    ).toBeInTheDocument();
  });

  it("does not mark actionable NXT rows as non-actionable", () => {
    render(
      <ProposalRow
        proposal={makeProposal({
          proposal_kind: "other",
          original_payload: makeReconciliationPayload({
            reconciliation_status: "near_fill",
            nxt_classification: "buy_pending_actionable",
            nxt_eligible: true,
          }) as unknown as Record<string, unknown>,
        })}
        onRecordOutcome={vi.fn()}
        onRespond={vi.fn()}
      />,
    );
    expect(
      screen.queryByText(/Non-NXT pending order/),
    ).not.toBeInTheDocument();
  });
});
```

- [ ] **Step 2: Run; confirm fail.**

```bash
cd frontend/trading-decision && npm test -- --run ProposalRow.test.tsx
```
Expected: FAIL — new assertions error because the badges are not rendered yet.

- [ ] **Step 3: Modify `ProposalRow.tsx`.**

Read the current file first (`frontend/trading-decision/src/components/ProposalRow.tsx`) to confirm the exact existing imports and render layout. Then:

1. Add imports at the top:
   ```tsx
   import { parseReconciliationPayload } from "../api/reconciliation";
   import NxtVenueBadge from "./NxtVenueBadge";
   import ReconciliationBadge from "./ReconciliationBadge";
   import ReconciliationDecisionSupportPanel from "./ReconciliationDecisionSupportPanel";
   import WarningChips from "./WarningChips";
   ```

2. Inside `ProposalRow`, after `const shouldShowSymbol = ...`, parse the payload and decide non-actionable:
   ```tsx
   const recon = parseReconciliationPayload(proposal.original_payload);
   const nonActionable =
     proposal.proposal_kind === "other" &&
     recon !== null &&
     recon.candidate_kind === "pending_order" &&
     (recon.reconciliation_status === "kr_pending_non_nxt" ||
       recon.nxt_classification === "non_nxt_pending_ignore_for_nxt" ||
       recon.nxt_classification === "data_mismatch_requires_review");
   ```

3. Replace the `<article className={styles.row}>` opening with:
   ```tsx
   <article className={`${styles.row} ${nonActionable ? styles.nonActionable : ""}`}>
   ```

4. In the header block, after `<StatusBadge value={proposal.user_response} />`, append:
   ```tsx
   {recon ? (
     <>
       <ReconciliationBadge value={recon.reconciliation_status} />
       <NxtVenueBadge
         marketScope={inferMarketScope(proposal)}
         nxtClassification={recon.nxt_classification}
         nxtEligible={recon.nxt_eligible}
       />
     </>
   ) : null}
   ```
   Define `inferMarketScope` at the bottom of the file:
   ```tsx
   function inferMarketScope(proposal: ProposalDetail): string {
     if (proposal.instrument_type === "equity_kr") return "kr";
     if (proposal.instrument_type === "equity_us") return "us";
     if (proposal.instrument_type === "crypto") return "crypto";
     return "";
   }
   ```

5. Above the existing `ProposalResponseControls` block, conditionally render the warnings + alert + decision-support panel:
   ```tsx
   {recon ? (
     <>
       <WarningChips tokens={recon.warnings} />
       <ReconciliationDecisionSupportPanel
         side={proposal.side}
         originalPrice={proposal.original_price}
         originalQuantity={proposal.original_quantity}
         payload={recon}
       />
       {nonActionable ? (
         <p className={styles.nonActionableAlert} role="alert">
           Non-NXT pending order — KR broker routing only. Review before
           deciding; recording a response on this row does not place or
           cancel a broker order.
         </p>
       ) : null}
     </>
   ) : null}
   ```

   Keep the existing safety note (`<p className={styles.safetyNote}>...`) as-is for the actionable case; when `nonActionable` is true, the new alert above conveys the warning.

- [ ] **Step 4: Modify `ProposalRow.module.css` to add the variants.**

Append to `ProposalRow.module.css`:
```css
.nonActionable {
  background: #fff7ed;
  border-color: #f5cf91;
}

.nonActionableAlert {
  background: #fff3cd;
  border: 1px solid #ffe08a;
  border-radius: 8px;
  color: #6b3f00;
  font-size: 0.92rem;
  font-weight: 600;
  margin: 0;
  padding: 10px 12px;
}
```

- [ ] **Step 5: Run; confirm pass.**

```bash
cd frontend/trading-decision && npm test -- --run ProposalRow.test.tsx
```
Expected: PASS for all existing + new tests.

- [ ] **Step 6: Commit.**

```bash
git add frontend/trading-decision/src/components/ProposalRow.tsx \
        frontend/trading-decision/src/components/ProposalRow.module.css \
        frontend/trading-decision/src/__tests__/ProposalRow.test.tsx
git commit -m "$(cat <<'EOF'
feat(rob-27): wire reconciliation/NXT badges and non-actionable variant into ProposalRow

Co-Authored-By: Paperclip <noreply@paperclip.ing>
EOF
)"
```

---

### Task 9 — Render structured Research Run summary in `MarketBriefPanel`

**Files:**
- Modify: `frontend/trading-decision/src/components/MarketBriefPanel.tsx`
- Modify: `frontend/trading-decision/src/components/MarketBriefPanel.module.css`
- Modify: `frontend/trading-decision/src/__tests__/SessionDetailPage.test.tsx`

- [ ] **Step 1: Add a failing assertion to `SessionDetailPage.test.tsx`.**

In the existing "shows market brief and proposals" test, after the existing assertions, add:
```tsx
expect(screen.getByText(/Research run/)).toBeInTheDocument();
expect(screen.getByText(/Reconciliation summary/)).toBeInTheDocument();
expect(screen.getByText(/Maintain: 1/)).toBeInTheDocument();
expect(screen.getByText(/Near fill: 1/)).toBeInTheDocument();
expect(screen.getByText(/KR broker only: 1/)).toBeInTheDocument();
```

And update the fixture in that test to use the structured market brief:
```tsx
import { makeResearchRunMarketBrief, makeSessionDetail } from "../test/fixtures";
```
And in the `mockFetch` for the session, change the response body to:
```tsx
new Response(
  JSON.stringify(
    makeSessionDetail({ market_brief: makeResearchRunMarketBrief() }),
  ),
);
```

- [ ] **Step 2: Run; confirm fail.**

```bash
cd frontend/trading-decision && npm test -- --run SessionDetailPage.test.tsx
```
Expected: FAIL — structured fields not rendered.

- [ ] **Step 3: Update `MarketBriefPanel.tsx`.**

```tsx
import styles from "./MarketBriefPanel.module.css";

interface MarketBriefPanelProps {
  brief: Record<string, unknown> | null;
  notes: string | null;
}

interface ResearchRunSummary {
  research_run_uuid: string | null;
  refreshed_at: string | null;
  counts: { candidates: number | null; reconciliations: number | null } | null;
  reconciliation_summary: Record<string, number> | null;
  nxt_summary: Record<string, number> | null;
  snapshot_warnings: string[];
  source_warnings: string[];
}

const RECON_LABEL: Record<string, string> = {
  maintain: "Maintain",
  near_fill: "Near fill",
  too_far: "Too far",
  chasing_risk: "Chasing risk",
  data_mismatch: "Data mismatch",
  kr_pending_non_nxt: "KR broker only",
  unknown_venue: "Unknown venue",
  unknown: "Unknown",
};

const NXT_LABEL: Record<string, string> = {
  actionable: "Actionable",
  too_far: "Too far",
  non_nxt: "Non-NXT",
  watch_only: "Watch only",
  data_mismatch_requires_review: "Review needed",
  unknown: "Unknown",
};

function tryParseSummary(brief: Record<string, unknown>): ResearchRunSummary | null {
  if (!("research_run_uuid" in brief)) return null;
  const counts = brief.counts;
  return {
    research_run_uuid:
      typeof brief.research_run_uuid === "string"
        ? brief.research_run_uuid
        : null,
    refreshed_at:
      typeof brief.refreshed_at === "string" ? brief.refreshed_at : null,
    counts:
      counts && typeof counts === "object"
        ? {
            candidates: numberOrNull(
              (counts as Record<string, unknown>).candidates,
            ),
            reconciliations: numberOrNull(
              (counts as Record<string, unknown>).reconciliations,
            ),
          }
        : null,
    reconciliation_summary: numberMap(brief.reconciliation_summary),
    nxt_summary: numberMap(brief.nxt_summary),
    snapshot_warnings: stringArray(brief.snapshot_warnings),
    source_warnings: stringArray(brief.source_warnings),
  };
}

function numberOrNull(v: unknown): number | null {
  return typeof v === "number" && Number.isFinite(v) ? v : null;
}

function numberMap(v: unknown): Record<string, number> | null {
  if (!v || typeof v !== "object") return null;
  const out: Record<string, number> = {};
  for (const [k, raw] of Object.entries(v as Record<string, unknown>)) {
    if (typeof raw === "number" && Number.isFinite(raw)) out[k] = raw;
  }
  return Object.keys(out).length ? out : null;
}

function stringArray(v: unknown): string[] {
  return Array.isArray(v) ? v.filter((x): x is string => typeof x === "string") : [];
}

export default function MarketBriefPanel({ brief, notes }: MarketBriefPanelProps) {
  if (brief === null && notes === null) return null;
  const summary = brief ? tryParseSummary(brief) : null;
  return (
    <details className={styles.panel} open>
      <summary>Market brief</summary>
      {notes ? <p className={styles.notes}>{notes}</p> : null}
      {summary ? (
        <div className={styles.summary}>
          <p>
            <strong>Research run:</strong>{" "}
            {summary.research_run_uuid ?? "—"}
            {summary.refreshed_at ? ` · refreshed ${summary.refreshed_at}` : ""}
          </p>
          {summary.counts ? (
            <p>
              <strong>Counts:</strong> candidates {summary.counts.candidates ?? "—"} ·
              reconciliations {summary.counts.reconciliations ?? "—"}
            </p>
          ) : null}
          {summary.reconciliation_summary ? (
            <SummaryList
              title="Reconciliation summary"
              entries={summary.reconciliation_summary}
              labels={RECON_LABEL}
            />
          ) : null}
          {summary.nxt_summary ? (
            <SummaryList
              title="NXT summary"
              entries={summary.nxt_summary}
              labels={NXT_LABEL}
            />
          ) : null}
          {summary.snapshot_warnings.length > 0 ? (
            <p>
              <strong>Snapshot warnings:</strong>{" "}
              {summary.snapshot_warnings.join(", ")}
            </p>
          ) : null}
          {summary.source_warnings.length > 0 ? (
            <p>
              <strong>Source warnings:</strong>{" "}
              {summary.source_warnings.join(", ")}
            </p>
          ) : null}
        </div>
      ) : brief ? (
        <pre>{JSON.stringify(brief, null, 2)}</pre>
      ) : null}
    </details>
  );
}

function SummaryList({
  title,
  entries,
  labels,
}: {
  title: string;
  entries: Record<string, number>;
  labels: Record<string, string>;
}) {
  return (
    <div>
      <strong>{title}</strong>
      <ul className={styles.summaryList}>
        {Object.entries(entries).map(([k, v]) => (
          <li key={k}>
            {labels[k] ?? k}: {v}
          </li>
        ))}
      </ul>
    </div>
  );
}
```

- [ ] **Step 4: Update `MarketBriefPanel.module.css`.**

Append:
```css
.summary {
  display: grid;
  font-size: 0.92rem;
  gap: 6px;
}

.summaryList {
  display: flex;
  flex-wrap: wrap;
  gap: 6px 14px;
  list-style: none;
  margin: 4px 0 0;
  padding: 0;
}
```

- [ ] **Step 5: Run; confirm pass.**

```bash
cd frontend/trading-decision && npm test -- --run SessionDetailPage.test.tsx
```
Expected: PASS.

- [ ] **Step 6: Commit.**

```bash
git add frontend/trading-decision/src/components/MarketBriefPanel.tsx \
        frontend/trading-decision/src/components/MarketBriefPanel.module.css \
        frontend/trading-decision/src/__tests__/SessionDetailPage.test.tsx
git commit -m "$(cat <<'EOF'
feat(rob-27): structured research-run market brief summary in MarketBriefPanel

Co-Authored-By: Paperclip <noreply@paperclip.ing>
EOF
)"
```

---

### Task 10 — Forbidden-mutation safety greps (frontend)

**Files:**
- Create: `frontend/trading-decision/src/__tests__/forbidden_mutation_imports.test.ts`

- [ ] **Step 1: Write the test.**

```ts
import { readFileSync, readdirSync, statSync } from "node:fs";
import { join, resolve } from "node:path";
import { describe, expect, it } from "vitest";

const SRC = resolve(__dirname, "..");
const FORBIDDEN_PATTERNS = [
  /\bdangerouslySetInnerHTML\b/,
  /\binnerHTML\b/,
  /\bplace_order\b/,
  /\bcancel_order\b/,
  /\bmodify_order\b/,
  /\bmanage_watch_alerts\b/,
  /\bpaper_order_handler\b/,
  /\bkis_trading_service\b/,
  /\bfill_notification\b/,
];

function* walk(dir: string): Generator<string> {
  for (const entry of readdirSync(dir)) {
    const full = join(dir, entry);
    const s = statSync(full);
    if (s.isDirectory()) {
      yield* walk(full);
    } else if (/\.(ts|tsx|css)$/.test(entry)) {
      yield full;
    }
  }
}

describe("forbidden mutation imports / unsafe rendering", () => {
  it("no source file uses dangerous HTML or trading-mutation symbols", () => {
    const violations: string[] = [];
    for (const file of walk(SRC)) {
      // Skip the safety test itself so its pattern strings don't self-match.
      if (file.endsWith("forbidden_mutation_imports.test.ts")) continue;
      const content = readFileSync(file, "utf8");
      for (const re of FORBIDDEN_PATTERNS) {
        if (re.test(content)) violations.push(`${file}: ${re}`);
      }
    }
    expect(violations).toEqual([]);
  });
});
```

- [ ] **Step 2: Run; confirm pass.**

```bash
cd frontend/trading-decision && npm test -- --run forbidden_mutation_imports.test.ts
```
Expected: PASS (no existing source files reference these).

- [ ] **Step 3: Commit.**

```bash
git add frontend/trading-decision/src/__tests__/forbidden_mutation_imports.test.ts
git commit -m "$(cat <<'EOF'
test(rob-27): forbid dangerouslySetInnerHTML/innerHTML and broker-mutation symbols in trading-decision frontend

Co-Authored-By: Paperclip <noreply@paperclip.ing>
EOF
)"
```

---

### Task 11 — Backend safety: confirm trading_decisions router is unchanged

**Files:** none (verification only).

- [ ] **Step 1: Run the existing safety tests.**

```bash
uv run pytest tests/test_trading_decisions_router_safety.py \
              tests/test_research_run_decision_session_router_safety.py \
              tests/test_research_run_decision_session_service_safety.py \
              tests/test_pending_reconciliation_service_safety.py \
              tests/test_nxt_classifier_service_safety.py \
              -v
```
Expected: all green. This confirms ROB-27 did not introduce a backend mutation import.

- [ ] **Step 2: Run forbidden-mutation grep against `app/`.**

```bash
git diff --name-only origin/main...HEAD | grep '^app/' || echo "no app/ changes"
```
Expected: `no app/ changes` (this issue is frontend-only). If anything appears, stop and re-evaluate — the plan says backend is untouched.

- [ ] **Step 3: No commit (no edits).**

---

### Task 12 — Optional reference doc on the `original_payload` shape

**Files:**
- Modify: `app/schemas/research_run_decision_session.py` (comment-only change at the bottom of the file)

- [ ] **Step 1: Append a docstring block at the bottom of the schema file.**

Append (do **not** edit existing classes):
```python
# ---------------------------------------------------------------------------
# Reference: shape of `proposal.original_payload` for proposals derived from a
# research run. Built by `app.services.research_run_decision_session_service.
# _proposal_payload`. Consumed (read-only) by the trading-decision SPA in
# `frontend/trading-decision/src/api/reconciliation.ts`.
#
# {
#   "advisory_only": True,
#   "execution_allowed": False,
#   "research_run_id": "<uuid>",
#   "research_run_candidate_id": <int>,
#   "refreshed_at": "<iso8601>",
#   "candidate_kind": "pending_order|holding|screener_hit|proposed|other",
#   "pending_order_id": "<order_id>|None",
#   "reconciliation_status": "<ReconClassificationLiteral>|None",
#   "reconciliation_summary": "<str>|None",
#   "nxt_classification": "<NxtClassificationLiteral>|None",
#   "nxt_summary": "<str>|None",
#   "nxt_eligible": True | False | None,
#   "venue_eligibility": {"nxt": True|False|None, "regular": True|None},
#   "live_quote": {"price": "<decimal>", "as_of": "<iso8601>"} | None,
#   "decision_support": { "current_price": ..., "gap_pct": ..., ... },
#   "warnings": ["str", ...]
# }
# ---------------------------------------------------------------------------
```

- [ ] **Step 2: Lint + typecheck.**

```bash
uv run ruff check app/ tests/
uv run ruff format --check app/ tests/
uv run ty check app/ --error-on-warning
```
Expected: clean (the change is comment-only).

- [ ] **Step 3: Commit.**

```bash
git add app/schemas/research_run_decision_session.py
git commit -m "$(cat <<'EOF'
docs(rob-27): document original_payload shape for the trading-decision SPA

Co-Authored-By: Paperclip <noreply@paperclip.ing>
EOF
)"
```

---

### Task 13 — Quality gate sweep + smoke

**Files:** none.

- [ ] **Step 1: Frontend full test.**

```bash
cd frontend/trading-decision && npm test -- --run
```
Expected: all green; no `console.error` complaints from React Testing Library.

- [ ] **Step 2: Frontend typecheck + build.**

```bash
cd frontend/trading-decision && npm run typecheck && npm run build
```
Expected: clean.

- [ ] **Step 3: Backend gates.**

```bash
uv run ruff check app/ tests/
uv run ruff format --check app/ tests/
uv run ty check app/ --error-on-warning
uv run pytest tests/ -m "not live"
```
Expected: clean (no behavioral change to backend; only Task 12's comment).

- [ ] **Step 4: UI smoke (optional, manual).**

`make dev` (uvicorn at port 8000), open `http://localhost:8000/trading/sessions/<uuid>` for a session created from a research run; eyeball the new badges, NXT badge, warnings, and decision-support panel. Capture a screenshot and attach to the PR description (no live trading involved). If you cannot generate a research-run-derived session in dev, say so explicitly in the PR description rather than claiming success.

---

### Task 14 — Open PR

**Files:** none.

- [ ] **Step 1: Push branch.**

```bash
git push -u origin feature/ROB-27-ui-reconciliation-badges-venue-warnings
```

- [ ] **Step 2: Open the PR against `main`.**

```bash
gh pr create --base main \
  --title "ROB-27: UI reconciliation badges and venue warnings" \
  --body "$(cat <<'EOF'
## Summary
- Surface reconciliation classification, NXT venue eligibility, and decision-support metadata persisted by ROB-22/23/25 directly in the trading-decision SPA.
- Add `ReconciliationBadge` (maintain / near_fill / too_far / chasing_risk / data_mismatch / kr_pending_non_nxt / unknown_venue / unknown) and `NxtVenueBadge` (NXT actionable / not actionable / Non-NXT / unknown / review needed) to each proposal row.
- Add a `ReconciliationDecisionSupportPanel` that renders pending side / price / qty, live quote, gap to current price, distance to fill, nearest support / resistance, and bid/ask spread.
- Render `original_payload.warnings` as friendly chips through an allowlist (`missing_quote`, `stale_quote`, `non_nxt_venue`, `missing_kr_universe`, …).
- Mark non-NXT pending and `data_mismatch_requires_review` rows as **non-actionable**: muted background, alert banner, response controls remain enabled so the operator can ledger a decision.
- Promote the session `market_brief` JSON dump to a structured Research Run summary (research_run_uuid, refreshed_at, counts, reconciliation_summary, nxt_summary, snapshot_warnings, source_warnings) with a JSON fallback for older briefs.

## Trading-safety
- Read-only / decision-support only. **No** `place_order`, `modify_order`, `cancel_order`, `manage_watch_alerts`, broker placement, paper, dry-run, or live order paths are introduced.
- Frontend safety test (`forbidden_mutation_imports.test.ts`) forbids `dangerouslySetInnerHTML`, `innerHTML`, and any literal reference to broker-mutation symbols in `frontend/trading-decision/src/`.
- All untrusted payload values are coerced through known string parsers; classifications and warning tokens are allowlisted before being mapped to CSS class names. No HTML is constructed from server-supplied strings.
- Backend untouched except a comment-only doc block in `app/schemas/research_run_decision_session.py`. Existing import-safety tests continue to pass.

## Out of scope
- New routes / endpoints. (Decision Session creation from a research run already exists at `POST /trading/api/decisions/from-research-run`.)
- Persisting reconciliation summaries beyond what ROB-22/23/25 already store.
- TradingAgents advisory rendering (still gated by ROB-26 follow-up).

## Test plan
- [ ] `cd frontend/trading-decision && npm test -- --run`
- [ ] `cd frontend/trading-decision && npm run typecheck && npm run build`
- [ ] `uv run ruff check app/ tests/`
- [ ] `uv run ruff format --check app/ tests/`
- [ ] `uv run ty check app/ --error-on-warning`
- [ ] `uv run pytest tests/ -m "not live"`

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

- [ ] **Step 3: Capture the PR URL in the AoE session log.**

---

## Forbidden-mutation Safety Checks (greps)

Run before opening the PR (Task 11 covers Step 1; the rest are PR-time spot checks):

```bash
git diff origin/main...HEAD -- app/ | grep -nE 'place_order|modify_order|cancel_order|manage_watch_alerts|kis_trading_service|kis_trading_contracts|fill_notification|execution_event|paper_order_handler' \
  && echo "FAIL: forbidden mutation symbol introduced in app/" || echo "OK: no forbidden mutation symbol"

git diff origin/main...HEAD -- frontend/ | grep -nE 'dangerouslySetInnerHTML|innerHTML|place_order|cancel_order|modify_order' \
  && echo "FAIL: dangerous HTML or mutation symbol in frontend" || echo "OK: frontend is clean"
```

Both should print `OK: ...`.

---

## Risks and Mitigations

| Risk | Mitigation |
|---|---|
| ROB-25 changes the `original_payload` shape after this PR is merged. | The TypeScript parser tolerates missing fields and falls back to `unknown`/`null`. Tests cover the absent-fields case. |
| Server-supplied class names could be injected via the classification field. | Classifications and NXT labels are allowlisted before they're used as CSS class names. Test in Task 1 covers an `<script>` value. |
| Warning tokens could be used to inject content. | Token regex (`^[a-z][a-z0-9_]{0,63}$`) drops anything outside ASCII lowercase / digits / underscore; the renderer only emits them as text content. Test in Task 6 covers `<script>` and `Foo Bar`. |
| Operator inadvertently triggers a broker order from this UI. | We don't add any new mutation path; existing controls plumbing is unchanged; non-actionable banner reminds the operator that response is ledger-only. |
| `MarketBriefPanel` regression for older sessions whose brief is unstructured. | The structured renderer only activates when `research_run_uuid` is present in the brief; otherwise we keep the existing `<pre>{JSON.stringify(...)}</pre>` fallback. |
| `ProposalRow` test churn. | New tests are appended; existing tests remain green. The `nonActionable` variant is keyed off `(proposal.proposal_kind === "other" && candidate_kind === "pending_order" && ...)`, so existing fixtures (which leave `original_payload` empty) remain unaffected. |

---

## Handoff Instructions for Sonnet Implementer

The plan is task-numbered (`Task 1` … `Task 14`). Use **superpowers:subagent-driven-development** if you want a fresh subagent per task with review between tasks; use **superpowers:executing-plans** for inline batched execution. Either way, follow tasks **in order** — Task 8 depends on the components from Tasks 1, 4, 5, 6, 7. Specifically:

1. **Tasks 1–3** (types/parser/format/fixtures) are pure utility work; review for type-safety and the warning-token allowlist before moving on.
2. **Tasks 4–7** create the four new components. They are co-located with their test files. Each is a fresh-context unit that should be writable in one batch by a subagent.
3. **Task 8** is the ProposalRow integration. **Read the current `ProposalRow.tsx`** before editing — line numbers in the plan are guidance, not hard references; match the existing JSX structure.
4. **Task 9** updates `MarketBriefPanel`. The existing fallback (`<pre>JSON.stringify(...)</pre>`) must be preserved for non-ResearchRun briefs, so the structured renderer only activates when `research_run_uuid` is present.
5. **Task 10** writes the safety grep test. **Do not** disable or weaken its allowlist if it flags a new file you wrote in Tasks 1–9 — fix the underlying file instead.
6. **Task 11** is verification only; if anything in `app/` shows up as changed beyond Task 12's comment block, stop and re-evaluate.
7. **Tasks 12–14** are documentation, gates, and PR. No new commits beyond Task 12 should land in `app/` from this plan.

Frequent commits (one per task) are the goal. Push only after Task 14 Step 1.

If the existing `original_payload` shape returned by `_proposal_payload` has changed since this plan was written, refresh the JSON shape comment and the parser's `HAS_PAYLOAD_KEYS` set before relying on the new fields. Treat any backend change beyond a comment block as out of scope for ROB-27.
