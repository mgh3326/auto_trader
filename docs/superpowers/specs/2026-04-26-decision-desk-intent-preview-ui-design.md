# Decision Desk Intent Preview UI + Discord Brief — Design

**Status:** approved (brainstorm)
**Date:** 2026-04-26
**Driver brief:** `/Users/robin/shared/prompts/2026-04-26-auto-trader-intent-preview-ui-discord-brief.md`
**Predecessor:** PR #588 (deployed) — `POST /portfolio/api/decision-runs/{run_id}/intent-preview`

## 1. Goal

Add a UI/operator handoff layer on top of the already-deployed Order Intent Preview endpoint:

1. From the Decision Desk page (`/portfolio/decision?run_id=<id>`), the operator can build an Order Intent Preview from the current persisted run.
2. Render the preview clearly in the browser.
3. Generate a Discord-ready markdown brief (server-side, deterministic) and let the operator copy it to the clipboard.

**This is UI / operator handoff only.** No order placement, no watch alert registration, no Redis writes (beyond existing session/auth), no Paperclip writes, no broker/task enqueue, no Discord webhook send.

## 2. Approach summary

The Discord brief is generated **server-side** by a pure formatter and returned as an additive `discord_brief: str | None` field on the existing preview response. The browser does not assemble the brief — it only stores `response.discord_brief` and copies it via the Clipboard API.

Rationale (chosen by user as option B):
- Deterministic markdown is easy to lock with pytest.
- Hermes/analyst can later reuse the same formatter to produce the same brief.
- Safety footer text lives in one place.
- Template JS stays minimal.
- `discord_brief` is optional/additive — existing clients are unaffected.

## 3. Architecture & file impact

```
[modified]
app/schemas/order_intent_preview.py            # add discord_brief: str | None = None
app/services/order_intent_preview_service.py   # accept decision_desk_url, call formatter
app/routers/portfolio.py                       # build decision_desk_url, pass to service
app/templates/portfolio_decision_desk.html     # add Order Intent Preview panel + Copy button
tests/test_order_intent_preview_service.py     # 2 cases: discord_brief None vs filled
tests/test_order_intent_preview_router.py      # 1 case: brief contains the persisted-run path

[new]
app/services/order_intent_discord_brief.py     # pure formatter + URL helper
tests/test_order_intent_discord_brief.py       # deterministic format + AST forbidden-import guard
```

Responsibility split:
- `order_intent_discord_brief.py` — pure functions, inputs in / string out, no DB / Redis / httpx / settings / env imports, no I/O, no logging side effects.
- `order_intent_preview_service.py` — preview intent construction (existing) + a single call into the formatter. **No markdown assembly inside the service.**
- `portfolio.py` (router) — composes `decision_desk_url` from `request.base_url` + run_id and passes it to the service. No new env vars in this PR.
- `portfolio_decision_desk.html` — renders preview UI and copies `response.discord_brief`. No brief assembly.

Call flow:
```
[browser] click Build Intent Preview
   POST /portfolio/api/decision-runs/{run_id}/intent-preview
[router]   decision_desk_url = build_decision_desk_url(str(request.base_url), run_id)
   → service.build_preview(..., decision_desk_url=decision_desk_url)
[service]  build intents (existing logic) → response
   if decision_desk_url is not None:
     response.discord_brief = format_discord_brief(preview=response, ...)
   return response
[browser] render preview panel; if response.discord_brief: enable Copy button
   click Copy → navigator.clipboard.writeText(response.discord_brief)
```

## 4. Backend changes

### 4.1 `app/schemas/order_intent_preview.py` — additive only

```python
class OrderIntentPreviewResponse(BaseModel):
    success: bool = True
    decision_run_id: str
    mode: Literal["preview_only"] = "preview_only"
    intents: list[OrderIntentPreviewItem] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    discord_brief: str | None = None   # new, optional, default None
```

No changes to `OrderIntentPreviewRequest`, `IntentBudgetInput`, `IntentSelectionInput`. The schema module must not import the service (no new cycles).

### 4.2 `app/services/order_intent_discord_brief.py` (new) — pure formatter

```python
"""Pure formatter for Decision Desk → Discord handoff brief.

Contract:
- No DB / Redis / httpx / settings / env imports.
- No I/O, no logging side effects, no global state.
- Inputs in → string out. Deterministic for fixed inputs.
"""
from __future__ import annotations

from typing import Literal
from urllib.parse import quote

from app.schemas.order_intent_preview import OrderIntentPreviewResponse

ExecutionMode = Literal["requires_final_approval", "paper_only", "dry_run_only"]
_TOP_INTENTS_DEFAULT_LIMIT = 10


def build_decision_desk_url(base_url: str, run_id: str) -> str:
    """Compose `<origin>/portfolio/decision?run_id=<quoted-id>`. Pure string op."""
    base = base_url.rstrip("/")
    return f"{base}/portfolio/decision?run_id={quote(run_id, safe='')}"


def format_discord_brief(
    *,
    preview: OrderIntentPreviewResponse,
    decision_desk_url: str,
    execution_mode: ExecutionMode,
    top_intents_limit: int = _TOP_INTENTS_DEFAULT_LIMIT,
) -> str:
    """Render a deterministic Discord-ready markdown brief."""
    ...
```

Notes:
- `execution_mode` is passed explicitly because it is per-intent in the schema — keeping it on the call lets the brief stay deterministic even when `intents` is empty.
- `format_discord_brief` does not read `preview.discord_brief` (chicken-and-egg avoidance).

### 4.3 Markdown layout — locked

Trailing newline `\n` once at the end. Section order is fixed. Field interpolation is exact.

```md
## Order Intent Preview Ready

Decision Desk: <decision_desk_url>
Run ID: `<preview.decision_run_id>`
Mode: `preview_only`
Execution mode: `<execution_mode>`

Summary:
- Total intents: <N>
- Buy: <count where side == "buy">
- Sell: <count where side == "sell">
- Manual review required: <count where status == "manual_review_required">
- Execution candidates: <count where status == "execution_candidate">
- Watch ready: <count where status == "watch_ready">

Top intents:
<lines, see 4.4>

Safety:
- This is preview-only.
- No orders were placed.
- No watch alerts were registered.
- Final approval is still required before any execution.
```

The four safety strings are **exact** and locked by tests:
- `This is preview-only.`
- `No orders were placed.`
- `No watch alerts were registered.`
- `Final approval is still required before any execution.`

### 4.4 Top intents lines

Iterate `preview.intents[: top_intents_limit]` in given order (the preview itself iterates `symbol_groups → items` deterministically).

Per-line template:
```
{idx}. `{symbol}` {market} {side} {intent_type} — {status}{trigger_part}{size_part}
```

- `{idx}` — 1-based.
- `{trigger_part}` — `f" — price {operator} {threshold:g}"` when `trigger` is non-null; otherwise empty. (`{:g}` keeps integers integer-shaped, drops trailing zeros.)
- `{size_part}` —
  - `side == "buy"` and `budget_krw is not None` → `f" — budget ₩{int(budget_krw):,}"`
  - `side == "sell"` and `quantity_pct is not None` → `f" — qty {quantity_pct:g}%"`
  - else empty.

Edge cases:
- Empty intents → single line `(no intents)` under `Top intents:`.
- `len(intents) > top_intents_limit` → after the listed lines, append `… and {len(intents) - top_intents_limit} more` (Unicode ellipsis `…` is canonical; formatter and tests use the same character).

`{side} {intent_type}` is intentionally redundant for buy/buy_candidate. Reason: operator readability. `manual_review` and any future intent_type may be ambiguous on direction; explicit `buy`/`sell` keeps the brief unambiguous.

Worked example (matches the brief's example, with side made explicit):
```
1. `KRW-BTC` CRYPTO sell trim_candidate — manual_review_required — qty 30%
2. `005930` KR buy buy_candidate — watch_ready — price below 72000 — budget ₩100,000
```

### 4.5 `app/services/order_intent_preview_service.py` — minimal change

Add a kw-only `decision_desk_url: str | None = None` parameter and call the formatter only when the URL is provided. **No markdown assembly inside the service.**

```python
from app.services.order_intent_discord_brief import format_discord_brief

class OrderIntentPreviewService:
    async def build_preview(
        self,
        *,
        user_id: int,
        run_id: str,
        request: OrderIntentPreviewRequest,
        decision_desk_url: str | None = None,
    ) -> OrderIntentPreviewResponse:
        # ... existing intent build logic unchanged ...
        response = OrderIntentPreviewResponse(
            decision_run_id=run_id,
            intents=intents,
            warnings=warnings,
        )
        if decision_desk_url is not None:
            response.discord_brief = format_discord_brief(
                preview=response,
                decision_desk_url=decision_desk_url,
                execution_mode=request.execution_mode,
            )
        return response
```

`decision_desk_url=None` keeps existing direct callers (tests, future internal users) unaffected.

### 4.6 `app/routers/portfolio.py` — preview endpoint wiring

Add `request: Request` to the existing endpoint signature (already imported in the module — no duplicate import). Build the URL and pass it down.

```python
from app.services.order_intent_discord_brief import build_decision_desk_url

@router.post(
    "/api/decision-runs/{run_id}/intent-preview",
    responses={
        404: {"description": "Decision run not found"},
        500: {"description": "Failed to build order intent preview"},
    },
)
async def preview_order_intents_for_decision_run(
    run_id: str,
    payload: OrderIntentPreviewRequest,
    request: Request,
    current_user: Annotated[User, Depends(get_authenticated_user)],
    preview_service: Annotated[
        OrderIntentPreviewService, Depends(get_order_intent_preview_service)
    ],
) -> OrderIntentPreviewResponse:
    # TODO(follow-up): respect PUBLIC_BASE_URL / X-Forwarded-* origin once the
    # public Decision Desk URL diverges from request.base_url under proxies.
    decision_desk_url = build_decision_desk_url(str(request.base_url), run_id)
    try:
        return await preview_service.build_preview(
            user_id=current_user.id,
            run_id=run_id,
            request=payload,
            decision_desk_url=decision_desk_url,
        )
    except PortfolioDecisionRunNotFoundError as e:
        raise HTTPException(status_code=404, detail=DECISION_RUN_NOT_FOUND_DETAIL) from e
    except Exception as e:
        logger.error("Error building intent preview: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=INTENT_PREVIEW_ERROR_DETAIL) from e
```

422 is left to FastAPI's default validation behavior (no custom handler). 404/500 follow the existing patterns — strings unchanged.

### 4.7 Contract diff

| Area | Change | Breaking? |
|---|---|---|
| `OrderIntentPreviewResponse` JSON | one optional `discord_brief` field added | no (additive) |
| `OrderIntentPreviewRequest` | none | no |
| `/intent-preview` HTTP behavior | 422/404/500 unchanged; 200 carries one extra field | no |
| `OrderIntentPreviewService.build_preview()` signature | kw-only `decision_desk_url=None` added | no (default) |
| New runtime dependencies | none (only stdlib `urllib.parse`) | — |

## 5. Frontend changes — `app/templates/portfolio_decision_desk.html`

### 5.1 Panel placement

Insert one new `<section>` between `#summary-section` and the filter card. Visible only when the existing `isSnapshotMode` JS flag is true (i.e., `?run_id=...` is set; variable already defined in the template).

### 5.2 Markup

```html
<!-- Order Intent Preview (snapshot mode only) -->
<section id="intent-preview-section" class="card border-0 shadow-sm mb-4 d-none" aria-labelledby="intent-preview-title">
  <div class="card-body">
    <div class="d-flex justify-content-between align-items-center mb-3">
      <h2 id="intent-preview-title" class="h5 mb-0">Order Intent Preview</h2>
      <span class="badge bg-info-subtle text-info-emphasis">preview_only</span>
    </div>

    <div class="alert alert-warning small mb-3" role="note">
      Preview only — no order, watch alert, Redis watch key, broker task, or Paperclip action is created.
    </div>

    <div class="row g-3 align-items-end mb-3">
      <div class="col-md-4">
        <label for="intent-default-buy-budget" class="form-label small text-uppercase fw-bold text-muted">
          Default buy budget (KRW)
        </label>
        <input id="intent-default-buy-budget" type="number" min="0" step="1000" class="form-control" placeholder="100000">
      </div>
      <div class="col-md-4">
        <label for="intent-execution-mode" class="form-label small text-uppercase fw-bold text-muted">
          Execution mode
        </label>
        <select id="intent-execution-mode" class="form-select">
          <option value="requires_final_approval" selected>requires_final_approval</option>
          <option value="paper_only">paper_only</option>
          <option value="dry_run_only">dry_run_only</option>
        </select>
      </div>
      <div class="col-md-2">
        <button id="build-intent-preview-btn" type="button" class="btn btn-primary w-100">Build Intent Preview</button>
      </div>
      <div class="col-md-2">
        <button id="copy-intent-discord-brief-btn" type="button" class="btn btn-outline-secondary w-100" disabled>
          <i class="bi bi-clipboard"></i> Copy Discord Brief
        </button>
      </div>
    </div>

    <div id="intent-preview-status" class="small text-muted mb-2" aria-live="polite"></div>

    <div id="intent-preview-result" class="d-none">
      <div class="row g-2 mb-3" id="intent-preview-counts"></div>
      <div class="table-responsive">
        <table class="table table-sm align-middle mb-0">
          <thead>
            <tr>
              <th>#</th><th>Symbol</th><th>Market</th><th>Side</th><th>Type</th>
              <th>Status</th><th>Trigger</th><th>Size</th><th>Warnings</th>
            </tr>
          </thead>
          <tbody id="intent-preview-rows"></tbody>
        </table>
      </div>
      <div id="intent-preview-truncation" class="small text-muted mt-2 d-none"></div>
    </div>
  </div>
</section>
```

### 5.3 JS behavior — added inside the existing `DOMContentLoaded` block

Reuses `snapshotRunId` and `isSnapshotMode` (defined at the top of the existing IIFE).

```js
const intentPreviewSection = document.getElementById('intent-preview-section');
const intentPreviewBudgetInput = document.getElementById('intent-default-buy-budget');
const intentPreviewModeSelect = document.getElementById('intent-execution-mode');
const buildIntentPreviewBtn = document.getElementById('build-intent-preview-btn');
const copyIntentBriefBtn = document.getElementById('copy-intent-discord-brief-btn');
const intentPreviewStatus = document.getElementById('intent-preview-status');
const intentPreviewResult = document.getElementById('intent-preview-result');
const intentPreviewCounts = document.getElementById('intent-preview-counts');
const intentPreviewRows = document.getElementById('intent-preview-rows');
const intentPreviewTruncation = document.getElementById('intent-preview-truncation');
const INTENT_PREVIEW_ROW_LIMIT = 10;

let lastIntentBrief = null;

if (isSnapshotMode) {
    intentPreviewSection.classList.remove('d-none');
    buildIntentPreviewBtn.addEventListener('click', buildIntentPreview);
    copyIntentBriefBtn.addEventListener('click', copyIntentBrief);
}

async function buildIntentPreview() {
    if (!snapshotRunId) return;
    setPreviewStatus('Building preview…', 'text-muted');
    intentPreviewResult.classList.add('d-none');
    buildIntentPreviewBtn.disabled = true;
    copyIntentBriefBtn.disabled = true;
    lastIntentBrief = null;

    const raw = intentPreviewBudgetInput.value.trim();
    const parsed = raw === '' ? null : Number(raw);
    const defaultBuyBudgetKrw = (parsed !== null && Number.isFinite(parsed)) ? parsed : null;

    const body = {
        budget: { default_buy_budget_krw: defaultBuyBudgetKrw },
        selections: [],
        execution_mode: intentPreviewModeSelect.value,
    };

    try {
        const response = await fetch(
            `/portfolio/api/decision-runs/${encodeURIComponent(snapshotRunId)}/intent-preview`,
            { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) },
        );
        const data = await response.json().catch(() => ({}));
        if (!response.ok) {
            setPreviewStatus(formatPreviewError(response.status, data), 'text-danger');
            return;
        }
        renderIntentPreview(data);
        if (data.discord_brief) {
            lastIntentBrief = data.discord_brief;
            copyIntentBriefBtn.disabled = false;
        }
        setPreviewStatus(`Preview built (${data.intents.length} intents).`, 'text-success');
    } catch (err) {
        setPreviewStatus(`Error: ${err.message}`, 'text-danger');
    } finally {
        buildIntentPreviewBtn.disabled = false;
    }
}

async function copyIntentBrief() {
    if (!lastIntentBrief) return;
    try {
        await navigator.clipboard.writeText(lastIntentBrief);
        const original = copyIntentBriefBtn.innerHTML;
        copyIntentBriefBtn.innerHTML = '<i class="bi bi-check2"></i> Copied';
        setTimeout(() => { copyIntentBriefBtn.innerHTML = original; }, 1500);
    } catch (err) {
        setPreviewStatus(`Clipboard error: ${err.message}`, 'text-danger');
    }
}

function setPreviewStatus(message, toneClass) {
    intentPreviewStatus.textContent = message;
    intentPreviewStatus.className = `small ${toneClass} mb-2`;
}

function formatPreviewError(status, data) {
    if (status === 422 && Array.isArray(data && data.detail)) {
        return data.detail.map(d => d.msg).filter(Boolean).join('; ') || 'Invalid input.';
    }
    if (data && typeof data.detail === 'string') return data.detail;
    return `Request failed (${status}).`;
}

function renderIntentPreview(data) {
    intentPreviewResult.classList.remove('d-none');

    // Counts chips: total / buy / sell / manual_review_required / execution_candidate / watch_ready / invalid
    const total = data.intents.length;
    const counts = {
        buy: 0, sell: 0,
        manual_review_required: 0, execution_candidate: 0, watch_ready: 0, invalid: 0,
    };
    data.intents.forEach(i => {
        if (i.side === 'buy') counts.buy += 1;
        if (i.side === 'sell') counts.sell += 1;
        counts[i.status] = (counts[i.status] || 0) + 1;
    });
    intentPreviewCounts.replaceChildren();
    appendCountChip('Total', total);
    appendCountChip('Buy', counts.buy);
    appendCountChip('Sell', counts.sell);
    appendCountChip('Manual review', counts.manual_review_required);
    appendCountChip('Execution candidate', counts.execution_candidate);
    appendCountChip('Watch ready', counts.watch_ready);
    appendCountChip('Invalid', counts.invalid);

    intentPreviewRows.replaceChildren();
    if (data.intents.length === 0) {
        const tr = document.createElement('tr');
        const td = document.createElement('td');
        td.colSpan = 9;
        td.className = 'text-muted text-center';
        td.textContent = '(no intents)';
        tr.appendChild(td);
        intentPreviewRows.appendChild(tr);
    } else {
        data.intents.slice(0, INTENT_PREVIEW_ROW_LIMIT).forEach((intent, idx) => {
            intentPreviewRows.appendChild(renderIntentRow(idx + 1, intent));
        });
    }

    if (data.intents.length > INTENT_PREVIEW_ROW_LIMIT) {
        intentPreviewTruncation.textContent =
            `Showing ${INTENT_PREVIEW_ROW_LIMIT} of ${data.intents.length} intents — see full list in Discord brief.`;
        intentPreviewTruncation.classList.remove('d-none');
    } else {
        intentPreviewTruncation.classList.add('d-none');
    }
}

function appendCountChip(label, value) {
    const col = document.createElement('div');
    col.className = 'col-auto';
    const span = document.createElement('span');
    span.className = 'badge bg-light text-dark border';
    span.textContent = `${label}: ${value}`;
    col.appendChild(span);
    intentPreviewCounts.appendChild(col);
}

function renderIntentRow(idx, intent) {
    const tr = document.createElement('tr');
    const cells = [
        String(idx),
        intent.symbol,
        intent.market,
        intent.side,
        intent.intent_type,
        intent.status,
        intent.trigger ? `${intent.trigger.metric} ${intent.trigger.operator} ${intent.trigger.threshold}` : '',
        intent.side === 'buy' && intent.budget_krw != null
            ? `₩${Number(intent.budget_krw).toLocaleString()}`
            : (intent.side === 'sell' && intent.quantity_pct != null ? `${intent.quantity_pct}%` : ''),
        (intent.warnings || []).join(', '),
    ];
    cells.forEach(text => {
        const td = document.createElement('td');
        td.textContent = text;   // textContent — never innerHTML for data
        tr.appendChild(td);
    });
    return tr;
}
```

**HTML escaping rule for this section:** all data going into table cells, count chips, status, and truncation note is set via `textContent` (never `innerHTML`). Only static button labels use `innerHTML` (icons), and those carry no user/server data.

### 5.4 Negative-path UX

| Case | Behavior |
|---|---|
| live mode (no `run_id`) | section stays hidden |
| budget < 0 → 422 | red status line shows flattened `detail[].msg`; result/Copy disabled |
| 404 unknown run | status shows `Decision run not found.` |
| 500 | status shows `Unable to build order intent preview.` |
| network failure | status shows `Error: <message>` |
| empty intents | counts all 0; table row `(no intents)`; Copy enabled (brief still meaningful) |
| missing `discord_brief` in response | Copy stays disabled; preview still rendered |

### 5.5 Side-effect guard (client)

The client only calls `POST /portfolio/api/decision-runs/{run_id}/intent-preview`. No other endpoint, no `localStorage` / `sessionStorage` writes, no Redis-affecting endpoints, no Discord webhook call.

## 6. Test plan

### 6.1 New — `tests/test_order_intent_discord_brief.py`

Pure-formatter unit tests using Pydantic fixtures directly (no slate/DB dependency). All cases marked `@pytest.mark.unit`.

Coverage:
1. `build_decision_desk_url` strips trailing slash, percent-encodes the run id, handles `localhost` origin.
2. Brief includes `Decision Desk: <url>`, `Run ID: \`<id>\``, `Mode: \`preview_only\``, `Execution mode: \`<mode>\``.
3. Safety footer contains all four exact strings:
   - `This is preview-only.`
   - `No orders were placed.`
   - `No watch alerts were registered.`
   - `Final approval is still required before any execution.`
4. Counts: total / buy / sell / manual_review_required / execution_candidate / watch_ready computed correctly across a mixed-intent fixture.
5. Top intent line for buy/buy_candidate/watch_ready with trigger + budget: exactly
   `` 1. `005930` KR buy buy_candidate — watch_ready — price below 72000 — budget ₩100,000 ``
6. Top intent line for sell/trim_candidate/manual_review_required with qty:
   `` 1. `KRW-BTC` CRYPTO sell trim_candidate — manual_review_required — qty 30% ``
7. Truncation: 13 intents → lines 1..10 present, line 11 absent, ends with `… and 3 more` (Unicode `…`).
8. Empty intents → `(no intents)` and `Total intents: 0`.
9. **AST-based forbidden-import guard.** Parse the formatter module with `ast`; collect every `ast.Import` and `ast.ImportFrom`; assert that no imported module name (or `from`-target) starts with any of:
   - `sqlalchemy`
   - `redis`
   - `httpx`
   - `app.core.config`
   - `app.tasks`
   - `app.services.kis`
   - `app.services.upbit`
   - `app.services.redis_token_manager`
   AST-based check (not substring) so that the docstring may freely mention `Redis` / `httpx` etc.

### 6.2 Modified — `tests/test_order_intent_preview_service.py`

Two added cases (existing tests untouched):
- `decision_desk_url=None` → `response.discord_brief is None`.
- `decision_desk_url="https://trader.robinco.dev/portfolio/decision?run_id=r"` → `response.discord_brief is not None`, contains the URL, contains `preview_only`.

### 6.3 Modified — `tests/test_order_intent_preview_router.py`

One added case (existing 404 case unchanged):
- `POST /portfolio/api/decision-runs/decision-r1/intent-preview` returns `discord_brief` containing the substring `/portfolio/decision?run_id=decision-r1`. Path/query substring is asserted (not the full origin) since `TestClient` base URL varies between environments.

### 6.4 No new UI auto-tests

Reasons:
- Backend formatter is the deterministic surface and is fully covered.
- Existing `test_portfolio_decision_page_with_run_id_renders_html_shell` already guards the page shell.
- No Playwright/Vitest/Jest infrastructure exists for templates; introducing one would expand scope.

Manual verification (from the brief, unchanged):
1. Open `/portfolio/decision?run_id=<persisted-run-id>` while logged in.
2. Click `Build Intent Preview` → panel renders `mode: preview_only` and intent count > 0 for a populated run.
3. Click `Copy Discord Brief` → paste into a scratch buffer; verify Decision Desk URL, run id, total count, four safety strings.
4. Confirm Redis `watch_alerts:*` and `model_rate_limit:*` key counts unchanged before/after.

### 6.5 Required commands

```
uv run ruff format --check app/ tests/
uv run ruff check app/ tests/
uv run pytest tests/test_order_intent_discord_brief.py -q
uv run pytest tests/test_order_intent_preview_service.py tests/test_order_intent_preview_router.py -q
uv run pytest tests/test_portfolio_decision_router.py tests/test_portfolio_decision_service.py tests/test_portfolio_decision_run_model.py -q
```

All five must be green pre- and post-PR.

## 7. Non-negotiable safety constraints (PR-wide)

This PR introduces **none** of the following, in any code path:

- `place_order(...)` (live, paper, or dry-run)
- `manage_watch_alerts(...)` or any watch-key Redis write
- Redis writes other than existing session/auth / token-manager behavior
- Paperclip API calls
- Broker / task enqueue
- KIS / Upbit order calls
- Discord webhook send from `auto_trader`

The formatter must remain pure: import-time graph reachable from `order_intent_discord_brief.py` must not include any of the forbidden modules listed in 6.1.

## 8. Out of scope (deferred)

- Automatic Discord webhook posting from `auto_trader`.
- Persisted `order_intent` DB model.
- Paperclip issue/comment integration.
- Watch alert registration after approval.
- Execution candidate creation.
- Final-approval flow.
- Live or paper order execution.
- Replacing `request.base_url` with a `PUBLIC_BASE_URL` / `X-Forwarded-*`-aware origin (TODO comment left in router).

## 9. Acceptance criteria

- Decision Desk page calls the preview endpoint for a persisted run.
- Preview result is visible and understandable without opening DevTools.
- Generated Discord brief is copyable and includes the Decision Desk URL.
- UI repeatedly states preview-only / no-execution semantics.
- Existing backend tests pass; new tests pass.
- No new live / paper / dry-run order or watch-alert side effects are introduced.
- Hermes `analyst` smoke check (operator-level) returns a `https://trader.robinco.dev/portfolio/decision?run_id=...` URL, includes `mode: preview_only`, and includes the four safety strings.
