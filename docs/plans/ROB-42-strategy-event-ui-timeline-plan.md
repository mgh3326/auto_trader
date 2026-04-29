# ROB-42 Strategy Event Timeline & Operator Event Form — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Linear Issue:** ROB-42 — Add strategy event timeline and operator event form to Decision Session UI
**Parent:** ROB-40 — Evolve preopen dashboard into intraday strategy decision ledger
**Builds on:** ROB-41 — Operator-provided market events backend (already merged; API live at `/trading/api/strategy-events`)
**Branch / worktree:** `feature/ROB-42-strategy-event-ui-timeline` at `/Users/mgh3326/work/auto_trader-worktrees/feature-ROB-42-strategy-event-ui-timeline`

**Goal:** First UI slice that lets the operator read and write append-only strategy events on the Trading Decision Session detail page — viewing a session-scoped event timeline and posting `operator_market_event` rows tied to the current `session_uuid`.

**Architecture:** Pure frontend slice. New API client module `frontend/trading-decision/src/api/strategyEvents.ts` wraps the existing ROB-41 endpoints (`GET /trading/api/strategy-events?session_uuid=<uuid>` and `POST /trading/api/strategy-events`). New hook `useStrategyEvents` follows the existing `useSessionAnalytics` / `useDecisionSession` state-machine pattern (`idle | loading | success | error | not_found`) and exposes `submit` + `refetch`. Two new presentational components — `StrategyEventTimeline` (list / empty / error) and `OperatorEventForm` (compact create form) — are mounted inside `SessionDetailPage` between the analytics matrix and proposals sections. Tests use the established `mockFetch` + Testing Library + Vitest stack.

**Tech Stack:** TypeScript 6, React 19, react-router-dom 7, Vitest 4, @testing-library/react 16, jsdom 29, Vite 8.

**Hard out-of-scope guardrails (DO NOT cross):**
- No broker / KIS / Upbit / order / watch / paper / live-execution API calls — direct or transitive.
- No order intent creation, no proposal mutation, no proposal-response trigger.
- No strategy revision auto-create / auto-mutate.
- No TradingAgents advisory integration in this slice.
- No backend code changes. The ROB-41 router/service/schema is already complete and live; only consume it. If a backend bug is uncovered, surface it and stop — do not fix in this PR.
- No new top-level pages, no new routes — UI slice is contained inside `SessionDetailPage`.
- The forbidden-import safety test at `frontend/trading-decision/src/__tests__/forbidden_mutation_imports.test.ts` MUST keep passing (don't even mention `place_order`, `kis_trading_service`, `paper_order_handler`, `manage_watch_alerts`, etc., as substrings in source files).

---

## Backend Contract Reference (DO NOT MODIFY)

These already exist on `main` from ROB-41 — this plan only consumes them.

### `GET /trading/api/strategy-events?session_uuid=<uuid>&limit=<n>&offset=<n>&mine=<bool>`
Returns `StrategyEventListResponse`:
```ts
{
  events: StrategyEventDetail[];
  total: number;
  limit: number;   // default 50, max 200
  offset: number;  // default 0
}
```
- 401 if unauthenticated
- 404 with `{detail: "session_uuid_not_found"}` if `session_uuid` is provided but no session matches

### `POST /trading/api/strategy-events`
Body: `StrategyEventCreateRequest`:
```ts
{
  source: "user";                 // literal — operator UI always sends "user"
  event_type:                     // ROB-42 only sends "operator_market_event"
    | "operator_market_event"
    | "earnings_event"
    | "macro_event"
    | "sector_rotation"
    | "technical_break"
    | "risk_veto"
    | "cash_budget_change"
    | "position_change";
  source_text: string;            // required, 1..8000 chars
  normalized_summary?: string;    // optional, max 2000
  session_uuid?: string;          // UUID — ROB-42 always sends current session
  affected_markets?: string[];    // each entry max 64 chars, list max 32
  affected_sectors?: string[];    // each entry max 64 chars, list max 32
  affected_themes?: string[];     // each entry max 64 chars, list max 32
  affected_symbols?: string[];    // each entry max 32 chars, list max 64
  severity?: number;              // 1..5, default 2
  confidence?: number;            // 0..100, default 50
  metadata?: Record<string, unknown>;
}
```
Returns `201` with `StrategyEventDetail`:
```ts
{
  id: number;
  event_uuid: string;          // UUID
  session_uuid: string | null; // UUID, echoed back
  source: "user" | "hermes" | "tradingagents" | "news" | "market_data" | "scheduler";
  event_type: <see above>;
  source_text: string;
  normalized_summary: string | null;
  affected_markets: string[];
  affected_sectors: string[];
  affected_themes: string[];
  affected_symbols: string[];
  severity: number;            // 1..5
  confidence: number;           // 0..100
  created_by_user_id: number | null;
  metadata: Record<string, unknown> | null;
  created_at: string;          // ISO datetime
}
```
- 401 if unauthenticated
- 404 with `{detail: "session_uuid_not_found"}` for unknown `session_uuid`
- 422 for schema-validation failures

Authoritative sources: `app/routers/strategy_events.py`, `app/schemas/strategy_events.py`, `tests/routers/test_strategy_events_router.py`.

---

## File Structure

| Path | Action | Responsibility |
|---|---|---|
| `frontend/trading-decision/src/api/types.ts` | modify | Append types: `StrategyEventSource`, `StrategyEventType`, `StrategyEventDetail`, `StrategyEventListResponse`, `StrategyEventCreateRequest`. |
| `frontend/trading-decision/src/api/strategyEvents.ts` | create | API client: `getStrategyEvents({ sessionUuid, limit?, offset? })`, `createStrategyEvent(body)`. Uses `apiFetch` from `client.ts`. |
| `frontend/trading-decision/src/__tests__/api.strategyEvents.test.ts` | create | Vitest unit tests for the client (URL building, body, only `/trading/api/...`). |
| `frontend/trading-decision/src/test/fixtures.ts` | modify | Append `makeStrategyEvent()` and `makeStrategyEventListResponse()` factories. |
| `frontend/trading-decision/src/hooks/useStrategyEvents.ts` | create | State-machine hook (`idle`/`loading`/`success`/`error`/`not_found`) returning `{ status, data, error, refetch, submit }`. |
| `frontend/trading-decision/src/components/StrategyEventTimeline.tsx` | create | Presentational timeline list, empty state, severity/confidence badges. |
| `frontend/trading-decision/src/components/StrategyEventTimeline.module.css` | create | Timeline styles. |
| `frontend/trading-decision/src/__tests__/StrategyEventTimeline.test.tsx` | create | Render tests: events shown, empty state, severity/confidence visible, symbols visible. |
| `frontend/trading-decision/src/components/OperatorEventForm.tsx` | create | Compact create form: source_text textarea (required), comma-separated symbols input, severity (1–5), confidence (0–100); always submits `source: "user"` + `event_type: "operator_market_event"` + provided `session_uuid`. |
| `frontend/trading-decision/src/components/OperatorEventForm.module.css` | create | Form styles. |
| `frontend/trading-decision/src/__tests__/OperatorEventForm.test.tsx` | create | Form tests: required validation, comma-split parsing, default severity/confidence, success clears textarea, error surfaces and does not mutate proposals. |
| `frontend/trading-decision/src/pages/SessionDetailPage.tsx` | modify | Wire `useStrategyEvents(session_uuid)` and render `<OperatorEventForm>` + `<StrategyEventTimeline>` between Analytics and Proposals sections. |
| `frontend/trading-decision/src/pages/SessionDetailPage.module.css` | modify | Add `.strategyEvents` block styling (gap rule). |
| `frontend/trading-decision/src/__tests__/SessionDetailPage.test.tsx` | modify | Append integration tests: timeline renders for session, empty state, submit POSTs `operator_market_event` with current `session_uuid`, optimistic refresh, API error surfaces without mutating proposals. |

No backend file changes. No new routes. No new top-level pages.

---

## Test plan summary

**Required vitest assertions (per ROB-42 acceptance):**
1. `StrategyEventTimeline` renders session-scoped events with type, severity, confidence, symbols, and created timestamp.
2. Empty state renders when the API returns `events: []`.
3. `OperatorEventForm` submit POSTs `source: "user"`, `event_type: "operator_market_event"`, current `session_uuid`, and trimmed `source_text`.
4. Successful submit refetches (or appends) so the new event appears in the timeline.
5. Submit-time API error is surfaced as an `role="alert"` message and does NOT trigger any proposal/order mutation request.

**Test commands run after implementation (Task 9):**
```bash
cd frontend/trading-decision
npm run test -- src/__tests__/api.strategyEvents.test.ts
npm run test -- src/__tests__/StrategyEventTimeline.test.tsx
npm run test -- src/__tests__/OperatorEventForm.test.tsx
npm run test -- src/__tests__/SessionDetailPage.test.tsx
npm run test -- src/__tests__/forbidden_mutation_imports.test.ts
npm run test
npm run typecheck
npm run build
```

If a step touches the backend, also run:
```bash
uv run pytest tests/routers/test_strategy_events_router.py tests/services/test_strategy_event_service.py -v
```
(Not expected — but listed for reviewer reference.)

---

## Tasks

### Task 1: Add strategy-event types

**Files:**
- Modify: `frontend/trading-decision/src/api/types.ts` (append at end of file)

- [ ] **Step 1: Append the types**

Open `frontend/trading-decision/src/api/types.ts` and append at the end of the file:

```ts
// Strategy events (ROB-41 backend, ROB-42 UI)
export type StrategyEventSource =
  | "user"
  | "hermes"
  | "tradingagents"
  | "news"
  | "market_data"
  | "scheduler";

export type StrategyEventType =
  | "operator_market_event"
  | "earnings_event"
  | "macro_event"
  | "sector_rotation"
  | "technical_break"
  | "risk_veto"
  | "cash_budget_change"
  | "position_change";

export interface StrategyEventDetail {
  id: number;
  event_uuid: Uuid;
  session_uuid: Uuid | null;
  source: StrategyEventSource;
  event_type: StrategyEventType;
  source_text: string;
  normalized_summary: string | null;
  affected_markets: string[];
  affected_sectors: string[];
  affected_themes: string[];
  affected_symbols: string[];
  severity: number;
  confidence: number;
  created_by_user_id: number | null;
  metadata: Record<string, unknown> | null;
  created_at: IsoDateTime;
}

export interface StrategyEventListResponse {
  events: StrategyEventDetail[];
  total: number;
  limit: number;
  offset: number;
}

export interface StrategyEventCreateRequest {
  source: "user";
  event_type: StrategyEventType;
  source_text: string;
  normalized_summary?: string;
  session_uuid?: Uuid;
  affected_markets?: string[];
  affected_sectors?: string[];
  affected_themes?: string[];
  affected_symbols?: string[];
  severity?: number;
  confidence?: number;
  metadata?: Record<string, unknown>;
}
```

- [ ] **Step 2: Run typecheck to confirm no broken imports**

Run from `frontend/trading-decision/`:
```bash
npm run typecheck
```
Expected: PASS (no new files reference these types yet — this just confirms the additions are well-formed).

- [ ] **Step 3: Commit**

```bash
git add frontend/trading-decision/src/api/types.ts
git commit -m "feat(ui): add strategy event TS types for ROB-42"
```

---

### Task 2: Add strategy-events API client (TDD)

**Files:**
- Create: `frontend/trading-decision/src/__tests__/api.strategyEvents.test.ts`
- Create: `frontend/trading-decision/src/api/strategyEvents.ts`

- [ ] **Step 1: Write failing test for `getStrategyEvents` URL + parsing**

Create `frontend/trading-decision/src/__tests__/api.strategyEvents.test.ts`:

```ts
import { afterEach, describe, expect, it, vi } from "vitest";
import {
  createStrategyEvent,
  getStrategyEvents,
} from "../api/strategyEvents";
import { mockFetch } from "../test/server";

describe("strategyEvents API client", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("getStrategyEvents builds query string with session_uuid", async () => {
    const { calls } = mockFetch({
      "/trading/api/strategy-events?session_uuid=session-1&limit=50&offset=0":
        () =>
          new Response(
            JSON.stringify({
              events: [],
              total: 0,
              limit: 50,
              offset: 0,
            }),
          ),
    });

    const data = await getStrategyEvents({ sessionUuid: "session-1" });

    expect(data.total).toBe(0);
    expect(calls[0]?.method).toBe("GET");
    expect(calls[0]?.url).toContain(
      "/trading/api/strategy-events?session_uuid=session-1&limit=50&offset=0",
    );
  });

  it("getStrategyEvents passes custom limit/offset", async () => {
    const { calls } = mockFetch({
      "/trading/api/strategy-events?session_uuid=session-1&limit=25&offset=10":
        () =>
          new Response(
            JSON.stringify({
              events: [],
              total: 0,
              limit: 25,
              offset: 10,
            }),
          ),
    });

    await getStrategyEvents({
      sessionUuid: "session-1",
      limit: 25,
      offset: 10,
    });

    expect(calls[0]?.url).toContain("limit=25&offset=10");
  });

  it("createStrategyEvent POSTs body and parses StrategyEventDetail", async () => {
    const { calls } = mockFetch({
      "/trading/api/strategy-events": () =>
        new Response(
          JSON.stringify({
            id: 1,
            event_uuid: "ev-1",
            session_uuid: "session-1",
            source: "user",
            event_type: "operator_market_event",
            source_text: "OpenAI earnings miss",
            normalized_summary: null,
            affected_markets: [],
            affected_sectors: [],
            affected_themes: [],
            affected_symbols: ["MSFT", "NVDA"],
            severity: 3,
            confidence: 60,
            created_by_user_id: 7,
            metadata: null,
            created_at: "2026-04-29T01:00:00Z",
          }),
          { status: 201 },
        ),
    });

    const result = await createStrategyEvent({
      source: "user",
      event_type: "operator_market_event",
      source_text: "OpenAI earnings miss",
      session_uuid: "session-1",
      affected_symbols: ["MSFT", "NVDA"],
      severity: 3,
      confidence: 60,
    });

    expect(result.event_uuid).toBe("ev-1");
    expect(result.source).toBe("user");
    expect(result.event_type).toBe("operator_market_event");
    expect(calls[0]?.method).toBe("POST");
    const body = JSON.parse(calls[0]?.body ?? "{}");
    expect(body.source).toBe("user");
    expect(body.event_type).toBe("operator_market_event");
    expect(body.session_uuid).toBe("session-1");
    expect(body.source_text).toBe("OpenAI earnings miss");
    expect(body.affected_symbols).toEqual(["MSFT", "NVDA"]);
    expect(body.severity).toBe(3);
    expect(body.confidence).toBe(60);
  });

  it("only calls /trading/api paths", async () => {
    const { calls } = mockFetch({
      "/trading/api/strategy-events?session_uuid=session-1&limit=50&offset=0":
        () =>
          new Response(
            JSON.stringify({
              events: [],
              total: 0,
              limit: 50,
              offset: 0,
            }),
          ),
    });

    await getStrategyEvents({ sessionUuid: "session-1" });

    for (const call of calls) {
      expect(new URL(call.url, "https://example.test").pathname).toMatch(
        /^\/trading\/api\//,
      );
    }
  });
});
```

- [ ] **Step 2: Run the test — expect FAIL with module-not-found**

Run from `frontend/trading-decision/`:
```bash
npm run test -- src/__tests__/api.strategyEvents.test.ts
```
Expected: FAIL — cannot find module `../api/strategyEvents`.

- [ ] **Step 3: Implement the API client**

Create `frontend/trading-decision/src/api/strategyEvents.ts`:

```ts
import { apiFetch } from "./client";
import type {
  StrategyEventCreateRequest,
  StrategyEventDetail,
  StrategyEventListResponse,
  Uuid,
} from "./types";

export interface GetStrategyEventsParams {
  sessionUuid: Uuid;
  limit?: number;
  offset?: number;
}

export function getStrategyEvents(
  params: GetStrategyEventsParams,
): Promise<StrategyEventListResponse> {
  const limit = params.limit ?? 50;
  const offset = params.offset ?? 0;
  const qs = new URLSearchParams();
  qs.set("session_uuid", params.sessionUuid);
  qs.set("limit", String(limit));
  qs.set("offset", String(offset));
  return apiFetch<StrategyEventListResponse>(
    `/strategy-events?${qs.toString()}`,
  );
}

export function createStrategyEvent(
  body: StrategyEventCreateRequest,
): Promise<StrategyEventDetail> {
  return apiFetch<StrategyEventDetail>(`/strategy-events`, {
    method: "POST",
    body: JSON.stringify(body),
  });
}
```

- [ ] **Step 4: Re-run test — expect PASS**

```bash
npm run test -- src/__tests__/api.strategyEvents.test.ts
```
Expected: 4/4 PASS.

- [ ] **Step 5: Run forbidden-import safety test to make sure new code stays clean**

```bash
npm run test -- src/__tests__/forbidden_mutation_imports.test.ts
```
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add frontend/trading-decision/src/api/strategyEvents.ts \
        frontend/trading-decision/src/__tests__/api.strategyEvents.test.ts
git commit -m "feat(ui): add strategy-events API client (ROB-42)"
```

---

### Task 3: Add test fixtures for strategy events

**Files:**
- Modify: `frontend/trading-decision/src/test/fixtures.ts` (append)

- [ ] **Step 1: Add fixture factories**

Append at the end of `frontend/trading-decision/src/test/fixtures.ts`:

```ts
import type {
  StrategyEventDetail,
  StrategyEventListResponse,
} from "../api/types";

export function makeStrategyEvent(
  overrides: Partial<StrategyEventDetail> = {},
): StrategyEventDetail {
  return {
    id: 1,
    event_uuid: "event-uuid-1",
    session_uuid: "session-1",
    source: "user",
    event_type: "operator_market_event",
    source_text: "OpenAI earnings missed expectations",
    normalized_summary: null,
    affected_markets: ["us"],
    affected_sectors: [],
    affected_themes: ["ai"],
    affected_symbols: ["MSFT", "NVDA"],
    severity: 3,
    confidence: 60,
    created_by_user_id: 7,
    metadata: null,
    created_at: now,
    ...overrides,
  };
}

export function makeStrategyEventListResponse(
  overrides: Partial<StrategyEventListResponse> = {},
): StrategyEventListResponse {
  return {
    events: [makeStrategyEvent()],
    total: 1,
    limit: 50,
    offset: 0,
    ...overrides,
  };
}
```

> **Note:** `now` is already declared at the top of `fixtures.ts` (`const now = "2026-04-28T06:00:00Z";`); the import line for `StrategyEventDetail` / `StrategyEventListResponse` should be **merged into the existing top-of-file `import type { ... } from "../api/types";` block** rather than added as a duplicate import statement. Open the file, find the existing block, and add `StrategyEventDetail` and `StrategyEventListResponse` to it.

- [ ] **Step 2: Run typecheck**

```bash
cd frontend/trading-decision && npm run typecheck
```
Expected: PASS.

- [ ] **Step 3: Run the existing test suite to confirm fixtures stay backward-compatible**

```bash
npm run test
```
Expected: all existing tests still PASS.

- [ ] **Step 4: Commit**

```bash
git add frontend/trading-decision/src/test/fixtures.ts
git commit -m "test(ui): add strategy-event fixtures (ROB-42)"
```

---

### Task 4: Add `useStrategyEvents` hook (TDD)

**Files:**
- Create: `frontend/trading-decision/src/hooks/useStrategyEvents.ts`

> **No dedicated hook test file** — the hook is exercised through `StrategyEventTimeline.test.tsx` and `SessionDetailPage.test.tsx` (Tasks 5 and 8). This matches the pattern of `useSessionAnalytics` (no dedicated test, exercised by its consumer).

- [ ] **Step 1: Implement the hook**

Create `frontend/trading-decision/src/hooks/useStrategyEvents.ts`:

```ts
import { useCallback, useEffect, useState } from "react";
import { ApiError } from "../api/client";
import {
  createStrategyEvent,
  getStrategyEvents,
} from "../api/strategyEvents";
import type {
  StrategyEventCreateRequest,
  StrategyEventListResponse,
} from "../api/types";

interface StrategyEventsState {
  status: "idle" | "loading" | "success" | "error" | "not_found";
  data: StrategyEventListResponse | null;
  error: string | null;
}

export interface UseStrategyEventsResult extends StrategyEventsState {
  refetch: () => void;
  submit: (
    body: StrategyEventCreateRequest,
  ) => Promise<{ ok: boolean; status?: number; detail?: string }>;
}

export function useStrategyEvents(
  sessionUuid: string,
): UseStrategyEventsResult {
  const [state, setState] = useState<StrategyEventsState>({
    status: "idle",
    data: null,
    error: null,
  });
  const [version, setVersion] = useState(0);
  const refetch = useCallback(() => setVersion((v) => v + 1), []);

  useEffect(() => {
    if (!sessionUuid) return;
    const controller = new AbortController();
    setState((current) => ({ ...current, status: "loading", error: null }));
    getStrategyEvents({ sessionUuid })
      .then((data) => {
        if (!controller.signal.aborted) {
          setState({ status: "success", data, error: null });
        }
      })
      .catch((error: unknown) => {
        if (controller.signal.aborted) return;
        if (error instanceof ApiError && error.status === 404) {
          setState({
            status: "not_found",
            data: null,
            error: error.detail,
          });
          return;
        }
        setState({
          status: "error",
          data: null,
          error:
            error instanceof ApiError
              ? error.detail
              : "Could not load strategy events.",
        });
      });
    return () => controller.abort();
  }, [sessionUuid, version]);

  async function submit(body: StrategyEventCreateRequest) {
    try {
      await createStrategyEvent(body);
      refetch();
      return { ok: true };
    } catch (error) {
      if (error instanceof ApiError) {
        return { ok: false, status: error.status, detail: error.detail };
      }
      return { ok: false, detail: "Could not submit strategy event." };
    }
  }

  return { ...state, refetch, submit };
}
```

- [ ] **Step 2: Run typecheck**

```bash
cd frontend/trading-decision && npm run typecheck
```
Expected: PASS.

- [ ] **Step 3: Commit**

```bash
git add frontend/trading-decision/src/hooks/useStrategyEvents.ts
git commit -m "feat(ui): add useStrategyEvents hook (ROB-42)"
```

---

### Task 5: Add `StrategyEventTimeline` component (TDD)

**Files:**
- Create: `frontend/trading-decision/src/__tests__/StrategyEventTimeline.test.tsx`
- Create: `frontend/trading-decision/src/components/StrategyEventTimeline.tsx`
- Create: `frontend/trading-decision/src/components/StrategyEventTimeline.module.css`

- [ ] **Step 1: Write the failing test**

Create `frontend/trading-decision/src/__tests__/StrategyEventTimeline.test.tsx`:

```tsx
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import StrategyEventTimeline from "../components/StrategyEventTimeline";
import { makeStrategyEvent } from "../test/fixtures";

describe("StrategyEventTimeline", () => {
  it("renders an empty state when there are no events", () => {
    render(<StrategyEventTimeline events={[]} />);
    expect(
      screen.getByText(/no strategy events yet/i),
    ).toBeInTheDocument();
  });

  it("renders event type, severity, confidence, symbols, and timestamp", () => {
    const event = makeStrategyEvent({
      event_type: "operator_market_event",
      source_text: "OpenAI earnings miss",
      normalized_summary: null,
      severity: 4,
      confidence: 75,
      affected_symbols: ["MSFT", "NVDA"],
      affected_markets: ["us"],
      affected_themes: ["ai"],
      created_at: "2026-04-29T01:30:00Z",
    });
    render(<StrategyEventTimeline events={[event]} />);

    expect(
      screen.getByText(/operator_market_event/i),
    ).toBeInTheDocument();
    expect(screen.getByText(/openai earnings miss/i)).toBeInTheDocument();
    expect(screen.getByText(/severity\s*4/i)).toBeInTheDocument();
    expect(screen.getByText(/confidence\s*75/i)).toBeInTheDocument();
    expect(screen.getByText("MSFT")).toBeInTheDocument();
    expect(screen.getByText("NVDA")).toBeInTheDocument();
    expect(screen.getByText(/us/)).toBeInTheDocument();
    expect(screen.getByText(/ai/)).toBeInTheDocument();
  });

  it("prefers normalized_summary over source_text when present", () => {
    const event = makeStrategyEvent({
      source_text: "raw text body",
      normalized_summary: "polished summary",
    });
    render(<StrategyEventTimeline events={[event]} />);
    expect(screen.getByText(/polished summary/i)).toBeInTheDocument();
    expect(screen.queryByText(/raw text body/i)).not.toBeInTheDocument();
  });
});
```

- [ ] **Step 2: Run the test — expect FAIL (module not found)**

```bash
cd frontend/trading-decision
npm run test -- src/__tests__/StrategyEventTimeline.test.tsx
```
Expected: FAIL — cannot find `../components/StrategyEventTimeline`.

- [ ] **Step 3: Create the CSS module**

Create `frontend/trading-decision/src/components/StrategyEventTimeline.module.css`:

```css
.timeline {
  display: grid;
  gap: 10px;
}

.empty {
  color: #536276;
  font-style: italic;
}

.event {
  border: 1px solid #d6dbe3;
  border-radius: 6px;
  padding: 10px 12px;
  display: grid;
  gap: 6px;
  background: #fafbfd;
}

.eventHeader {
  align-items: center;
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  font-size: 0.85rem;
  color: #36455c;
}

.type {
  background: #eef2f8;
  border-radius: 4px;
  padding: 2px 6px;
  font-weight: 600;
}

.summary {
  margin: 0;
  font-size: 0.95rem;
}

.tags {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  font-size: 0.75rem;
  color: #4b5b73;
}

.tag {
  background: #ffffff;
  border: 1px solid #d6dbe3;
  border-radius: 12px;
  padding: 2px 8px;
}

.meta {
  color: #6a7a93;
  font-size: 0.75rem;
}
```

- [ ] **Step 4: Implement the component**

Create `frontend/trading-decision/src/components/StrategyEventTimeline.tsx`:

```tsx
import { formatDateTime } from "../format/datetime";
import type { StrategyEventDetail } from "../api/types";
import styles from "./StrategyEventTimeline.module.css";

interface StrategyEventTimelineProps {
  events: StrategyEventDetail[];
}

export default function StrategyEventTimeline({
  events,
}: StrategyEventTimelineProps) {
  if (events.length === 0) {
    return (
      <p className={styles.empty}>
        No strategy events yet for this session.
      </p>
    );
  }
  return (
    <ol className={styles.timeline} aria-label="Strategy events">
      {events.map((event) => {
        const summary = event.normalized_summary ?? event.source_text;
        return (
          <li key={event.event_uuid} className={styles.event}>
            <div className={styles.eventHeader}>
              <span className={styles.type}>{event.event_type}</span>
              <span>severity {event.severity}</span>
              <span>confidence {event.confidence}</span>
              <span className={styles.meta}>
                {formatDateTime(event.created_at)}
              </span>
            </div>
            <p className={styles.summary}>{summary}</p>
            {event.affected_symbols.length > 0 ||
            event.affected_markets.length > 0 ||
            event.affected_themes.length > 0 ||
            event.affected_sectors.length > 0 ? (
              <div className={styles.tags}>
                {event.affected_symbols.map((s) => (
                  <span key={`sym-${s}`} className={styles.tag}>
                    {s}
                  </span>
                ))}
                {event.affected_markets.map((m) => (
                  <span key={`mkt-${m}`} className={styles.tag}>
                    {m}
                  </span>
                ))}
                {event.affected_sectors.map((s) => (
                  <span key={`sec-${s}`} className={styles.tag}>
                    {s}
                  </span>
                ))}
                {event.affected_themes.map((t) => (
                  <span key={`thm-${t}`} className={styles.tag}>
                    {t}
                  </span>
                ))}
              </div>
            ) : null}
          </li>
        );
      })}
    </ol>
  );
}
```

- [ ] **Step 5: Re-run the test — expect PASS**

```bash
npm run test -- src/__tests__/StrategyEventTimeline.test.tsx
```
Expected: 3/3 PASS.

- [ ] **Step 6: Commit**

```bash
git add frontend/trading-decision/src/components/StrategyEventTimeline.tsx \
        frontend/trading-decision/src/components/StrategyEventTimeline.module.css \
        frontend/trading-decision/src/__tests__/StrategyEventTimeline.test.tsx
git commit -m "feat(ui): add StrategyEventTimeline component (ROB-42)"
```

---

### Task 6: Add `OperatorEventForm` component (TDD)

**Files:**
- Create: `frontend/trading-decision/src/__tests__/OperatorEventForm.test.tsx`
- Create: `frontend/trading-decision/src/components/OperatorEventForm.tsx`
- Create: `frontend/trading-decision/src/components/OperatorEventForm.module.css`

- [ ] **Step 1: Write the failing test**

Create `frontend/trading-decision/src/__tests__/OperatorEventForm.test.tsx`:

```tsx
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import OperatorEventForm from "../components/OperatorEventForm";

describe("OperatorEventForm", () => {
  it("submits operator_market_event with current session_uuid and trimmed source_text", async () => {
    const onSubmit = vi.fn().mockResolvedValue({ ok: true });
    render(
      <OperatorEventForm sessionUuid="session-1" onSubmit={onSubmit} />,
    );

    await userEvent.type(
      screen.getByLabelText(/source text/i),
      "  OpenAI earnings missed  ",
    );
    await userEvent.click(
      screen.getByRole("button", { name: /add event/i }),
    );

    expect(onSubmit).toHaveBeenCalledTimes(1);
    expect(onSubmit).toHaveBeenCalledWith(
      expect.objectContaining({
        source: "user",
        event_type: "operator_market_event",
        session_uuid: "session-1",
        source_text: "OpenAI earnings missed",
        severity: 2,
        confidence: 50,
      }),
    );
  });

  it("blocks submit when source_text is empty", async () => {
    const onSubmit = vi.fn();
    render(
      <OperatorEventForm sessionUuid="session-1" onSubmit={onSubmit} />,
    );

    await userEvent.click(
      screen.getByRole("button", { name: /add event/i }),
    );

    expect(onSubmit).not.toHaveBeenCalled();
    expect(screen.getByRole("alert")).toHaveTextContent(
      /source text is required/i,
    );
  });

  it("parses comma-separated affected symbols", async () => {
    const onSubmit = vi.fn().mockResolvedValue({ ok: true });
    render(
      <OperatorEventForm sessionUuid="session-1" onSubmit={onSubmit} />,
    );

    await userEvent.type(screen.getByLabelText(/source text/i), "msg");
    await userEvent.type(
      screen.getByLabelText(/affected symbols/i),
      "MSFT, NVDA ,  AAPL",
    );
    await userEvent.click(
      screen.getByRole("button", { name: /add event/i }),
    );

    expect(onSubmit).toHaveBeenCalledWith(
      expect.objectContaining({
        affected_symbols: ["MSFT", "NVDA", "AAPL"],
      }),
    );
  });

  it("clears the textarea after a successful submit", async () => {
    const onSubmit = vi.fn().mockResolvedValue({ ok: true });
    render(
      <OperatorEventForm sessionUuid="session-1" onSubmit={onSubmit} />,
    );

    const textarea = screen.getByLabelText(
      /source text/i,
    ) as HTMLTextAreaElement;
    await userEvent.type(textarea, "abc");
    await userEvent.click(
      screen.getByRole("button", { name: /add event/i }),
    );

    expect(textarea.value).toBe("");
  });

  it("surfaces an error and keeps the form intact when submit fails", async () => {
    const onSubmit = vi.fn().mockResolvedValue({
      ok: false,
      status: 422,
      detail: "validation failed",
    });
    render(
      <OperatorEventForm sessionUuid="session-1" onSubmit={onSubmit} />,
    );

    const textarea = screen.getByLabelText(
      /source text/i,
    ) as HTMLTextAreaElement;
    await userEvent.type(textarea, "abc");
    await userEvent.click(
      screen.getByRole("button", { name: /add event/i }),
    );

    expect(screen.getByRole("alert")).toHaveTextContent(/validation failed/i);
    expect(textarea.value).toBe("abc");
  });

  it("clamps severity to 1..5 and confidence to 0..100", async () => {
    const onSubmit = vi.fn().mockResolvedValue({ ok: true });
    render(
      <OperatorEventForm sessionUuid="session-1" onSubmit={onSubmit} />,
    );

    await userEvent.type(screen.getByLabelText(/source text/i), "msg");
    const severity = screen.getByLabelText(/severity/i) as HTMLInputElement;
    await userEvent.clear(severity);
    await userEvent.type(severity, "9");
    const confidence = screen.getByLabelText(
      /confidence/i,
    ) as HTMLInputElement;
    await userEvent.clear(confidence);
    await userEvent.type(confidence, "150");
    await userEvent.click(
      screen.getByRole("button", { name: /add event/i }),
    );

    expect(onSubmit).toHaveBeenCalledWith(
      expect.objectContaining({ severity: 5, confidence: 100 }),
    );
  });
});
```

- [ ] **Step 2: Run the test — expect FAIL (module not found)**

```bash
cd frontend/trading-decision
npm run test -- src/__tests__/OperatorEventForm.test.tsx
```
Expected: FAIL — cannot find `../components/OperatorEventForm`.

- [ ] **Step 3: Create the CSS module**

Create `frontend/trading-decision/src/components/OperatorEventForm.module.css`:

```css
.form {
  display: grid;
  grid-template-columns: repeat(2, minmax(140px, 1fr));
  gap: 0.5rem 1rem;
  border: 1px solid #d6dbe3;
  border-radius: 6px;
  padding: 12px;
  background: #ffffff;
}

.form label {
  display: flex;
  flex-direction: column;
  gap: 0.25rem;
  font-size: 0.85rem;
}

.fullWidth {
  grid-column: 1 / -1;
}

.form textarea {
  font-family: inherit;
  font-size: 0.95rem;
  min-height: 4rem;
  padding: 0.4rem;
}

.form button {
  grid-column: 1 / -1;
  padding: 0.5rem;
}

.error {
  grid-column: 1 / -1;
  color: var(--danger, #b00020);
  margin: 0;
}
```

- [ ] **Step 4: Implement the component**

Create `frontend/trading-decision/src/components/OperatorEventForm.tsx`:

```tsx
import { useState } from "react";
import type { FormEvent } from "react";
import type {
  StrategyEventCreateRequest,
  Uuid,
} from "../api/types";
import styles from "./OperatorEventForm.module.css";

interface OperatorEventFormProps {
  sessionUuid: Uuid;
  onSubmit: (
    body: StrategyEventCreateRequest,
  ) => Promise<{ ok: boolean; status?: number; detail?: string }>;
}

function clamp(value: number, lo: number, hi: number): number {
  if (Number.isNaN(value)) return lo;
  return Math.max(lo, Math.min(hi, value));
}

function splitSymbols(raw: string): string[] {
  return raw
    .split(",")
    .map((s) => s.trim())
    .filter((s) => s.length > 0);
}

export default function OperatorEventForm({
  sessionUuid,
  onSubmit,
}: OperatorEventFormProps) {
  const [sourceText, setSourceText] = useState("");
  const [symbolsRaw, setSymbolsRaw] = useState("");
  const [severity, setSeverity] = useState("2");
  const [confidence, setConfidence] = useState("50");
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  async function handleSubmit(e: FormEvent) {
    e.preventDefault();
    setError(null);
    const trimmed = sourceText.trim();
    if (!trimmed) {
      setError("Source text is required.");
      return;
    }

    const body: StrategyEventCreateRequest = {
      source: "user",
      event_type: "operator_market_event",
      source_text: trimmed,
      session_uuid: sessionUuid,
      severity: clamp(Number(severity), 1, 5),
      confidence: clamp(Number(confidence), 0, 100),
    };

    const symbols = splitSymbols(symbolsRaw);
    if (symbols.length > 0) body.affected_symbols = symbols;

    setSubmitting(true);
    const res = await onSubmit(body);
    setSubmitting(false);
    if (!res.ok) {
      setError(res.detail ?? "Could not submit strategy event.");
      return;
    }
    setSourceText("");
    setSymbolsRaw("");
    setSeverity("2");
    setConfidence("50");
  }

  return (
    <form
      className={styles.form}
      onSubmit={handleSubmit}
      aria-label="Add operator market event"
    >
      <label className={styles.fullWidth}>
        Source text
        <textarea
          value={sourceText}
          onChange={(e) => setSourceText(e.target.value)}
          placeholder="e.g. OpenAI earnings missed expectations"
        />
      </label>

      <label className={styles.fullWidth}>
        Affected symbols (comma-separated, optional)
        <input
          value={symbolsRaw}
          onChange={(e) => setSymbolsRaw(e.target.value)}
          placeholder="e.g. MSFT, NVDA"
        />
      </label>

      <label>
        Severity (1–5)
        <input
          type="number"
          min={1}
          max={5}
          step={1}
          value={severity}
          onChange={(e) => setSeverity(e.target.value)}
        />
      </label>

      <label>
        Confidence (0–100)
        <input
          type="number"
          min={0}
          max={100}
          step={1}
          value={confidence}
          onChange={(e) => setConfidence(e.target.value)}
        />
      </label>

      {error ? (
        <p role="alert" className={styles.error}>
          {error}
        </p>
      ) : null}

      <button type="submit" disabled={submitting}>
        {submitting ? "Submitting..." : "Add event"}
      </button>
    </form>
  );
}
```

- [ ] **Step 5: Re-run the test — expect PASS**

```bash
npm run test -- src/__tests__/OperatorEventForm.test.tsx
```
Expected: 6/6 PASS.

- [ ] **Step 6: Run forbidden-import safety test**

```bash
npm run test -- src/__tests__/forbidden_mutation_imports.test.ts
```
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add frontend/trading-decision/src/components/OperatorEventForm.tsx \
        frontend/trading-decision/src/components/OperatorEventForm.module.css \
        frontend/trading-decision/src/__tests__/OperatorEventForm.test.tsx
git commit -m "feat(ui): add OperatorEventForm component (ROB-42)"
```

---

### Task 7: Wire timeline + form into `SessionDetailPage`

**Files:**
- Modify: `frontend/trading-decision/src/pages/SessionDetailPage.tsx`
- Modify: `frontend/trading-decision/src/pages/SessionDetailPage.module.css`

- [ ] **Step 1: Add CSS rule for the new section**

Open `frontend/trading-decision/src/pages/SessionDetailPage.module.css` and append at the end of the file:

```css
.strategyEvents {
  display: grid;
  gap: 12px;
}

.strategyEvents h2 {
  margin: 0;
}
```

- [ ] **Step 2: Wire the hook + section into the page**

Open `frontend/trading-decision/src/pages/SessionDetailPage.tsx`. Apply two edits:

**Edit A — add imports and call the hook.** At the top of the file, alongside the other component imports, add:

```tsx
import OperatorEventForm from "../components/OperatorEventForm";
import StrategyEventTimeline from "../components/StrategyEventTimeline";
import { useStrategyEvents } from "../hooks/useStrategyEvents";
```

Inside `SessionDetailPage`, immediately after the existing
```tsx
const analytics = useSessionAnalytics(sessionUuid ?? "");
```
line, add:
```tsx
const strategyEvents = useStrategyEvents(sessionUuid ?? "");
```

**Edit B — render the new section between the analytics block and the proposals section.** Insert this immediately before the existing `<section className={styles.proposals} aria-label="Proposals">`:

```tsx
<section
  className={styles.strategyEvents}
  aria-label="Strategy events"
>
  <h2>Strategy events</h2>
  <OperatorEventForm
    sessionUuid={data.session_uuid}
    onSubmit={(body) => strategyEvents.submit(body)}
  />
  {strategyEvents.status === "loading" ||
  strategyEvents.status === "idle" ? (
    <p>Loading strategy events...</p>
  ) : null}
  {strategyEvents.status === "error" ? (
    <p role="alert">{strategyEvents.error}</p>
  ) : null}
  {strategyEvents.status === "not_found" ? (
    <p role="alert">Session not found for strategy events.</p>
  ) : null}
  {strategyEvents.status === "success" && strategyEvents.data ? (
    <StrategyEventTimeline events={strategyEvents.data.events} />
  ) : null}
</section>
```

- [ ] **Step 3: Run typecheck**

```bash
cd frontend/trading-decision && npm run typecheck
```
Expected: PASS.

- [ ] **Step 4: Run the existing suite to confirm no regressions**

```bash
npm run test
```
Expected: existing `SessionDetailPage.test.tsx` cases must still pass — the existing tests do not stub `/trading/api/strategy-events?...`, so the hook's fetch attempt will be a 599 from `mockFetch`, which the hook surfaces as an `error` state. That is acceptable: the existing assertions check market-brief / proposals / analytics text, none of which reference the strategy-events section, and the `role="alert"` from the error state lives inside the `aria-label="Strategy events"` section so it does not collide with the existing `Session is archived` alert assertion. **If any existing test now fails because of the new section's presence** (for example a stricter `screen.getByRole("alert")` accidentally matching the strategy-events error), update only the existing test's mock to return an empty list for `/trading/api/strategy-events?...`, do not change assertions — and stop and report this in the PR description.

- [ ] **Step 5: Commit**

```bash
git add frontend/trading-decision/src/pages/SessionDetailPage.tsx \
        frontend/trading-decision/src/pages/SessionDetailPage.module.css
git commit -m "feat(ui): mount strategy event timeline + operator form in session detail (ROB-42)"
```

---

### Task 8: SessionDetailPage integration tests

**Files:**
- Modify: `frontend/trading-decision/src/__tests__/SessionDetailPage.test.tsx` (append `describe` block)

- [ ] **Step 1: Append integration tests**

At the bottom of `frontend/trading-decision/src/__tests__/SessionDetailPage.test.tsx`, **before the final closing `});`** of the existing `describe("SessionDetailPage", ...)` block, add the following tests (note: `makeStrategyEventListResponse` and `makeStrategyEvent` need to be added to the imports near the top — extend the existing `import { ... } from "../test/fixtures";` block):

```tsx
  it("renders session-scoped strategy events timeline", async () => {
    mockFetch({
      "/trading/api/decisions/session-1": () =>
        new Response(JSON.stringify(makeSessionDetail())),
      "/trading/api/decisions/session-1/analytics": () =>
        new Response(JSON.stringify(makeAnalyticsResponse())),
      "/trading/api/strategy-events?session_uuid=session-1&limit=50&offset=0":
        () =>
          new Response(
            JSON.stringify(
              makeStrategyEventListResponse({
                events: [
                  makeStrategyEvent({
                    source_text: "Fed hike confirmed",
                    affected_symbols: ["TSLA"],
                  }),
                ],
              }),
            ),
          ),
    });

    renderDetail();

    expect(await screen.findByText("Strategy events")).toBeInTheDocument();
    expect(await screen.findByText(/fed hike confirmed/i)).toBeInTheDocument();
    expect(screen.getByText("TSLA")).toBeInTheDocument();
    expect(screen.getByText(/operator_market_event/i)).toBeInTheDocument();
  });

  it("renders an empty state when there are no strategy events", async () => {
    mockFetch({
      "/trading/api/decisions/session-1": () =>
        new Response(JSON.stringify(makeSessionDetail())),
      "/trading/api/decisions/session-1/analytics": () =>
        new Response(JSON.stringify(makeAnalyticsResponse())),
      "/trading/api/strategy-events?session_uuid=session-1&limit=50&offset=0":
        () =>
          new Response(
            JSON.stringify(
              makeStrategyEventListResponse({ events: [], total: 0 }),
            ),
          ),
    });

    renderDetail();

    expect(
      await screen.findByText(/no strategy events yet/i),
    ).toBeInTheDocument();
  });

  it("submitting the operator event form POSTs operator_market_event with current session_uuid and refreshes the timeline", async () => {
    let listCalls = 0;
    const recorded: { url: string; method: string; body?: string }[] = [];
    mockFetch({
      "/trading/api/decisions/session-1": () =>
        new Response(JSON.stringify(makeSessionDetail())),
      "/trading/api/decisions/session-1/analytics": () =>
        new Response(JSON.stringify(makeAnalyticsResponse())),
      "/trading/api/strategy-events?session_uuid=session-1&limit=50&offset=0":
        () => {
          listCalls += 1;
          if (listCalls === 1) {
            return new Response(
              JSON.stringify(
                makeStrategyEventListResponse({ events: [], total: 0 }),
              ),
            );
          }
          return new Response(
            JSON.stringify(
              makeStrategyEventListResponse({
                events: [
                  makeStrategyEvent({
                    source_text: "OpenAI earnings missed",
                    affected_symbols: ["MSFT"],
                  }),
                ],
                total: 1,
              }),
            ),
          );
        },
      "/trading/api/strategy-events": (req) => {
        return req.text().then((body) => {
          recorded.push({ url: req.url, method: req.method, body });
          return new Response(
            JSON.stringify(
              makeStrategyEvent({
                source_text: "OpenAI earnings missed",
                affected_symbols: ["MSFT"],
              }),
            ),
            { status: 201 },
          );
        });
      },
    });

    renderDetail();

    await screen.findByText(/no strategy events yet/i);

    await userEvent.type(
      screen.getByLabelText(/source text/i),
      "OpenAI earnings missed",
    );
    await userEvent.type(
      screen.getByLabelText(/affected symbols/i),
      "MSFT",
    );
    await userEvent.click(
      screen.getByRole("button", { name: /add event/i }),
    );

    await waitFor(() => expect(recorded.length).toBe(1));
    const sentBody = JSON.parse(recorded[0]!.body ?? "{}");
    expect(sentBody.source).toBe("user");
    expect(sentBody.event_type).toBe("operator_market_event");
    expect(sentBody.session_uuid).toBe("session-1");
    expect(sentBody.source_text).toBe("OpenAI earnings missed");
    expect(sentBody.affected_symbols).toEqual(["MSFT"]);

    expect(
      await screen.findByText(/openai earnings missed/i),
    ).toBeInTheDocument();
  });

  it("surfaces a strategy-event submit error without mutating proposals", async () => {
    let proposalRespondCalled = false;
    mockFetch({
      "/trading/api/decisions/session-1": () =>
        new Response(JSON.stringify(makeSessionDetail())),
      "/trading/api/decisions/session-1/analytics": () =>
        new Response(JSON.stringify(makeAnalyticsResponse())),
      "/trading/api/strategy-events?session_uuid=session-1&limit=50&offset=0":
        () =>
          new Response(
            JSON.stringify(
              makeStrategyEventListResponse({ events: [], total: 0 }),
            ),
          ),
      "/trading/api/strategy-events": () =>
        new Response(JSON.stringify({ detail: "validation failed" }), {
          status: 422,
        }),
      "/trading/api/proposals/proposal-btc/respond": () => {
        proposalRespondCalled = true;
        return new Response(JSON.stringify({}));
      },
    });

    renderDetail();

    await screen.findByText(/no strategy events yet/i);
    await userEvent.type(screen.getByLabelText(/source text/i), "msg");
    await userEvent.click(
      screen.getByRole("button", { name: /add event/i }),
    );

    expect(
      await screen.findByText(/validation failed/i),
    ).toBeInTheDocument();
    expect(proposalRespondCalled).toBe(false);
  });
```

- [ ] **Step 2: Run the new tests**

```bash
cd frontend/trading-decision
npm run test -- src/__tests__/SessionDetailPage.test.tsx
```
Expected: all tests in the file PASS, including the four new ones.

- [ ] **Step 3: Run the full suite**

```bash
npm run test
```
Expected: all PASS.

- [ ] **Step 4: Commit**

```bash
git add frontend/trading-decision/src/__tests__/SessionDetailPage.test.tsx
git commit -m "test(ui): integration tests for strategy event UI on SessionDetailPage (ROB-42)"
```

---

### Task 9: Final verification

- [ ] **Step 1: Run all targeted tests**

```bash
cd frontend/trading-decision
npm run test -- src/__tests__/api.strategyEvents.test.ts
npm run test -- src/__tests__/StrategyEventTimeline.test.tsx
npm run test -- src/__tests__/OperatorEventForm.test.tsx
npm run test -- src/__tests__/SessionDetailPage.test.tsx
npm run test -- src/__tests__/forbidden_mutation_imports.test.ts
```
Expected: all PASS.

- [ ] **Step 2: Run full vitest suite**

```bash
npm run test
```
Expected: all PASS.

- [ ] **Step 3: Typecheck**

```bash
npm run typecheck
```
Expected: PASS (no type errors).

- [ ] **Step 4: Build**

```bash
npm run build
```
Expected: PASS (production bundle produced under `dist/`).

- [ ] **Step 5: Confirm no backend changes were made**

```bash
git diff --name-only main...HEAD | grep -v '^frontend/trading-decision/' | grep -v '^docs/plans/ROB-42' || echo "OK: only frontend + plan touched"
```
Expected: prints `OK: only frontend + plan touched` (or nothing if grep returned non-zero — that also signals only allowed files were touched).

- [ ] **Step 6: Confirm forbidden tokens absent in new sources**

```bash
grep -RnE "place_order|kis_trading_service|paper_order_handler|manage_watch_alerts|fill_notification|cancel_order|modify_order" frontend/trading-decision/src/api/strategyEvents.ts frontend/trading-decision/src/components/StrategyEventTimeline.tsx frontend/trading-decision/src/components/OperatorEventForm.tsx frontend/trading-decision/src/hooks/useStrategyEvents.ts && echo "VIOLATION" || echo "OK"
```
Expected: prints `OK`.

- [ ] **Step 7: Push branch and open PR**

```bash
git push -u origin feature/ROB-42-strategy-event-ui-timeline
gh pr create \
  --base main \
  --title "feat(ui): strategy event timeline & operator event form (ROB-42)" \
  --body "$(cat <<'EOF'
## Summary
- Add strategy-events API client (read + create) consuming the ROB-41 backend.
- Add `StrategyEventTimeline` and `OperatorEventForm` components plus `useStrategyEvents` hook.
- Mount the new section inside `SessionDetailPage` between analytics and proposals.

## Scope
- Frontend-only slice. No backend, broker, order, watch, paper, or live-execution code touched.
- Operator form always sends `source: "user"`, `event_type: "operator_market_event"`, and the current `session_uuid`.

## Test plan
- [x] `vitest` unit + integration tests for client, components, hook usage, and SessionDetailPage integration.
- [x] `npm run typecheck`
- [x] `npm run build`
- [x] forbidden-mutation-imports safety test still PASS.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```
Expected: PR URL printed.

---

## Acceptance criteria (mapped to ROB-42 spec)

| Spec requirement | Covered by |
|---|---|
| API client added under `frontend/trading-decision/src/api/` (or pattern extended) | Task 2 (`api/strategyEvents.ts`) |
| `SessionDetailPage` calls `GET /trading/api/strategy-events?session_uuid=<uuid>` | Task 4 + Task 7 (hook + page wiring) |
| Timeline section shows event type, source/normalized text, affected symbols/markets/themes, severity, confidence, created timestamp | Task 5 (`StrategyEventTimeline`) |
| Compact operator event form: required source_text textarea, optional comma-separated symbols, severity 1–5, confidence 0–100, safe defaults | Task 6 (`OperatorEventForm`) |
| Form POST payload: `source=user`, `event_type=operator_market_event`, `session_uuid=<current>` | Task 6 |
| Successful submit refreshes/appends timeline | Task 4 (`submit` calls `refetch`) + Task 8 integration test |
| Loading / error / empty state | Tasks 5, 6, 7 + Task 8 |
| TDD: failing tests written before implementation | Tasks 2, 5, 6, 8 (all start with a failing-test step) |
| Test 1: timeline renders session-scoped events | Task 8, test 1 |
| Test 2: empty state | Task 8, test 2 |
| Test 3: submit POSTs `operator_market_event` with current `session_uuid` | Task 8, test 3 |
| Test 4: successful submit refreshes/appends timeline | Task 8, test 3 |
| Test 5: API error surfaced; no proposal/order mutation triggered | Task 8, test 4 |
| Targeted vitest, `npm run typecheck`, `npm run build` | Task 9 |
| No broker/order/watch/paper/live execution imports | Task 9 step 6 + existing `forbidden_mutation_imports.test.ts` |
| No proposal decision auto-mutation | Task 8, test 4 verifies `/proposals/.../respond` is never hit |
| No strategy revision auto-mutation | No code path touches strategy revisions; verified by Task 9 step 5 (frontend-only diff) |
| No TradingAgents advisory integration | Out of scope; no code path touches it |

---

## Self-Review Notes

- **Spec coverage:** every numbered item under "필수 scope" (1–6), "안전/비목표," and "검증 기준" maps to a task above.
- **Placeholders scan:** every code-bearing step contains the actual code. No "TBD", no "fill in details", no "similar to Task N".
- **Type consistency:** types defined in Task 1 (`StrategyEventDetail`, `StrategyEventListResponse`, `StrategyEventCreateRequest`, `StrategyEventSource`, `StrategyEventType`) are referenced verbatim in Tasks 2, 4, 5, 6. `Uuid` is the existing alias from `types.ts`.
- **Function signatures:** `getStrategyEvents({ sessionUuid, limit?, offset? })` and `createStrategyEvent(body)` defined in Task 2 and consumed identically in Task 4. Hook `useStrategyEvents(sessionUuid)` returns `{ status, data, error, refetch, submit }` consumed identically in Task 7.
- **Component props:** `<StrategyEventTimeline events={...} />` and `<OperatorEventForm sessionUuid={...} onSubmit={...} />` defined in Tasks 5 and 6 and used identically in Task 7.
- **Backend contract:** verified against `app/routers/strategy_events.py`, `app/schemas/strategy_events.py`, `tests/routers/test_strategy_events_router.py` on this worktree.
