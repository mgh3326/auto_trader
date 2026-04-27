# ROB-2 — Trading Decision API Contract Plan

- **PR scope:** Prompt 2 of `auto_trader_trading_decision_workspace_roadmap.md` only.
- **Branch / worktree:** `feature/ROB-2-trading-decision-api-contract` (single PR).
- **Status:** Plan only. No code yet.
- **Depends on:** ROB-1 / PR #595 (DB schema + service) — already merged to `main`.

> ⚠️ This PR ships **API endpoints only** over the ROB-1 persistence layer. React/Vite scaffold (ROB-3), decision workspace UI (ROB-4), outcome/analytics UI (ROB-5), Discord delivery, periodic reassessment, broker/watch live execution, and any KIS/Upbit/Redis side effect are **explicitly deferred**.

---

## 1. Goal

Expose authenticated FastAPI endpoints that let a future UI (and other internal callers) **create / read decision sessions and proposals, record user responses, record action links, create counterfactual tracks, and record/read outcome marks** — entirely as record-only operations on top of the ROB-1 schema.

Two non-negotiable invariants carry over from ROB-1 and are now contract-level guarantees:

1. The **immutable** `original_*` fields on a proposal are never mutated by an API call — even when the user `modify`s a recommendation. The user’s adjustments live in `user_*` fields.
2. The API performs **no broker, watch, or token-refresh side effect**. `record action` only persists external IDs the caller already obtained from a separate execution flow.

---

## 2. In-scope vs Out-of-scope

| Area | In scope (this PR) | Deferred |
|---|---|---|
| FastAPI router module | ✅ `app/routers/trading_decisions.py` | — |
| Pydantic request/response schemas | ✅ `app/schemas/trading_decisions.py` | — |
| Auth via existing `get_authenticated_user` dependency | ✅ | — |
| Session/proposal/action/counterfactual/outcome write endpoints | ✅ | — |
| List + detail read endpoints (sessions, proposals, outcomes nested in detail) | ✅ | — |
| Read helper additions in `trading_decision_service.py` | ✅ | — |
| Router test file (`tests/test_trading_decisions_router.py`) using `TestClient` + dependency overrides | ✅ | — |
| Side-effect safety test (no forbidden imports loaded by router module) | ✅ | — |
| Router-level OpenAPI `tags`, summary, response models | ✅ | — |
| React UI / Vite scaffold | ❌ | ROB-3 |
| Outcome dashboard / analytics view | ❌ | ROB-5 |
| Discord brief / push of new sessions | ❌ | future |
| Periodic reassessment cron job | ❌ | future |
| Live order placement, watch alert registration | ❌ (forbidden — see §9) | — |
| Hermes ingestion adapter (writing proposals from analyst output) | ❌ | future PR |

---

## 3. Workflow the API must support

The same scenario from ROB-1, now driven through HTTP:

```text
POST /trading/api/decisions                                    # create session
POST /trading/api/decisions/{session_uuid}/proposals           # add BTC trim, ETH/SOL watch, ORCA/ZBT avoid
POST /trading/api/proposals/{btc_uuid}/respond {accept}        # accept BTC exactly
POST /trading/api/proposals/{btc_uuid}/respond {modify, ...}   # OR: modify BTC 20% → 10%
POST /trading/api/proposals/{eth_uuid}/respond {accept}        # accept ETH only
POST /trading/api/proposals/{sol_uuid}/respond {defer}         # defer SOL
POST /trading/api/proposals/{btc_uuid}/actions {live_order,
       external_order_id, external_source}                     # link to KIS/Upbit order id
POST /trading/api/proposals/{sol_uuid}/counterfactuals {...}   # paper track for rejected SOL
POST /trading/api/proposals/{btc_uuid}/outcomes {1h, ...}      # mark price at horizon
GET  /trading/api/decisions                                    # list user’s sessions
GET  /trading/api/decisions/{session_uuid}                     # detail incl. nested proposals/actions/outcomes
```

A future UI will read `GET /decisions` for the inbox and `GET /decisions/{uuid}` for the detail screen.

---

## 4. Module placement & router structure

### 4.1 New files

| File | Purpose |
|---|---|
| `app/routers/trading_decisions.py` | `APIRouter` with all 8 endpoints |
| `app/schemas/trading_decisions.py` | Pydantic v2 request/response models |
| `tests/test_trading_decisions_router.py` | Router unit tests with `TestClient` + dependency overrides |
| `tests/test_trading_decisions_router_safety.py` | Subprocess import test enforcing §9 forbidden-import boundary |

### 4.2 Files modified

| File | Change |
|---|---|
| `app/main.py` | `from app.routers import trading_decisions` + `app.include_router(trading_decisions.router)` (next to `trading.router`) |
| `app/services/trading_decision_service.py` | Add **read** helpers only (`get_session_by_uuid`, `list_user_sessions`, `get_proposal_by_uuid`). No new write helpers — existing ones already satisfy the 8 endpoints. |

### 4.3 Router declaration

```python
# app/routers/trading_decisions.py
router = APIRouter(prefix="/trading", tags=["trading-decisions"])
```

**Why prefix `/trading` (same as `app.routers.trading`)?** The roadmap mandates `/trading/api/decisions/...`. FastAPI allows multiple routers to share a prefix; the existing `trading.router` owns `/trading/api/buy|sell|v1/trading/...` and the new router owns `/trading/api/decisions|proposals/...` — no path collisions. Tests confirm registration order and route uniqueness.

The middleware `TemplateFormCSRFMiddleware` already exempts `^/trading/` (see `app/main.py:190`), so JSON POSTs from internal clients won’t hit CSRF. Browser clients hit the same path through `AuthMiddleware` which populates `request.state.user`.

---

## 5. Authentication / authorization / dependency pattern

### 5.1 Auth dependency (reuse existing pattern)

Use `get_authenticated_user` from `app/routers/dependencies.py:14`:

```python
from app.routers.dependencies import get_authenticated_user
from app.models.trading import User

async def endpoint(
    ...,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_authenticated_user),
) -> SomeResponse: ...
```

Rationale: matches `app.routers.trading`, `app.routers.portfolio`, `app.routers.order_estimation`. Supports both JWT bearer (via `AuthMiddleware`) and web-session cookies. Unauthenticated requests get HTTP 401 from the dependency itself — no manual check needed in the handler.

### 5.2 Authorization model

Every endpoint that touches a `session_uuid` or `proposal_uuid` **must verify the current user owns the session**. The check must be done in the service layer (single source of truth), not duplicated in handlers.

Rule:

> If `session.user_id != current_user.id`, the API returns **HTTP 404**, **never 403**, to avoid leaking session existence.

Implementation:

- `get_session_by_uuid(db, *, session_uuid, user_id)` returns `None` if not found *or* not owned. Handler raises `HTTPException(404, "Decision session not found")`.
- `get_proposal_by_uuid(db, *, proposal_uuid, user_id)` joins `trading_decision_proposals → trading_decision_sessions` and filters on `sessions.user_id`. Returns `None` if not found or not owned.
- All proposal-scoped endpoints (`/proposals/{uuid}/respond|actions|counterfactuals|outcomes`) call `get_proposal_by_uuid` first; failure → 404.

### 5.3 No anonymous read

`GET /trading/api/decisions` returns the calling user’s sessions only. No admin/global view is added in this PR.

---

## 6. Pydantic request/response schemas

All schemas live in `app/schemas/trading_decisions.py`. Pydantic v2 syntax (matches `app/schemas/order_intent_preview.py`).

### 6.1 Shared types

```python
from datetime import datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID
from pydantic import BaseModel, Field, model_validator

ProposalKindLiteral = Literal[
    "trim", "add", "enter", "exit",
    "pullback_watch", "breakout_watch",
    "avoid", "no_action", "other",
]
SideLiteral = Literal["buy", "sell", "none"]
UserResponseLiteral = Literal[
    "pending", "accept", "reject", "modify", "partial_accept", "defer",
]
ActionKindLiteral = Literal[
    "live_order", "paper_order", "watch_alert", "no_action", "manual_note",
]
TrackKindLiteral = Literal[
    "accepted_live", "accepted_paper",
    "rejected_counterfactual", "analyst_alternative", "user_alternative",
]
OutcomeHorizonLiteral = Literal["1h", "4h", "1d", "3d", "7d", "final"]
SessionStatusLiteral = Literal["open", "closed", "archived"]
InstrumentTypeLiteral = Literal[
    "equity_kr", "equity_us", "crypto", "etf", "futures", "option", "other",
]  # mirror app.models.trading.InstrumentType
```

> The Literal aliases must be kept in sync with the SQLAlchemy CHECK constraints in `app/models/trading_decision.py`. A test (see §10.6) imports both and asserts set equality.

### 6.2 Session schemas

```python
class SessionCreateRequest(BaseModel):
    source_profile: str = Field(..., min_length=1, max_length=64)
    strategy_name: str | None = Field(default=None, max_length=128)
    market_scope: str | None = Field(default=None, max_length=32)
    market_brief: dict | None = None
    generated_at: datetime
    notes: str | None = Field(default=None, max_length=4000)

class SessionSummary(BaseModel):
    session_uuid: UUID
    source_profile: str
    strategy_name: str | None
    market_scope: str | None
    status: SessionStatusLiteral
    generated_at: datetime
    created_at: datetime
    updated_at: datetime
    proposals_count: int
    pending_count: int

class SessionDetail(SessionSummary):
    market_brief: dict | None
    notes: str | None
    proposals: list["ProposalDetail"]

class SessionListResponse(BaseModel):
    sessions: list[SessionSummary]
    total: int
    limit: int
    offset: int
```

### 6.3 Proposal schemas

```python
class ProposalCreateItem(BaseModel):
    symbol: str = Field(..., min_length=1, max_length=64)
    instrument_type: InstrumentTypeLiteral
    proposal_kind: ProposalKindLiteral
    side: SideLiteral = "none"
    original_quantity: Decimal | None = None
    original_quantity_pct: Decimal | None = Field(default=None, ge=0, le=100)
    original_amount: Decimal | None = Field(default=None, ge=0)
    original_price: Decimal | None = Field(default=None, ge=0)
    original_trigger_price: Decimal | None = Field(default=None, ge=0)
    original_threshold_pct: Decimal | None = Field(default=None, ge=0, le=100)
    original_currency: str | None = Field(default=None, max_length=8)
    original_rationale: str | None = Field(default=None, max_length=4000)
    original_payload: dict  # required, lossless analyst snapshot

class ProposalCreateBulkRequest(BaseModel):
    proposals: list[ProposalCreateItem] = Field(..., min_length=1, max_length=100)

class ProposalSummary(BaseModel):
    proposal_uuid: UUID
    symbol: str
    instrument_type: InstrumentTypeLiteral
    proposal_kind: ProposalKindLiteral
    side: SideLiteral
    user_response: UserResponseLiteral
    responded_at: datetime | None
    created_at: datetime
    updated_at: datetime

class ProposalDetail(ProposalSummary):
    original_quantity: Decimal | None
    original_quantity_pct: Decimal | None
    original_amount: Decimal | None
    original_price: Decimal | None
    original_trigger_price: Decimal | None
    original_threshold_pct: Decimal | None
    original_currency: str | None
    original_rationale: str | None
    original_payload: dict
    user_quantity: Decimal | None
    user_quantity_pct: Decimal | None
    user_amount: Decimal | None
    user_price: Decimal | None
    user_trigger_price: Decimal | None
    user_threshold_pct: Decimal | None
    user_note: str | None
    actions: list["ActionDetail"]
    counterfactuals: list["CounterfactualDetail"]
    outcomes: list["OutcomeDetail"]

class ProposalCreateBulkResponse(BaseModel):
    proposals: list[ProposalDetail]
```

### 6.4 Response schemas

```python
class ProposalRespondRequest(BaseModel):
    response: Literal["accept", "reject", "modify", "partial_accept", "defer"]
    user_quantity: Decimal | None = None
    user_quantity_pct: Decimal | None = Field(default=None, ge=0, le=100)
    user_amount: Decimal | None = Field(default=None, ge=0)
    user_price: Decimal | None = Field(default=None, ge=0)
    user_trigger_price: Decimal | None = Field(default=None, ge=0)
    user_threshold_pct: Decimal | None = Field(default=None, ge=0, le=100)
    user_note: str | None = Field(default=None, max_length=4000)

    @model_validator(mode="after")
    def _modify_requires_some_user_field(self) -> "ProposalRespondRequest":
        if self.response in ("modify", "partial_accept") and not any(
            v is not None for v in (
                self.user_quantity, self.user_quantity_pct,
                self.user_amount, self.user_price,
                self.user_trigger_price, self.user_threshold_pct,
            )
        ):
            raise ValueError(
                "modify/partial_accept requires at least one user_* numeric field"
            )
        return self
```

> Note: server stamps `responded_at = utcnow()` — clients **cannot** override it. Keeps the audit trail trustworthy.

### 6.5 Action / counterfactual / outcome schemas

```python
class ActionCreateRequest(BaseModel):
    action_kind: ActionKindLiteral
    external_order_id: str | None = Field(default=None, max_length=128)
    external_paper_id: str | None = Field(default=None, max_length=128)
    external_watch_id: str | None = Field(default=None, max_length=128)
    external_source: str | None = Field(default=None, max_length=64)
    payload_snapshot: dict

    @model_validator(mode="after")
    def _kinds_requiring_external_id(self) -> "ActionCreateRequest":
        needs_id = self.action_kind not in ("no_action", "manual_note")
        has_id = any([self.external_order_id, self.external_paper_id, self.external_watch_id])
        if needs_id and not has_id:
            raise ValueError(
                f"action_kind '{self.action_kind}' requires at least one external_* id"
            )
        return self

class ActionDetail(BaseModel):
    id: int
    action_kind: ActionKindLiteral
    external_order_id: str | None
    external_paper_id: str | None
    external_watch_id: str | None
    external_source: str | None
    payload_snapshot: dict
    recorded_at: datetime
    created_at: datetime

class CounterfactualCreateRequest(BaseModel):
    track_kind: Literal[
        "rejected_counterfactual", "analyst_alternative",
        "user_alternative", "accepted_paper",
    ]
    baseline_price: Decimal = Field(..., ge=0)
    baseline_at: datetime
    quantity: Decimal | None = None
    payload: dict
    notes: str | None = Field(default=None, max_length=4000)

class CounterfactualDetail(BaseModel):
    id: int
    track_kind: TrackKindLiteral
    baseline_price: Decimal
    baseline_at: datetime
    quantity: Decimal | None
    payload: dict
    notes: str | None
    created_at: datetime

class OutcomeCreateRequest(BaseModel):
    track_kind: TrackKindLiteral
    horizon: OutcomeHorizonLiteral
    price_at_mark: Decimal = Field(..., ge=0)
    counterfactual_id: int | None = None  # plain int FK; matches DB
    pnl_pct: Decimal | None = None
    pnl_amount: Decimal | None = None
    marked_at: datetime
    payload: dict | None = None

    @model_validator(mode="after")
    def _accepted_live_invariant(self) -> "OutcomeCreateRequest":
        if self.track_kind == "accepted_live" and self.counterfactual_id is not None:
            raise ValueError("accepted_live track must not include counterfactual_id")
        if self.track_kind != "accepted_live" and self.counterfactual_id is None:
            raise ValueError(
                f"track_kind '{self.track_kind}' requires counterfactual_id"
            )
        return self

class OutcomeDetail(BaseModel):
    id: int
    counterfactual_id: int | None
    track_kind: TrackKindLiteral
    horizon: OutcomeHorizonLiteral
    price_at_mark: Decimal
    pnl_pct: Decimal | None
    pnl_amount: Decimal | None
    marked_at: datetime
    payload: dict | None
    created_at: datetime
```

> All `Decimal` fields serialize as JSON strings (Pydantic v2 default for Decimal) to avoid float precision loss. Documented in the OpenAPI schema.

---

## 7. Endpoint behavior

All endpoints are async, use `get_authenticated_user` and `get_db`, and return Pydantic response models.

### 7.1 `GET /trading/api/decisions` → `SessionListResponse`

**Query params:** `limit: int = 50` (max 200), `offset: int = 0`, `status: SessionStatusLiteral | None = None`.

**Behavior:**
1. `sessions, total = await list_user_sessions(db, user_id=current_user.id, limit=limit, offset=offset, status=status)`
2. Each `SessionSummary` includes `proposals_count` (total) and `pending_count` (`user_response = 'pending'`) — computed in a single SQL query (subquery or grouped count) to avoid N+1.
3. Response always includes echoed `limit`/`offset`/`total`.

### 7.2 `POST /trading/api/decisions` → `SessionDetail`

**Body:** `SessionCreateRequest`.

**Behavior:**
1. `session_obj = await create_decision_session(db, user_id=current_user.id, ...)`.
2. `await db.commit()` (router commits — service flushes).
3. Refetch via `get_session_by_uuid` to populate empty `proposals` list. Return `SessionDetail`.

**Status:** 201 Created. `Location: /trading/api/decisions/{session_uuid}`.

### 7.3 `GET /trading/api/decisions/{session_uuid}` → `SessionDetail`

**Path:** `session_uuid: UUID`.

**Behavior:**
1. `session_obj = await get_session_by_uuid(db, session_uuid=session_uuid, user_id=current_user.id)`.
2. If `None` → 404 `"Decision session not found"`.
3. Eager-load (`selectinload`) proposals → actions / counterfactuals / outcomes. Build `SessionDetail` with full nesting.

### 7.4 `POST /trading/api/decisions/{session_uuid}/proposals` → `ProposalCreateBulkResponse`

**Body:** `ProposalCreateBulkRequest`.

**Behavior:**
1. Look up session via `get_session_by_uuid`. 404 if not owned.
2. Refuse if `session.status != 'open'` → HTTP 409 `"Session is not open"`.
3. Convert each `ProposalCreateItem` to the service’s `ProposalCreate` TypedDict and call `add_decision_proposals`. Commit. Return `ProposalCreateBulkResponse` with refetched proposals (so server-set fields populate).

**Status:** 201 Created.

### 7.5 `POST /trading/api/proposals/{proposal_uuid}/respond` → `ProposalDetail`

**Path:** `proposal_uuid: UUID`. **Body:** `ProposalRespondRequest`.

**Behavior:**
1. `proposal = await get_proposal_by_uuid(db, proposal_uuid=proposal_uuid, user_id=current_user.id)`. 404 if missing/not owned.
2. Refuse if proposal’s parent session is `'archived'` → 409.
3. Call `record_user_response(db, proposal_id=proposal.id, response=UserResponse(req.response), user_quantity=..., responded_at=utcnow())`. Commit.
4. Refetch with eager-loaded children → return `ProposalDetail`.

**Idempotency note:** A second `respond` call overwrites the earlier user response. The DB CHECK `(user_response='pending') = (responded_at IS NULL)` is preserved because `responded_at` is always set when a non-pending response is written.

### 7.6 `POST /trading/api/proposals/{proposal_uuid}/actions` → `ActionDetail`

**Body:** `ActionCreateRequest`.

**Behavior:**
1. Look up proposal (404 if missing/not owned).
2. Call `record_decision_action(db, proposal_id=proposal.id, action_kind=ActionKind(req.action_kind), external_order_id=..., payload_snapshot=...)`. The service raises `ValueError` if external-id invariant is violated → translate to **HTTP 422** with the message.
3. Commit. Return `ActionDetail`.

**Safety:** the handler does **not** import or call `app.services.kis*`, `app.services.upbit*`, `app.services.brokers.*`, `app.services.order_service`, `app.services.fill_notification`, watch-alert services, or any Redis token manager. External IDs are passed as opaque strings.

**Status:** 201 Created.

### 7.7 `POST /trading/api/proposals/{proposal_uuid}/counterfactuals` → `CounterfactualDetail`

**Body:** `CounterfactualCreateRequest`.

**Behavior:**
1. Look up proposal (404 if missing/not owned).
2. Call `create_counterfactual_track(db, proposal_id=proposal.id, track_kind=TrackKind(req.track_kind), baseline_price=..., payload=...)`. Commit. Return `CounterfactualDetail`.

**Status:** 201 Created.

### 7.8 `POST /trading/api/proposals/{proposal_uuid}/outcomes` → `OutcomeDetail`

**Body:** `OutcomeCreateRequest`.

**Behavior:**
1. Look up proposal (404 if missing/not owned).
2. If `counterfactual_id` is provided, verify it belongs to the same proposal — service-level helper or a single SELECT before insert. Mismatch → 422.
3. Call `record_outcome_mark(...)`. Commit. Return `OutcomeDetail`.
4. Service raises `ValueError` on `accepted_live` invariant violations → translate to 422.
5. PostgreSQL `IntegrityError` on the unique `(proposal_id, counterfactual_id, track_kind, horizon)` index → 409 `"Outcome mark already exists for this horizon"`.

**Status:** 201 Created.

### 7.9 Error response shape

All `HTTPException` instances use `detail: str`. No structured error envelope is introduced in this PR (matches `order_intent_preview` precedent). Validation errors come back as standard FastAPI 422 with `loc/msg/type`.

---

## 8. Service layer additions (read helpers)

Add to `app/services/trading_decision_service.py`. Pure persistence — no new write functions.

```python
async def get_session_by_uuid(
    session: AsyncSession,
    *,
    session_uuid: UUID,
    user_id: int,
) -> TradingDecisionSession | None:
    """Return session iff it exists AND belongs to user_id; eager-load proposals."""

async def list_user_sessions(
    session: AsyncSession,
    *,
    user_id: int,
    limit: int = 50,
    offset: int = 0,
    status: str | None = None,
) -> tuple[list[tuple[TradingDecisionSession, int, int]], int]:
    """
    Return (rows, total). Each row is (session, proposals_count, pending_count).
    Single SQL query: `SELECT s.*, count(p.id) AS proposals_count,
    count(p.id) FILTER (WHERE p.user_response='pending') AS pending_count
    FROM trading_decision_sessions s LEFT JOIN trading_decision_proposals p ...
    GROUP BY s.id ORDER BY s.generated_at DESC LIMIT :limit OFFSET :offset`.
    `total` is a separate `SELECT count(*) WHERE user_id = :user_id [AND status=:status]`.
    """

async def get_proposal_by_uuid(
    session: AsyncSession,
    *,
    proposal_uuid: UUID,
    user_id: int,
) -> TradingDecisionProposal | None:
    """JOIN sessions to enforce ownership. Eager-load actions/counterfactuals/outcomes."""
```

These are **read-only**. None of them invoke broker, watch, or Redis paths. The §9 forbidden-import test covers them transitively.

---

## 9. Side-effect safety boundaries (forbidden imports)

The router **must not** import (directly or transitively from `app.services.trading_decision_service` or `app.schemas.trading_decisions`) any of:

```text
app.services.kis
app.services.kis_trading_service
app.services.kis_trading_contracts
app.services.upbit
app.services.upbit_websocket
app.services.brokers
app.services.order_service
app.services.fill_notification
app.services.execution_event
app.services.redis_token_manager
app.services.kis_websocket
app.services.kis_websocket_internal
app.tasks
```

This is the **same list** ROB-1 enforces on the service module. ROB-2 extends the assertion to the router module.

**Permitted imports** the router *does* need:

- `app.routers.dependencies.get_authenticated_user`
- `app.core.db.get_db`
- `app.models.trading.User` (typing only)
- `app.models.trading_decision.*` (enums + ORM classes)
- `app.services.trading_decision_service.*` (existing + new read helpers)
- `app.schemas.trading_decisions.*`
- standard FastAPI / SQLAlchemy / Pydantic imports

A dedicated test (`tests/test_trading_decisions_router_safety.py`, see §10.6) imports the router module in a clean subprocess and asserts no forbidden module name (exact or `prefix + "."`) ends up in `sys.modules`. Mirrors the ROB-1 service test pattern in `tests/models/test_trading_decision_service.py:576`.

> If a future change pulls in one of these modules — even transitively — the test will fail. The fix is to isolate the import behind a function or move logic, not to weaken the test.

---

## 10. Tests

All tests `pytest`-async. Router tests follow the `TestClient` + `app.dependency_overrides[get_authenticated_user] = lambda: user` pattern from `tests/test_order_intent_preview_router.py` (the canonical example in this repo). The DB layer is mocked via an `AsyncMock` for unit-level router tests; one integration test exercises the full stack against a real test DB (gated by the same `_ensure_trading_decision_tables` helper as ROB-1).

### 10.1 `tests/test_trading_decisions_router.py` — request/response unit tests

Each test creates a `TestClient` with:
- `app.dependency_overrides[get_authenticated_user] = lambda: SimpleNamespace(id=7)`
- `app.dependency_overrides[get_db] = lambda: AsyncMock(...)`
- The trading_decision_service module is monkeypatched per test to return canned ORM-shaped objects (or `SimpleNamespace`).

Cases (mirrors roadmap’s required test list):

1. **`test_authenticated_create_session`** — POST /decisions returns 201, response includes `session_uuid`, `created_at`, `proposals=[]`.
2. **`test_unauthenticated_request_returns_401`** — no auth override; assert 401.
3. **`test_create_proposals_btc_eth_sol`** — POST /decisions/{uuid}/proposals with 3 items returns 201 with all 3 in payload, `user_response="pending"`.
4. **`test_modify_btc_20_to_10_preserves_original`** — POST /proposals/{uuid}/respond `{response:"modify", user_quantity_pct:10}`. Response shows `original_quantity_pct=20` and `user_quantity_pct=10`. Assert the call to `record_user_response` passed `responded_at` set by the server (not from request body).
5. **`test_accept_btc_eth_defer_sol`** — three POST /respond calls; each returns the updated proposal; verify per-proposal `user_response` independence.
6. **`test_record_live_order_action`** — POST /actions `{action_kind:"live_order", external_order_id:"KIS-1", external_source:"kis", payload_snapshot:{...}}`. Assert 201 and that `record_decision_action` was called with those exact kwargs.
7. **`test_record_watch_alert_action`** — analogous for `watch_alert` + `external_watch_id`.
8. **`test_action_no_external_id_returns_422`** — body `{action_kind:"live_order"}` with no external_*; Pydantic validator rejects → 422.
9. **`test_session_not_owned_returns_404`** — service helper returns `None`; assert 404 (not 403, not 401).
10. **`test_proposal_not_owned_returns_404`** — same for proposal endpoints.
11. **`test_create_proposals_on_archived_session_returns_409`** — service returns archived session; handler refuses.
12. **`test_outcome_mark_invalid_track_combo_returns_422`** — request `{track_kind:"accepted_live", counterfactual_id:42}` rejected by validator.
13. **`test_outcome_mark_duplicate_horizon_returns_409`** — service raises `IntegrityError`; handler maps to 409.
14. **`test_modify_without_user_fields_returns_422`** — `{response:"modify"}` with no user_* fields → validator error.
15. **`test_list_decisions_pagination`** — service returns 3 sessions w/ counts; assert response shape, `total`, `limit`, `offset` echoed.
16. **`test_get_session_detail_includes_nested_actions_and_outcomes`** — service returns session w/ proposals → actions → outcomes; response nests them.
17. **`test_create_counterfactual_track`** — POST /counterfactuals returns 201; service called with parsed `Decimal`.

### 10.2 `tests/test_trading_decisions_router_safety.py` — forbidden-import safety

Single test (`test_router_module_does_not_import_execution_paths`):
- Subprocess script imports `app.routers.trading_decisions` (with the same `app.services` pre-stub trick as ROB-1’s `test_service_module_does_not_import_execution_paths`).
- Asserts none of the §9 forbidden prefixes are present in `sys.modules`.

### 10.3 Schema-vs-DB consistency test

Single test in `tests/test_trading_decisions_router.py` (`test_pydantic_literals_match_db_enums`):
- Imports `ProposalKind`, `UserResponse`, `ActionKind`, `TrackKind`, `OutcomeHorizon`, `SessionStatus` enums from `app.models.trading_decision` and the corresponding `Literal[...]` aliases from `app.schemas.trading_decisions`.
- Asserts `set(EnumClass) == set(get_args(LiteralAlias))`. Catches drift if the model gains a new value but the schema is not updated.

### 10.4 Optional integration test

One smoke integration test gated by the `@pytest.mark.integration` marker, using the real test DB (same setup pattern as `tests/models/test_trading_decision_service.py`):

- `test_full_btc_session_round_trip` — creates a user via raw SQL, hits `POST /decisions`, `POST /decisions/{uuid}/proposals`, `POST /proposals/{uuid}/respond`, `GET /decisions/{uuid}`, asserts the returned shape matches the service-level scenario tests in ROB-1.

This test is allowed to be skipped in CI if PG is unavailable (same `_ensure_trading_decision_tables` skip pattern).

### 10.5 Verification commands

```bash
# unit
uv run pytest tests/test_trading_decisions_router.py -q
uv run pytest tests/test_trading_decisions_router_safety.py -q

# integration (DB-required)
uv run pytest tests/test_trading_decisions_router.py -q -m integration

# full ROB-1 + ROB-2 sanity
uv run pytest tests/models/test_trading_decision_models.py tests/models/test_trading_decision_service.py tests/test_trading_decisions_router.py tests/test_trading_decisions_router_safety.py -q

# lint
uv run ruff check app/routers/trading_decisions.py app/schemas/trading_decisions.py tests/test_trading_decisions_router.py tests/test_trading_decisions_router_safety.py

# type
uv run ty check app/routers/trading_decisions.py app/schemas/trading_decisions.py app/services/trading_decision_service.py
```

---

## 11. File-by-file changeset

| File | Action | Notes |
|---|---|---|
| `app/routers/trading_decisions.py` | **new** | 8 endpoints, ~400 LOC |
| `app/schemas/trading_decisions.py` | **new** | Pydantic request/response models, ~350 LOC |
| `app/services/trading_decision_service.py` | **modify** | add 3 read helpers (`get_session_by_uuid`, `list_user_sessions`, `get_proposal_by_uuid`); existing write functions untouched |
| `app/main.py` | **modify** | import + `include_router(trading_decisions.router)` |
| `tests/test_trading_decisions_router.py` | **new** | unit/router tests |
| `tests/test_trading_decisions_router_safety.py` | **new** | subprocess import safety test |
| `docs/plans/ROB-2-trading-decision-api-contract-plan.md` | **this file** | — |

No model files change. No migration. No template / static / settings change.

---

## 12. Acceptance checklist (used at PR review time)

- [ ] All 8 endpoints in §3 are reachable under `/trading/api/...` exactly as in the roadmap.
- [ ] All endpoints require authentication; unauthenticated calls return 401.
- [ ] Cross-user access of `session_uuid` / `proposal_uuid` returns **404** (not 403).
- [ ] `POST /respond` with `response="modify"` updates `user_*` columns and never mutates `original_*` (verified by integration test §10.4 *and* by the ROB-1 service-level test that already exists).
- [ ] `responded_at` is server-stamped; client cannot override.
- [ ] `POST /actions` for `live_order|paper_order|watch_alert` requires at least one external id (rejected at the schema layer with 422).
- [ ] `POST /outcomes` enforces `(track_kind='accepted_live') ⇔ (counterfactual_id IS NULL)`.
- [ ] Duplicate outcome at the same horizon returns 409 (DB unique constraint mapped).
- [ ] `app/routers/trading_decisions.py` imports **none** of the §9 forbidden modules (test enforced).
- [ ] No new SQLAlchemy model, no Alembic revision, no template, no static asset.
- [ ] `ruff check app/ tests/` clean. `ty check` clean on the two new files.
- [ ] `app/main.py` registers the router exactly once, after `trading.router`.
- [ ] All §10.1–§10.3 tests pass; §10.4 passes when DB is available.
- [ ] OpenAPI schema (`/openapi.json`) shows all 8 routes under tag `trading-decisions`.

---

## 13. Out-of-scope reminders (do not creep)

If during implementation any of these is tempting, **stop and split into a new PR**:

- Adding any frontend / React / Vite scaffold → ROB-3.
- Adding outcome dashboard or analytics endpoint beyond the per-proposal POST → ROB-5.
- Adding a Discord brief or webhook on session creation → out of scope.
- Calling KIS / Upbit / brokers from the action endpoint, or auto-registering watch alerts → forbidden (§9).
- Adding a Hermes ingestion route that auto-creates sessions from analyst output → separate PR.
- Adding admin/global session list views → not now; user-scoped only.
- Adding WebSocket push of new proposals → out of scope.
- Soft-delete / archive cascade behavior beyond the 409 refusal → out of scope.

---

## 14. Implementer handoff prompt

Paste the block below into a fresh **Sonnet implementer** session in the same worktree (`feature-ROB-2-trading-decision-api-contract`). The implementer should be a TDD agent — write the failing test first, then the minimal code to make it pass, commit, repeat.

```text
You are the implementer for ROB-2 (Trading Decision API contract PR).

Worktree:  /Users/mgh3326/work/auto_trader-worktrees/feature-ROB-2-trading-decision-api-contract
Branch:    feature/ROB-2-trading-decision-api-contract
Plan:      docs/plans/ROB-2-trading-decision-api-contract-plan.md  ← READ FIRST, FOLLOW EXACTLY

ROB-1 (DB schema + service) is already on main. Do NOT modify ROB-1 models or migrations.
You may add 3 read helpers to app/services/trading_decision_service.py (see plan §8). Do not add new write helpers.

Constraints (hard):
1. No broker, watch, or Redis side-effect code. The §9 forbidden-import list is enforced by tests/test_trading_decisions_router_safety.py — that test must stay green.
2. Cross-user access returns 404, never 403. Authorization is via service-layer helpers only.
3. responded_at is server-stamped; reject any attempt to read it from the request body.
4. Decimal fields are strings on the wire (Pydantic v2 default).
5. Use the existing get_authenticated_user dependency from app/routers/dependencies.py.

Build order (TDD, frequent commits):
  1. tests/test_trading_decisions_router.py  — start with test_authenticated_create_session and test_unauthenticated_request_returns_401, drive scaffolding from there.
  2. app/schemas/trading_decisions.py        — schemas exactly per plan §6.
  3. app/services/trading_decision_service.py — add the 3 read helpers per plan §8 with their own unit tests.
  4. app/routers/trading_decisions.py        — endpoints per plan §7, one at a time, each behind a failing test first.
  5. app/main.py                             — register the router.
  6. tests/test_trading_decisions_router_safety.py — subprocess forbidden-import test.
  7. Run the verification commands in plan §10.5; everything must pass.

When you finish each endpoint, commit with: `git commit -m "feat(rob-2): <verb> <endpoint>"`.

Open a PR against main when §12 acceptance checklist is fully green.
```

---

## 15. Open decisions (defaults chosen, easy to revisit in review)

1. **Path parameter naming: `session_uuid` / `proposal_uuid` vs `session_id` / `proposal_id`.** → Use UUIDs. The roadmap uses `{session_id}` informally; FastAPI will type-validate the UUID string. Rationale: never leak DB sequence ids to the client; UUIDs are already stamped on every row.
2. **One router or split into `decisions_router` + `proposals_router`.** → One router, one module. Eight endpoints, two URL roots, one logical concern.
3. **Decimal serialization.** → Pydantic v2 default (string). Front-end can use `decimal.js`. Floats would lose precision on `100000000` BTC prices, etc.
4. **Pagination default 50, max 200.** → Conservative; revisit when the inbox view actually exists in ROB-4.
5. **Eager-loading strategy.** → `selectinload` for proposals → actions / counterfactuals / outcomes. Avoids N+1 without a giant JOIN cartesian.
6. **Status filter on list endpoint.** → Optional `status=open|closed|archived`. Useful for the future inbox UI; cheap to add now.
7. **No bulk respond endpoint.** → Each proposal is responded to individually. Roadmap UX (“accept BTC, accept ETH, defer SOL”) is N×POST. Bulk endpoint can be added in ROB-4 if it proves a real ergonomic need.
8. **No PATCH/DELETE.** → ROB-2 ships create + read. Cancel/archive is deferred until ROB-4 product UX defines it.
9. **Logging.** → Standard module logger; no Sentry custom tags in this PR. Sentry SQLAlchemy integration covers DB-level errors automatically.
10. **Rate limiting.** → Not added. Existing `slowapi` setup applies app-wide; no per-endpoint limits needed for the workflow.
