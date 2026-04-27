# ROB-1 — Trading Decision DB Schema/Design Plan

- **PR scope:** Prompt 1 of `auto_trader_trading_decision_workspace_roadmap.md` only.
- **Branch / worktree:** `feature/ROB-1-trading-decision-db-schema` (this PR is a single unit).
- **Status:** Plan only. No code changes yet.

> ⚠️ This PR ships **DB layer only**. API endpoints, FastAPI routers, React/Vite scaffold, decision UI, outcome dashboards, periodic reassessment, Discord delivery, and broker/watch execution are explicitly deferred to ROB-2~5.

---

## 1. Goal

Lay the persistence foundation for recording analyst-generated trading recommendations and the user's response to them, so later PRs can read/write through stable contracts.

The schema must preserve **both**:
- the **immutable** original analyst recommendation, and
- the **user-selected / user-adjusted** decision,

so that downstream analytics (ROB-5) can attribute outcomes to the right track.

---

## 2. In-scope vs Out-of-scope

| Area | In scope (this PR) | Deferred |
|---|---|---|
| SQLAlchemy models | ✅ 5 tables under `app/models/` | — |
| Alembic migration | ✅ single revision | — |
| Service / repository functions | ✅ pure persistence in `app/services/trading_decision_service.py` | broker/watch hooks |
| Tests | ✅ model + service unit tests | API integration tests |
| FastAPI routes | ❌ | ROB-2 |
| Pydantic request/response schemas | ❌ (only typed dataclasses needed by service signatures) | ROB-2 |
| React/Vite scaffold | ❌ | ROB-3 |
| Decision workspace UI | ❌ | ROB-4 |
| Outcome/analytics views | ❌ | ROB-5 |
| Periodic reassessment job | ❌ | future |
| Discord notification of proposals | ❌ | future |
| KIS/Upbit/Redis token side effects | ❌ (forbidden — see §7) | — |
| Live order placement / watch alert registration | ❌ (forbidden — see §7) | — |

---

## 3. Workflow the schema must support

From the roadmap:

```text
Analyst proposes:
- BTC 20% trim at 117,800,000
- ETH pullback watch
- SOL pullback watch
- avoid chasing ORCA/ZBT

User may respond:
- accept BTC exactly
- modify BTC 20% -> 10%
- accept ETH only
- reject/defer SOL
- accept avoid/no-action proposal
```

The schema must let us answer these queries later without losing fidelity:

1. *What did the analyst originally recommend?* → frozen on the proposal row.
2. *What did the user decide?* → user-response columns + (optional) user-adjusted columns.
3. *Which proposals were turned into actual orders / watch alerts?* → `trading_decision_actions` rows with **external IDs only**.
4. *What would have happened if we'd taken the rejected route?* → `trading_decision_counterfactuals`.
5. *Compare accepted-live vs rejected-counterfactual at 1h / 4h / 1d / 3d / 7d / final* → `trading_decision_outcomes`.

---

## 4. Schema design

### 4.1 Tables (overview)

```text
trading_decision_sessions            (1) ── (N) trading_decision_proposals
trading_decision_proposals           (1) ── (N) trading_decision_actions
trading_decision_proposals           (1) ── (N) trading_decision_counterfactuals
trading_decision_proposals           (1) ── (N) trading_decision_outcomes
trading_decision_counterfactuals     (1) ── (N) trading_decision_outcomes  (optional FK)
```

Decisions:
- All five tables live in the **default `public` schema** (consistent with `portfolio_decision_runs`, not `review`/`paper`).
- Surrogate `BigInteger` PKs everywhere; **plus** a UUID column on session/proposal for stable external references that won't leak DB sequence info to a future API/UI.
- All timestamps are `TIMESTAMP(timezone=True)` with `server_default=func.now()`.
- All "kind"/"status" enums are stored as **CHECK-constrained `Text`** (matching `trade_journals` precedent) rather than PG ENUM types — easier to evolve, easier to migrate.

### 4.2 `trading_decision_sessions`

One row per analyst proposal *batch* (e.g. one Hermes morning slate).

| Column | Type | Notes |
|---|---|---|
| `id` | `BigInteger` PK | |
| `session_uuid` | `UUID`, unique, indexed | external/API-stable id |
| `user_id` | `BigInteger` FK → `users.id` ON DELETE CASCADE | who the slate is *for* |
| `source_profile` | `Text` | e.g. `"hermes"`, future `"day-trader"`. Not enum-locked. |
| `strategy_name` | `Text` nullable | optional label |
| `market_scope` | `Text` nullable | e.g. `"crypto"`, `"kr"`, `"us"`, `"mixed"` |
| `market_brief` | `JSONB` nullable | analyst context payload (free-form snapshot) |
| `status` | `Text` (CHECK in `'open','closed','archived'`) default `'open'` | lifecycle |
| `notes` | `Text` nullable | |
| `generated_at` | `TIMESTAMP(tz)` not null | when analyst produced the slate |
| `created_at` | `TIMESTAMP(tz)` server default now() | |
| `updated_at` | `TIMESTAMP(tz)` server default now(), onupdate now() | |

Indexes:
- `ix_trading_decision_sessions_user_generated_at (user_id, generated_at DESC)`
- `ix_trading_decision_sessions_status (status)`
- unique on `session_uuid`

### 4.3 `trading_decision_proposals`

One row per recommended action inside a session.

```text
A proposal has IMMUTABLE original_* fields populated at creation,
plus MUTABLE user_response_* fields populated when the user responds.
Modifying user_response_* MUST NOT mutate any original_* column.
```

| Column | Type | Notes |
|---|---|---|
| `id` | `BigInteger` PK | |
| `proposal_uuid` | `UUID`, unique, indexed | API-stable id |
| `session_id` | `BigInteger` FK → `trading_decision_sessions.id` ON DELETE CASCADE | |
| `symbol` | `Text` not null | DB-canonical form (`.` separator for US — see CLAUDE.md) |
| `instrument_type` | `instrument_type` enum (existing) | reuse `app.models.trading.InstrumentType` |
| `proposal_kind` | `Text` CHECK in `('trim','add','enter','exit','pullback_watch','breakout_watch','avoid','no_action','other')` | what *kind* of proposal — covers BTC trim, ETH/SOL watch, ORCA/ZBT avoid |
| `side` | `Text` CHECK in `('buy','sell','none')` default `'none'` | |
| **— immutable original recommendation —** | | |
| `original_quantity` | `Numeric(20,8)` nullable | |
| `original_quantity_pct` | `Numeric(8,4)` nullable | percent of position (e.g. 20% trim) |
| `original_amount` | `Numeric(20,4)` nullable | currency-denominated size |
| `original_price` | `Numeric(20,8)` nullable | suggested limit price |
| `original_trigger_price` | `Numeric(20,8)` nullable | for watch/breakout |
| `original_threshold_pct` | `Numeric(8,4)` nullable | |
| `original_currency` | `Text` nullable | `'KRW'`/`'USD'`/`'BTC'`/etc. |
| `original_rationale` | `Text` nullable | analyst's prose |
| `original_payload` | `JSONB` not null | full snapshot of the analyst's structured proposal at creation time (lossless) |
| **— user response —** | | |
| `user_response` | `Text` CHECK in `('pending','accept','reject','modify','partial_accept','defer')` default `'pending'`, indexed | |
| `user_quantity` | `Numeric(20,8)` nullable | only set when `modify` / `partial_accept` |
| `user_quantity_pct` | `Numeric(8,4)` nullable | e.g. 10% override of analyst's 20% |
| `user_amount` | `Numeric(20,4)` nullable | |
| `user_price` | `Numeric(20,8)` nullable | |
| `user_trigger_price` | `Numeric(20,8)` nullable | |
| `user_threshold_pct` | `Numeric(8,4)` nullable | |
| `user_note` | `Text` nullable | |
| `responded_at` | `TIMESTAMP(tz)` nullable | null while `pending` |
| `created_at` | `TIMESTAMP(tz)` | |
| `updated_at` | `TIMESTAMP(tz)` | |

Constraints:
- CHECK `(user_response = 'pending') = (responded_at IS NULL)` — invariant that pending ⇔ no response timestamp.
- CHECK `proposal_kind IN (...)` and `side IN (...)`.
- CHECK `user_response IN (...)`.

Indexes:
- `ix_trading_decision_proposals_session_id (session_id)`
- `ix_trading_decision_proposals_session_response (session_id, user_response)`
- `ix_trading_decision_proposals_symbol (symbol)`
- unique on `proposal_uuid`

### 4.4 `trading_decision_actions`

Record-only link from a proposal to whatever happened in the **separate** execution flow. **No broker/watch calls happen here.**

| Column | Type | Notes |
|---|---|---|
| `id` | `BigInteger` PK | |
| `proposal_id` | `BigInteger` FK → `trading_decision_proposals.id` ON DELETE CASCADE, indexed | |
| `action_kind` | `Text` CHECK in `('live_order','paper_order','watch_alert','no_action','manual_note')` | |
| `external_order_id` | `Text` nullable | broker order id (string — may include alphanumeric prefixes) |
| `external_paper_id` | `Text` nullable | paper trade id |
| `external_watch_id` | `Text` nullable | watch alert key/id |
| `external_source` | `Text` nullable | e.g. `'kis'`, `'upbit'`, `'paper'`, `'watch_alerts'` |
| `payload_snapshot` | `JSONB` not null | what was approved at action time (price, qty, etc.) |
| `recorded_at` | `TIMESTAMP(tz)` not null default now() | |
| `created_at` | `TIMESTAMP(tz)` | |

Constraints:
- CHECK `action_kind IN (...)`.
- CHECK that at least one of `external_order_id`, `external_paper_id`, `external_watch_id` is non-null **unless** `action_kind IN ('no_action','manual_note')`. (Encoded as a single CHECK expression.)

Indexes:
- `ix_trading_decision_actions_proposal_id (proposal_id)`
- `ix_trading_decision_actions_external_order (external_source, external_order_id)` partial where `external_order_id IS NOT NULL`

### 4.5 `trading_decision_counterfactuals`

Paper / simulated tracks tied to a proposal. **Never live.**

| Column | Type | Notes |
|---|---|---|
| `id` | `BigInteger` PK | |
| `proposal_id` | `BigInteger` FK → `trading_decision_proposals.id` ON DELETE CASCADE | |
| `track_kind` | `Text` CHECK in `('rejected_counterfactual','analyst_alternative','user_alternative','accepted_paper')` | |
| `baseline_price` | `Numeric(20,8)` not null | snapshot price when track was created |
| `baseline_at` | `TIMESTAMP(tz)` not null | |
| `quantity` | `Numeric(20,8)` nullable | hypothetical size |
| `payload` | `JSONB` not null | track parameters (entry, stop, target, etc.) |
| `notes` | `Text` nullable | |
| `created_at` | `TIMESTAMP(tz)` | |

Indexes: `ix_trading_decision_counterfactuals_proposal_id`.

### 4.6 `trading_decision_outcomes`

Mark price / PnL at fixed horizons for a track.

| Column | Type | Notes |
|---|---|---|
| `id` | `BigInteger` PK | |
| `proposal_id` | `BigInteger` FK → `trading_decision_proposals.id` ON DELETE CASCADE | |
| `counterfactual_id` | `BigInteger` FK → `trading_decision_counterfactuals.id` nullable, ON DELETE CASCADE | null when track is `accepted_live` |
| `track_kind` | `Text` CHECK in `('accepted_live','accepted_paper','rejected_counterfactual','analyst_alternative','user_alternative')` | denormalized for query speed |
| `horizon` | `Text` CHECK in `('1h','4h','1d','3d','7d','final')` | |
| `price_at_mark` | `Numeric(20,8)` not null | |
| `pnl_pct` | `Numeric(10,4)` nullable | |
| `pnl_amount` | `Numeric(20,4)` nullable | |
| `marked_at` | `TIMESTAMP(tz)` not null | |
| `payload` | `JSONB` nullable | freeform extras |
| `created_at` | `TIMESTAMP(tz)` | |

Constraints:
- Unique on `(proposal_id, counterfactual_id, track_kind, horizon)` with **`NULLS NOT DISTINCT`** (PostgreSQL ≥ 15 required). Treating NULL `counterfactual_id` values as equal prevents duplicate `accepted_live` marks at the same horizon — the earlier "PostgreSQL treats NULL ≠ NULL" note has been superseded because allowing duplicate accepted-live marks would be a defect. See model comment in `app/models/trading_decision.py`.
- CHECK `track_kind IN (...)`, CHECK `horizon IN (...)`.
- CHECK `(track_kind = 'accepted_live') = (counterfactual_id IS NULL)`.

Indexes: `ix_trading_decision_outcomes_proposal_horizon (proposal_id, horizon)`.

---

## 5. SQLAlchemy models

New file: `app/models/trading_decision.py`

```python
class TradingDecisionSession(Base): ...
class TradingDecisionProposal(Base): ...
class TradingDecisionAction(Base): ...
class TradingDecisionCounterfactual(Base): ...
class TradingDecisionOutcome(Base): ...
```

- Use `Mapped[...]` / `mapped_column(...)` (consistent with current models).
- `relationship(...)` with `back_populates`:
  - `TradingDecisionSession.proposals` ↔ `TradingDecisionProposal.session`
  - `TradingDecisionProposal.actions` ↔ `TradingDecisionAction.proposal`
  - `TradingDecisionProposal.counterfactuals` ↔ `TradingDecisionCounterfactual.proposal`
  - `TradingDecisionProposal.outcomes` ↔ `TradingDecisionOutcome.proposal`
  - `TradingDecisionCounterfactual.outcomes` ↔ `TradingDecisionOutcome.counterfactual`
- Reuse `InstrumentType` from `app.models.trading`.
- Define lightweight `enum.StrEnum` classes for `UserResponse`, `ProposalKind`, `ActionKind`, `TrackKind`, `OutcomeHorizon`, `SessionStatus` — used only at the Python layer (DB stores plain `Text` with CHECK).
- Register the new models in `app/models/__init__.py`.

---

## 6. Alembic migration

- Single revision: `alembic/versions/<hash>_create_trading_decision_tables.py`
- `revision = ...`, `down_revision = <current head>` — must be regenerated against current head; do not hand-pick.
- Creates the five tables in dependency order: sessions → proposals → actions, counterfactuals → outcomes.
- Reuses existing `instrument_type` PG enum (`Enum(..., create_type=False)`).
- All CHECK / UNIQUE / FK constraints created in the same revision (no follow-up).
- `downgrade()` drops in reverse order.
- Verify: `uv run alembic upgrade head && uv run alembic downgrade -1 && uv run alembic upgrade head` round-trips clean against a disposable PG.

---

## 7. Service layer (`app/services/trading_decision_service.py`)

Pure persistence module. **Async** (consistent with rest of codebase).

### 7.1 Public functions

```python
async def create_decision_session(
    session: AsyncSession,
    *,
    user_id: int,
    source_profile: str,
    strategy_name: str | None = None,
    market_scope: str | None = None,
    market_brief: dict | None = None,
    generated_at: datetime,
    notes: str | None = None,
) -> TradingDecisionSession: ...

async def add_decision_proposals(
    session: AsyncSession,
    *,
    session_id: int,
    proposals: Sequence[ProposalCreate],
) -> list[TradingDecisionProposal]: ...

async def record_user_response(
    session: AsyncSession,
    *,
    proposal_id: int,
    response: UserResponse,
    user_quantity: Decimal | None = None,
    user_quantity_pct: Decimal | None = None,
    user_amount: Decimal | None = None,
    user_price: Decimal | None = None,
    user_trigger_price: Decimal | None = None,
    user_threshold_pct: Decimal | None = None,
    user_note: str | None = None,
    responded_at: datetime | None = None,
) -> TradingDecisionProposal: ...

async def record_decision_action(
    session: AsyncSession,
    *,
    proposal_id: int,
    action_kind: ActionKind,
    external_order_id: str | None = None,
    external_paper_id: str | None = None,
    external_watch_id: str | None = None,
    external_source: str | None = None,
    payload_snapshot: dict,
) -> TradingDecisionAction: ...

async def create_counterfactual_track(
    session: AsyncSession,
    *,
    proposal_id: int,
    track_kind: TrackKind,
    baseline_price: Decimal,
    baseline_at: datetime,
    quantity: Decimal | None = None,
    payload: dict,
    notes: str | None = None,
) -> TradingDecisionCounterfactual: ...

async def record_outcome_mark(
    session: AsyncSession,
    *,
    proposal_id: int,
    track_kind: TrackKind,
    horizon: OutcomeHorizon,
    price_at_mark: Decimal,
    counterfactual_id: int | None = None,
    pnl_pct: Decimal | None = None,
    pnl_amount: Decimal | None = None,
    marked_at: datetime,
    payload: dict | None = None,
) -> TradingDecisionOutcome: ...
```

`ProposalCreate` is a small typed dataclass / TypedDict in the same module (no Pydantic — that arrives with ROB-2's API contract).

### 7.2 Invariants enforced in the service layer

- `record_user_response` **must not** mutate any `original_*` column. Implementation reads the row, sets only the user_* / response fields, and `responded_at = responded_at or now()`. A test asserts the original_* columns are byte-identical pre/post.
- `record_decision_action` requires at least one external id unless `action_kind in {'no_action','manual_note'}`. The DB CHECK is the source of truth; the service raises a clear `ValueError` before the DB round-trip for better error messages.
- `record_outcome_mark` requires `counterfactual_id IS NULL ⇔ track_kind == 'accepted_live'`.

### 7.3 Forbidden imports (safety boundary)

This module **must not** import (directly or transitively from its own siblings) any of:

```text
app.services.kis
app.services.kis_trading_service
app.services.kis_trading_contracts
app.services.upbit  (and upbit_websocket, upbit_*)
app.services.brokers.*
app.services.order_service
app.services.fill_notification
app.services.execution_event
app.services.redis_token_manager
app.services.kis_websocket*
anything under app.tasks/  that triggers orders, watches, or token refresh
```

A test (see §8) asserts this by inspecting `sys.modules` after importing `trading_decision_service`.

---

## 8. Tests

Two new test files, both `pytest`-async with the existing fixture conventions.

### 8.1 `tests/models/test_trading_decision_models.py`

Pure model / mapping tests against an empty test DB (use existing fixture for an isolated session):

- `test_session_with_proposals_round_trips` — insert session + 3 proposals (BTC trim 20%, ETH pullback_watch, SOL pullback_watch), reload, assert relationships and original payload integrity.
- `test_proposal_check_constraints` — invalid `proposal_kind` / `user_response` / `side` raise IntegrityError.
- `test_pending_response_invariant` — setting `user_response='accept'` without `responded_at` violates CHECK; setting both passes.
- `test_action_external_id_required` — `action_kind='live_order'` without any external id violates CHECK; with order id passes.
- `test_outcome_unique_per_horizon` — two `('rejected_counterfactual','1h')` rows for the same `(proposal_id, counterfactual_id)` violate the unique index; different horizon passes.
- `test_outcome_accepted_live_requires_null_counterfactual` — track_kind `accepted_live` with non-null counterfactual_id violates CHECK.
- `test_cascade_delete_session` — deleting a session cascades through proposals → actions / counterfactuals / outcomes.

### 8.2 `tests/models/test_trading_decision_service.py`

Service-level scenarios from the roadmap:

1. `test_create_session_with_btc_eth_sol_proposals` — BTC trim 20%, ETH pullback_watch, SOL pullback_watch saved with frozen `original_*`.
2. `test_modify_btc_proposal_20_to_10` — `record_user_response(response='modify', user_quantity_pct=Decimal('10'))`. Assert `original_quantity_pct == 20`, `user_quantity_pct == 10`, `responded_at` set.
3. `test_select_subset_btc_eth_reject_sol` — accept BTC, accept ETH, `defer` SOL. Assert per-proposal status and that nothing leaks across proposals.
4. `test_record_live_order_action_no_broker_call` — `record_decision_action(action_kind='live_order', external_order_id='KIS-12345', external_source='kis', payload_snapshot=...)`. Assert no KIS module import path is touched (monkeypatch `sys.modules` sentinel + assertion).
5. `test_record_watch_alert_action_no_watch_registration` — analogous for `action_kind='watch_alert'`, `external_watch_id='WA-…'`.
6. `test_create_rejected_proposal_counterfactual` — proposal rejected, then `create_counterfactual_track(track_kind='rejected_counterfactual', baseline_price=…)`.
7. `test_record_1h_and_1d_outcome_marks` — record 1h and 1d marks for the counterfactual; assert PnL fields stored, unique index respected.
8. `test_record_user_response_does_not_mutate_original_fields` — snapshot original_* columns before/after, assert equality.
9. `test_service_module_does_not_import_execution_paths` — import `trading_decision_service`, then assert none of the forbidden modules listed in §7.3 are present in `sys.modules` *as a result of that import alone* (use a subprocess or `importlib` reload pattern to keep the assertion robust to test ordering).

### 8.3 Verification commands

```bash
uv run alembic upgrade head
uv run pytest tests/models/test_trading_decision_models.py -q
uv run pytest tests/models/test_trading_decision_service.py -q
uv run ruff check app/ tests/
uv run alembic downgrade -1 && uv run alembic upgrade head   # round-trip
```

---

## 9. File-by-file changeset

| File | Action |
|---|---|
| `app/models/trading_decision.py` | **new** — 5 ORM classes + `StrEnum` definitions |
| `app/models/__init__.py` | extend `__all__` and imports |
| `app/services/trading_decision_service.py` | **new** — async service functions + dataclasses |
| `alembic/versions/<hash>_create_trading_decision_tables.py` | **new** — single migration |
| `tests/models/test_trading_decision_models.py` | **new** |
| `tests/models/test_trading_decision_service.py` | **new** |
| `docs/plans/ROB-1-trading-decision-db-schema-plan.md` | **this file** |

No existing files are modified beyond `app/models/__init__.py`. No API router, no template, no settings.

---

## 10. Open decisions (defaults chosen, easy to revisit in review)

1. **CHECK + Text vs PG ENUM for `proposal_kind`/`user_response`/etc.** → CHECK + Text. Rationale: matches `trade_journals`; lower migration cost when adding values.
2. **UUID column on session/proposal even though API is deferred to ROB-2.** → keep. Rationale: cheap now, expensive to backfill later, lets ROB-2 expose `:session_uuid` without leaking sequence ids.
3. **Schema location: `public` (no namespace)**. Rationale: this is general-purpose decision data; `paper`/`review` schemas are domain-specific.
4. **No soft-delete column.** Rationale: cascade delete from session is sufficient; outcomes integrity matters more than recoverability for this dataset. Re-evaluate if ROB-5 needs audit history.
5. **`source_profile` is free-form `Text`, not enum.** Rationale: roadmap §Future-note explicitly anticipates new profiles (`day-trader`, etc.); enum churn isn't worth it.
6. **`original_payload` and `payload_snapshot` are JSONB, not normalized.** Rationale: they exist to preserve fidelity of upstream payloads; normalizing would defeat the immutability requirement.
7. **No FK from `trading_decision_actions` to `paper_trades` / `review.trades` etc.** Rationale: actions store **string external IDs** to avoid coupling cross-schema and to avoid implying execution responsibility.
8. **Outcome unique index uses `NULLS NOT DISTINCT`.** Rationale: see §4.6. Requires PostgreSQL ≥ 15 (production runs PG 17). If a future deployment must run on PG ≤ 14, replace this with a service-level guard in `record_outcome_mark` that pre-checks for an existing `(proposal_id, counterfactual_id IS NULL, 'accepted_live', horizon)` row.

---

## 11. Out of scope reminders (do not creep)

If during implementation any of these is tempting, **stop and split into a new PR**:

- Adding a FastAPI router → ROB-2.
- Adding a Pydantic request/response model used outside the service signatures → ROB-2.
- Wiring proposals into Hermes / analyst pipelines → out of this PR.
- Wiring actions into the live order flow or watch alert registration → forbidden (§7.3).
- Adding a Discord brief or notification → out of scope.
- Adding any `frontend/` directory or React tooling → ROB-3.

---

## 12. Acceptance checklist (used at PR review time)

- [ ] 5 tables created via single Alembic revision, head moves cleanly.
- [ ] `alembic downgrade -1 && alembic upgrade head` round-trips with no errors.
- [ ] All CHECK / UNIQUE / FK constraints from §4 present in the migration.
- [ ] SQLAlchemy models registered in `app/models/__init__.py`.
- [ ] Service module exposes the six functions listed in §7.1, all `async`.
- [ ] No imports from §7.3 forbidden list in the new module (test enforced).
- [ ] `record_user_response` provably does not mutate `original_*` (test enforced).
- [ ] All tests in §8 pass.
- [ ] `ruff check app/ tests/` clean.
- [ ] `docs/plans/ROB-1-trading-decision-db-schema-plan.md` is the only doc; no API/UI docs added.
