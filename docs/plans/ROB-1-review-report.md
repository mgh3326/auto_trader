# ROB-1 Implementation Review — Plan Conformance Report

- **Branch:** `feature/ROB-1-trading-decision-db-schema`
- **Reviewed against:** `docs/plans/ROB-1-trading-decision-db-schema-plan.md`
- **Implementer report:** "DB 모델, 서비스 레이어, Alembic migration, 모델/서비스 테스트 구현 완료. pytest 12 passed, make lint/ty passed"
- **Reviewer scope:** code is **not** modified — review only.

---

## 0. TL;DR

- **Plan scope respected.** No API router, no React/Vite, no UI, no analytics, no Discord, no broker/watch side effects, no `place_order` / `manage_watch_alerts` / KIS / Upbit / Redis token imports in production paths.
- **Schema, models, migration are substantially aligned with the plan**, with a few small deviations (one missing index, one stricter unique-index semantics than plan, one silent dependency on PostgreSQL ≥ 15).
- **Test coverage is the weakest area.** 12 of 16 planned cases were implemented; the safety-critical "service module does not import execution paths" test was *folded into* a functional test and only checks 3 of the 10+ forbidden modules listed in plan §7.3. Tests are also not properly isolated: they commit data without cleanup and reuse a hard-coded `test_user` username — flaky against shared `test_db`.
- **Recommendation:** **Fix the must-fix items below before merge** (forbidden-import test hardening, test isolation, missing watch-alert / counterfactual / SOL-defer scenarios). Other deviations can be follow-ups documented in the plan.

---

## 1. Files reviewed (all present)

`git status` confirms exactly the file set predicted by plan §9, no extras:

| File | State | Plan §9 expectation |
|---|---|---|
| `app/models/trading_decision.py` | new | ✅ |
| `app/models/__init__.py` | modified (only imports + `__all__`) | ✅ |
| `app/services/trading_decision_service.py` | new | ✅ |
| `alembic/versions/ce5d470cc894_create_trading_decision_tables.py` | new | ✅ |
| `tests/test_trading_decision_models.py` | new | ✅ |
| `tests/test_trading_decision_service.py` | new | ✅ |
| `docs/plans/ROB-1-trading-decision-db-schema-plan.md` | new (already in tree) | ✅ |

`git diff --stat` shows only `__init__.py` (24 lines added). No unrelated files touched. **No commits yet** on the branch — all work is unstaged/untracked. Implementer should commit before opening the PR.

`uv run alembic heads` returns a single head `ce5d470cc894` — the new migration cleanly extends the previous head `0f4a7c9d3e21` with no merge or branch.

---

## 2. Scope discipline (criterion 1 + 2)

### 2.1 Out-of-scope items confirmed absent

I checked every new/modified file. None of the deferred areas leaked in:

- ❌ No FastAPI router, `APIRouter`, `Depends`, route decorators.
- ❌ No Pydantic `BaseModel` (only a `TypedDict` for service input — allowed by plan §7.1).
- ❌ No `frontend/`, `package.json`, `tsconfig.json`, `vite.config.ts`, `.tsx`, `.ts`.
- ❌ No Discord, Telegram, n8n, webhook, or notification code.
- ❌ No outcome dashboard, chart, or analytics aggregation.
- ❌ No periodic reassessment task / scheduler.

### 2.2 Forbidden side-effect imports (criterion 3) — production paths

`app/services/trading_decision_service.py` imports only:

```python
from collections.abc import Sequence
from datetime import datetime
from decimal import Decimal
from typing import TypedDict
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from app.models.trading import InstrumentType
from app.models.trading_decision import (...)
```

`app/models/trading_decision.py` imports only stdlib + sqlalchemy + `app.models.base` + `app.models.trading.InstrumentType`.

**Verdict:** Zero side-effect risk in production code. None of `place_order`, `manage_watch_alerts`, `app.services.kis*`, `app.services.upbit*`, `app.services.brokers.*`, `app.services.order_service`, `app.services.fill_notification`, `app.services.execution_event`, `app.services.redis_token_manager`, `app.services.kis_websocket*`, `app.tasks/*` are touched.

This satisfies plan §7.3 *for the production module*. The test-side enforcement of this invariant is weaker — see §5 below.

---

## 3. Schema / model conformance (criterion 4)

### 3.1 Tables, columns, FKs

All 5 tables present, every column from plan §4.2–4.6 is present with the correct type/nullability/default. CASCADE FKs as specified. CHECK constraints match plan text verbatim (whitespace-only differences). `instrument_type` PG enum reused with `create_type=False`. ✅

### 3.2 Indexes — one missing

Plan §4.2 lists three sessions indexes:

- ✅ `ix_trading_decision_sessions_user_generated_at (user_id, generated_at DESC)` — present.
- ✅ unique on `session_uuid` — present.
- ❌ **`ix_trading_decision_sessions_status (status)` — MISSING** in both model `__table_args__` and migration.

Severity: **Minor.** The status filter will rarely be the dominant predicate in a query (sessions per user is small). Acceptable as a follow-up.

Remaining indexes from §4.3–4.6 are all present, matching the plan including the partial index on `(external_source, external_order_id) WHERE external_order_id IS NOT NULL` and the unique outcome track-identity index.

### 3.3 Outcome unique index — semantic deviation (with hidden PG ≥ 15 dependency)

Plan §4.6:

> Unique on `(proposal_id, counterfactual_id, track_kind, horizon)` — prevents duplicate marks at the same horizon for the same track. (`counterfactual_id` is part of the key; PostgreSQL treats `NULL ≠ NULL` so accepted-live rows won't collide via this column — that's intended.)

Implementation (`app/models/trading_decision.py:304–312`, mirrored in migration line 150):

```python
Index(
    "ix_trading_decision_outcomes_track_identity",
    "proposal_id", "counterfactual_id", "track_kind", "horizon",
    unique=True,
    postgresql_nulls_not_distinct=True,
)
```

`postgresql_nulls_not_distinct=True` causes PG 15+ to **treat NULL counterfactual_ids as equal**, so two `accepted_live` rows with the same `(proposal_id, '1h')` *will* collide — the opposite of the plan's stated intent.

This is actually a **functional improvement** (the plan's stated semantics would have allowed duplicate `accepted_live` 1h marks for the same proposal — almost certainly a bug). The test `test_outcome_unique_per_horizon` codifies the new behavior and passes.

But it introduces an undocumented dependency:

- **PostgreSQL must be ≥ 15.** On PG ≤ 14 the `nulls_not_distinct` keyword is rejected and `alembic upgrade head` will fail.
- The plan was not updated to reflect this design choice.

Severity: **Must-fix-before-merge if the production DB is < 15. Otherwise, must-document.**

### 3.4 Migration round-trip integrity

`alembic heads` clean. Migration `down_revision='0f4a7c9d3e21'` matches the prior head. Tables dropped in correct reverse order in `downgrade()`. Indexes/constraints attached to tables are dropped implicitly via `drop_table` — fine.

I did **not** independently run `alembic upgrade head && downgrade -1 && upgrade head` against a disposable DB; the implementer should do this and report. (Plan §6 listed it as a verification step.)

---

## 4. Service layer conformance (criterion 4)

### 4.1 Function signatures

All six functions from plan §7.1 are present and `async`:

`create_decision_session`, `add_decision_proposals`, `record_user_response`, `record_decision_action`, `create_counterfactual_track`, `record_outcome_mark`. ✅

`ProposalCreate` is implemented as `TypedDict, total=False` rather than a typed dataclass. Acceptable — plan §7.1 allowed either.

### 4.2 Invariants

- ✅ `record_user_response` does not touch any `original_*` column (lines 121–131 only assign to `user_*` and `responded_at`).
- ✅ `record_decision_action` raises `ValueError` when no external id is provided for non-`no_action`/`manual_note` kinds (lines 151–155). DB CHECK is the source of truth as planned.
- ✅ `record_outcome_mark` enforces `counterfactual_id IS NULL ⇔ track_kind == 'accepted_live'` at the service level (lines 214–219). DB CHECK also enforces it.

### 4.3 Subtle behavior worth noting (not a defect, but follow-up consideration)

`record_user_response` **unconditionally writes all `user_*` columns**, including those defaulted to `None`. So calling `record_user_response(response='accept', responded_at=...)` after a previous `record_user_response(response='modify', user_quantity_pct=10)` will **clear `user_quantity_pct` to NULL**. That matches the typed signature (caller passes the full state), but it's not "patch" semantics. If ROB-2's API exposes this as a PATCH-style endpoint, the API layer will need to read-modify-write itself.

Severity: **Follow-up** — flag in ROB-2 design.

---

## 5. Test conformance (criterion 4 + 6)

### 5.1 Coverage gap vs plan

Plan §8 listed 16 tests (7 model + 9 service). Implementation has 12 tests (7 model + 5 service) — matches the reported "12 passed".

| Plan test | Status |
|---|---|
| **§8.1 Model tests** | |
| test_session_with_proposals_round_trips | ✅ |
| test_proposal_check_constraints | ⚠️ partial — only `proposal_kind` is exercised; `side` and `user_response` enum violations are not |
| test_pending_response_invariant | ✅ |
| test_action_external_id_required | ✅ |
| test_outcome_unique_per_horizon | ✅ |
| test_outcome_accepted_live_requires_null_counterfactual | ✅ |
| test_cascade_delete_session | ✅ (covers session→proposal→outcome; does not exercise actions or counterfactuals — minor) |
| **§8.2 Service tests** | |
| test_create_session_with_btc_eth_sol_proposals | ⚠️ partial — only BTC + ETH; **no SOL proposal**, no avoid/no-action proposal |
| test_modify_btc_proposal_20_to_10 | ✅ |
| test_select_subset_btc_eth_reject_sol | ❌ **MISSING** |
| test_record_live_order_action_no_broker_call | ✅ (combined with import-safety check — see §5.2) |
| test_record_watch_alert_action_no_watch_registration | ❌ **MISSING** |
| test_create_rejected_proposal_counterfactual | ❌ **MISSING** as standalone (functionality is exercised inside `test_record_outcome_marks` but the dedicated invariant test is absent) |
| test_record_1h_and_1d_outcome_marks | ✅ (`test_record_outcome_marks`) |
| test_record_user_response_does_not_mutate_original_fields | ⚠️ partial — only checks `original_quantity_pct` and `original_payload`; plan asked for byte-identical comparison of *all* `original_*` columns |
| test_service_module_does_not_import_execution_paths | ❌ **NOT IMPLEMENTED AS PLANNED** — see §5.2 |

### 5.2 Forbidden-import test is too weak

Plan §8.2.9 required a dedicated test that:

> uses a subprocess or `importlib` reload pattern to keep the assertion robust to test ordering

The implementation instead embeds 3 `assert ... not in sys.modules` checks at the **end** of `test_record_live_order_action_no_broker_call` (lines 175–182), against only:

```python
forbidden = ["app.services.kis", "app.services.upbit", "app.tasks"]
```

Problems:
1. **Order-sensitive**: any earlier test in the run that imports any forbidden module from any *other* module (transitively, e.g. `from app.services.upbit_websocket import ...` somewhere) will leave it in `sys.modules` and this assertion will fire — yet not because `trading_decision_service.py` imported it.
2. **Coverage gap**: only checks 3 of the 10+ modules listed in plan §7.3. Missing: `kis_trading_service`, `kis_trading_contracts`, `brokers.*`, `order_service`, `fill_notification`, `execution_event`, `redis_token_manager`, `kis_websocket*`.
3. **Wrong assertion target**: the test asserts global `sys.modules` state, not "imports caused by importing `trading_decision_service`".

Severity: **Must-fix.** The whole point of plan §7.3 + §8.2.9 is a regression-proof guard against future implementers wiring the service into broker code. The current assertion will not catch that — it will either falsely fail (if some other test imports KIS first) or falsely pass (if the forbidden module is loaded before the assertion runs but `trading_decision_service` does not actually import it).

### 5.3 Test isolation problems

Both new test files use this pattern:

```python
@pytest_asyncio.fixture
async def db_session():
    async with SessionLocal() as session:
        yield session
        await session.rollback()

@pytest_asyncio.fixture
async def test_user_id(db_session):
    result = await db_session.execute(text("SELECT id FROM users LIMIT 1"))
    user_id = result.scalar()
    if not user_id:
        # INSERT ... commit()  # NOT rolled back
```

Issues compared with the codebase's own pattern in `tests/models/test_user_settings.py` (which uses unique UUID-suffixed usernames + explicit `_cleanup_user` in `try/finally` + `@pytest.mark.integration` skip-if-DB-missing + table-existence guard):

1. **Hard-coded username `'test_user'`** — collides with any pre-existing row of the same name; the `SELECT id FROM users LIMIT 1` query is even worse, returning the *first* user in the table regardless of username, so on a populated DB the FK target is some unrelated real user.
2. **`test_user_id` commits and never cleans up** — leaks a row across test runs.
3. **Several tests call `await db_session.commit()`** (e.g. `test_session_with_proposals_round_trips`, `test_modify_btc_proposal_20_to_10`, `test_cascade_delete_session`) — committed data survives the fixture's `rollback()` and accumulates `trading_decision_sessions`/`proposals`/`outcomes` rows in `test_db`.
4. **No `@pytest.mark.integration` marker** — these tests run on every `make test`, not gated. If a CI runner doesn't have `test_db` provisioned, the test session fails hard instead of skipping.
5. **No table-existence guard** — `test_user_settings.py` skips when the table isn't migrated; these tests just crash.

Severity: **Must-fix before merge.** "12 passed" today does not mean "12 will pass tomorrow on a fresh runner" or "12 will keep passing after another developer runs the suite locally". The plan §8 explicitly said "use existing fixture conventions"; the existing convention in this repo is the `test_user_settings.py` pattern, not this one.

### 5.4 What "12 passed" does *not* prove (criterion 6 — additional verification)

The implementer reported `pytest 12 passed, make lint/ty passed`. Things that have **not** been verified and should be before merge:

1. `uv run alembic upgrade head && uv run alembic downgrade -1 && uv run alembic upgrade head` round-trip on a disposable PG (plan §6).
2. `uv run alembic check` (no autogenerate drift).
3. Running tests on an **empty** `test_db` (truncate first) to detect dependence on leaked data.
4. Running tests **twice in a row** without DB reset, to detect state pollution between runs.
5. PostgreSQL version of `test_db` is ≥ 15 (required by `postgresql_nulls_not_distinct`).
6. `make security` (plan didn't mandate it but the project Makefile has it — bandit/safety on new files).
7. `make typecheck` is reported passing, but `ProposalCreate` uses `total=False` while `add_decision_proposals` reads `p["symbol"]`/`p["instrument_type"]`/`p["proposal_kind"]`/`p["original_payload"]` as required keys. ty may or may not flag this depending on configuration. Reading the actual `make typecheck` output is worthwhile.

---

## 6. Must-fix vs Follow-up (criterion 7)

### 6.1 Must fix before merge

| # | Issue | Location |
|---|---|---|
| M1 | Forbidden-import test is order-sensitive and covers only 3/10+ modules. Replace with the subprocess/`importlib` pattern from plan §8.2.9 and the full forbidden list from plan §7.3. | `tests/test_trading_decision_service.py:175–182` |
| M2 | Add the missing planned tests: `test_select_subset_btc_eth_reject_sol`, `test_record_watch_alert_action_no_watch_registration`, `test_create_rejected_proposal_counterfactual`. | `tests/test_trading_decision_service.py` |
| M3 | Test isolation: switch fixtures to the `test_user_settings.py` pattern (UUID-suffixed username, explicit cleanup in `try/finally`, table-existence skip guard, `@pytest.mark.integration` marker). Stop committing decision-table rows that are never cleaned up. | both new test files |
| M4 | Either confirm production PG is ≥ 15 and update plan §4.6 to document the `postgresql_nulls_not_distinct=True` choice, **or** drop that argument and accept that two `accepted_live` rows at the same horizon would be allowed (and add a service-level guard if so). | `app/models/trading_decision.py:304–312`, migration line 150, plan §4.6 |
| M5 | Commit the work. Branch currently has zero commits ahead of `main`; PR cannot be opened. | git |

### 6.2 Recommended fixes (strong preference, can ship as part of M1–M5 batch)

| # | Issue | Location |
|---|---|---|
| R1 | Strengthen `test_proposal_check_constraints` to also cover `side` and `user_response` enum violations. | `tests/test_trading_decision_models.py` |
| R2 | Strengthen `test_record_user_response_does_not_mutate_original_fields` to compare *every* `original_*` column (and `original_payload`) before/after, not just two. | `tests/test_trading_decision_service.py` |
| R3 | Add the BTC/ETH/**SOL** proposal in `test_create_session_with_btc_eth_sol_proposals` so the test name matches its content. | `tests/test_trading_decision_service.py` |
| R4 | Run `alembic upgrade → downgrade -1 → upgrade` round-trip on a disposable DB and report the result in the PR description. | verification |

### 6.3 Follow-up (not blocking this PR)

| # | Issue | Notes |
|---|---|---|
| F1 | Missing index `ix_trading_decision_sessions_status (status)` from plan §4.2. | New migration in a follow-up PR. Plan can be amended now to drop this requirement if it's deemed unnecessary. |
| F2 | `record_user_response` is full-overwrite, not patch semantics. Document this for ROB-2 API design so PATCH-style endpoints handle merging. | Plan ROB-2 design note. |
| F3 | `test_cascade_delete_session` doesn't exercise cascade through `actions` or `counterfactuals`. | Add later. |

---

## 7. Specific edit instructions for the implementer pane

Copy-paste ready directives. The implementer should treat each numbered item as a discrete TODO.

---

**[FIX-1] Replace the in-test forbidden-import assertion with a hardened, isolated test.**

In `tests/test_trading_decision_service.py`, **remove** the inline assertion block at lines 175–182 (inside `test_record_live_order_action_no_broker_call`).

Add a new dedicated test that runs `trading_decision_service`'s import in a subprocess and asserts no forbidden module ends up in `sys.modules` of that subprocess. The full forbidden list from plan §7.3 must be enforced:

```
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

Use a subprocess (e.g. `subprocess.run([sys.executable, "-c", "..."])`) or `importlib` with a clean module cache so the test does not depend on what previous tests imported into the global `sys.modules`. The test must fail if *any* prefix-matching module ends up loaded as a transitive consequence of `import app.services.trading_decision_service`.

---

**[FIX-2] Add the three missing service tests from plan §8.2.**

In `tests/test_trading_decision_service.py`:

1. `test_select_subset_btc_eth_reject_sol` — create a session, add three proposals (BTC trim, ETH pullback_watch, SOL pullback_watch), then call `record_user_response` with `accept` for BTC, `accept` for ETH, `defer` for SOL. Assert each row's `user_response` and `responded_at`, and assert that BTC/ETH responses do not affect SOL.
2. `test_record_watch_alert_action_no_watch_registration` — analogous to `test_record_live_order_action_no_broker_call` but with `action_kind=ActionKind.watch_alert`, `external_watch_id="WA-1"`, `external_source="watch_alerts"`. Assert the action persists and (covered by FIX-1) no watch-registration module is imported.
3. `test_create_rejected_proposal_counterfactual` — reject a proposal via `record_user_response(response=UserResponse.reject, ...)`, then call `create_counterfactual_track(track_kind=TrackKind.rejected_counterfactual, baseline_price=..., baseline_at=..., payload=...)`. Assert the counterfactual is linked to the proposal and the proposal's `user_response` is unchanged by the counterfactual creation.

---

**[FIX-3] Rewrite the test fixtures to match the codebase isolation convention.**

In **both** `tests/test_trading_decision_models.py` and `tests/test_trading_decision_service.py`:

1. Mark every test with `@pytest.mark.integration` (in addition to `@pytest.mark.asyncio`).
2. Add a `_ensure_trading_decision_tables()` helper that does `SELECT to_regclass('trading_decision_sessions')` and `pytest.skip(...)` if the migration hasn't been applied — same shape as `_ensure_user_settings_table` in `tests/models/test_user_settings.py`.
3. Replace the `test_user_id` fixture with a `_create_user()` / `_cleanup_user(user_id)` pair that uses a UUID-suffixed username and `try/finally` cleanup, mirroring `tests/models/test_user_settings.py`. Stop using `SELECT id FROM users LIMIT 1` — that returns an arbitrary unrelated row on a populated DB.
4. Audit every `await db_session.commit()` call in these two files. For tests that need committed data to test cascade delete behavior, perform the cleanup inside the same `try/finally` block that creates the user. The new fixture must leave the `trading_decision_*` tables empty after each test.
5. Move both files into `tests/models/` to match the existing convention for DB-backed tests, **or** justify keeping them in `tests/` root in the PR description.

---

**[FIX-4] Resolve the PostgreSQL version dependency in the outcome unique index.**

In `app/models/trading_decision.py:304–312` and `alembic/versions/ce5d470cc894_create_trading_decision_tables.py:150`:

Confirm with the team whether the production PostgreSQL is **≥ 15**. Then choose **one** path:

- **Path A (recommended if PG ≥ 15):** Keep `postgresql_nulls_not_distinct=True`. Update `docs/plans/ROB-1-trading-decision-db-schema-plan.md` §4.6 to state explicitly that the unique index uses NULLS NOT DISTINCT, that PG ≥ 15 is required, and that the design intentionally diverges from the plan's earlier "NULL ≠ NULL" note (because the earlier note would have allowed duplicate `accepted_live` marks at the same horizon — a defect). Add a comment in the model file pointing at the plan section.
- **Path B (if PG < 15):** Remove `postgresql_nulls_not_distinct=True` from both the model and the migration. Add a service-level guard in `record_outcome_mark` that, when `track_kind == TrackKind.accepted_live` and `counterfactual_id is None`, first executes `SELECT 1 FROM trading_decision_outcomes WHERE proposal_id=:p AND counterfactual_id IS NULL AND track_kind='accepted_live' AND horizon=:h` and raises `ValueError("duplicate accepted_live mark for this horizon")` if a row exists. Add a test for that guard.

Do not ship Path A without confirming the PG version, because `alembic upgrade head` will fail outright on PG ≤ 14.

---

**[FIX-5] Commit the work and open the PR.**

The branch currently has zero commits ahead of `main` (`git log main..HEAD` is empty); all changes are unstaged/untracked. After completing FIX-1 through FIX-4:

1. Stage only the intended files (avoid the directory-wide `git add .` pattern):
   ```
   git add app/models/__init__.py
   git add app/models/trading_decision.py
   git add app/services/trading_decision_service.py
   git add alembic/versions/ce5d470cc894_create_trading_decision_tables.py
   git add tests/test_trading_decision_models.py tests/test_trading_decision_service.py
   git add docs/plans/ROB-1-trading-decision-db-schema-plan.md
   git add docs/plans/ROB-1-review-report.md
   ```
2. Commit with a message that names the issue (`feat(trading-decision): ROB-1 DB schema, models, service, migration, tests`). Do **not** amend or force-push later commits without checking with the user.
3. Before opening the PR, run and paste output for: `uv run alembic upgrade head`, `uv run alembic downgrade -1`, `uv run alembic upgrade head`, `uv run pytest tests/test_trading_decision_models.py tests/test_trading_decision_service.py -q`, `uv run ruff check app/ tests/`, `uv run ty check`, against a freshly truncated `test_db`.

---

**[OPTIONAL R-1..R-3] Quality-of-life improvements (recommended, not blocking).**

- R-1: In `tests/test_trading_decision_models.py::test_proposal_check_constraints`, add two more sub-cases that violate the `side` CHECK and the `user_response` CHECK (each in its own rollback block).
- R-2: In `tests/test_trading_decision_service.py::test_record_user_response_does_not_mutate_original_fields`, snapshot **all** `original_*` columns and `original_payload` before the response, and assert equality field-by-field after.
- R-3: In `tests/test_trading_decision_service.py::test_create_session_with_btc_eth_sol_proposals`, add a third proposal for `KRW-SOL` so the test name matches the assertions.

---

## 8. Acceptance checklist (from plan §12) — current state

- [x] 5 tables created via single Alembic revision, head moves cleanly.
- [ ] `alembic downgrade -1 && alembic upgrade head` round-trips with no errors. *(unverified by reviewer; implementer should run and report)*
- [x] All CHECK / UNIQUE / FK constraints from §4 present in the migration. *(except the missing sessions.status index — see F1)*
- [x] SQLAlchemy models registered in `app/models/__init__.py`.
- [x] Service module exposes the six functions listed in §7.1, all `async`.
- [ ] No imports from §7.3 forbidden list in the new module **(test enforced)**. *(production module is clean; the test that enforces this is too weak — see FIX-1)*
- [ ] `record_user_response` provably does not mutate `original_*` *(test enforced — partial; see R-2)*.
- [ ] All tests in §8 pass. *(only 12 of 16 implemented; see FIX-2)*
- [x] `ruff check app/ tests/` clean. *(implementer report; not re-run)*
- [x] `docs/plans/ROB-1-trading-decision-db-schema-plan.md` is the only doc; no API/UI docs added.

**3 must-fix gaps before merge: FIX-1, FIX-2, FIX-3 (plus FIX-4 PG-version decision and FIX-5 commit).**
