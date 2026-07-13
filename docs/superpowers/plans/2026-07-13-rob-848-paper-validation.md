# ROB-848 Paper Validation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a server-derived role boundary and append-only paper-validation state machine that exact-binds ROB-846 experiment identity, exposes validation tools beside the unchanged six-tool paper broker façade, and never calls a live/proposal/broker mutation path.

**Architecture:** Three immutable `research` tables preserve transitions, hypothesis drafts, and postmortem reviews. Service mutations take a PostgreSQL transaction advisory lock per validation, re-read ordered history, verify injected frozen-input/policy stamps and ROB-846 hashes, then append one event; no mutable current-state row exists. The existing `PAPER_EXECUTION` registry branch composes an independent validation registrar with the ROB-845 registrar under the same gate.

**Tech Stack:** Python 3.13, Pydantic v2, SQLAlchemy 2 async, PostgreSQL 17, Alembic, FastMCP, pytest/pytest-asyncio/xdist, Ruff, ty.

## Global Constraints

- Use Linear ROB-848 plus the approved design at `docs/superpowers/specs/2026-07-13-rob-848-paper-validation-design.md` as source of truth.
- Preserve the exact graph `draft -> offline_eligible -> shadow_soak -> paper_active -> promotion_eligible -> promoted|rejected|aborted`.
- Reuse ROB-846 `research.strategy_experiments`, `StrategyExperimentIdentity`, and `app.services.research_canonical_hash`; do not copy or change their formula.
- Define/inject `FrozenInputHashProvider` and `PolicyHashProvider`; do not import ROB-838/839 concrete implementations.
- Do not implement ROB-849 cohort/snapshot storage or a concrete production `ExperimentProvenanceVerifier`.
- Do not import/call live order, broker-native submit, or ROB-816 proposal mutation code.
- No process-local concurrency lock, mutable current-state column, update/delete service, force push, amend, squash, or merge.
- Every production behavior starts with an observed focused RED and ends with focused GREEN before commit.

---

### Task 1: Closed contracts and deterministic state graph

**Files:**
- Create: `app/services/paper_validation/__init__.py`
- Create: `app/services/paper_validation/contracts.py`
- Create: `app/services/paper_validation/state_machine.py`
- Create: `tests/services/paper_validation/__init__.py`
- Create: `tests/services/paper_validation/test_contracts.py`
- Create: `tests/services/paper_validation/test_state_machine.py`

**Interfaces:**
- Produces: `ActorRole`, `ValidationState`, `ValidationIdentity`, `FrozenInputStamp`, `PolicyStamp`, `TransitionRequest`, `TransitionDecision`, `PaperOrderAuthorization`, `HypothesisDraftInput`, `PostmortemReviewInput`.
- Produces: `ActorRoleProvider.resolve(caller_id)`, `FrozenInputHashProvider.get_stamp(identity)`, `PolicyHashProvider.get_stamp(identity)` protocols.
- Produces: `next_state(prior, requested)` and `is_order_authorizable(state)`.

- [ ] **Step 1: Write closed-enum/schema tests.** Assert payloads with `actor_id` or `actor_role` fail Pydantic `extra="forbid"`, hashes require 64 lowercase hex characters, hypothesis fields are all required, and provider stamps must be `verified=True`.

```python
with pytest.raises(ValidationError):
    TransitionRequest(**payload, actor_role="operator")
assert ValidationIdentity(**identity).experiment_hash == identity["experiment_hash"]
```

- [ ] **Step 2: Write the complete graph matrix test.** Parametrize every state pair and assert only the four prerequisite edges plus three terminal edges are accepted; terminal requests return `terminal_state`, all other pairs return `invalid_transition`.
- [ ] **Step 3: Run RED.** Run `uv run pytest --no-cov -q tests/services/paper_validation/test_contracts.py tests/services/paper_validation/test_state_machine.py`; expect import failures for the new package.
- [ ] **Step 4: Implement frozen Pydantic/dataclass contracts and pure state functions.** Use `StrEnum`, `ConfigDict(frozen=True, extra="forbid")`, and no DB/broker imports.
- [ ] **Step 5: Run GREEN.** Re-run the focused command and `uv run pytest --no-cov -q tests/services/research/test_research_canonical_hash.py`.
- [ ] **Step 6: Commit.** `git add app/services/paper_validation tests/services/paper_validation && git commit -m "feat(ROB-848): define paper validation contracts"`.

### Task 2: Append-only ORM, migration, and schema bootstrap

**Files:**
- Create: `app/models/paper_validation.py`
- Modify: `app/models/__init__.py`
- Create: `alembic/versions/20260713_rob848_paper_validation.py`
- Modify: `tests/_schema_bootstrap.py`
- Create: `tests/services/paper_validation/test_models.py`
- Create: `tests/services/paper_validation/test_migration.py`

**Interfaces:**
- Produces: `PaperValidationStateTransition`, `StrategyHypothesisDraft`, `PaperValidationPostmortemReview`.
- Migration revision: `20260713_rob848_paper_validation`, `down_revision = "20260713_rob866_manual_alerts"`.
- DB invariants: experiment FK, experiment/strategy/config/policy hash trigger, immutable UPDATE/DELETE trigger, state/role/hash checks, transition sequence/idempotency uniqueness.

- [ ] **Step 1: Write ORM metadata tests.** Assert exact table/schema/column names, no `current_state`, update/delete methods, or cascade deletion, and all immutable audit fields are non-null where contracted.
- [ ] **Step 2: Write real-PostgreSQL RED tests.** Insert a ROB-846 experiment, then assert direct UPDATE/DELETE, invalid state/role/hash, duplicate sequence/key, missing experiment FK, and experiment hash mismatch are rejected.

```python
await session.execute(text(
    "UPDATE research.paper_validation_state_transitions "
    "SET reason_text='mutated' WHERE id=:id"
), {"id": event.id})
with pytest.raises(DBAPIError, match="append-only"):
    await session.commit()
```

- [ ] **Step 3: Run RED.** Run `uv run pytest --no-cov -q tests/services/paper_validation/test_models.py tests/services/paper_validation/test_migration.py`; expect missing model/table failures.
- [ ] **Step 4: Implement models and additive migration.** The transition insert trigger selects `research.strategy_experiments` and compares `experiment_id`, `strategy_hash`, `frozen_config_hash`, and `policy_hash`; all three audit tables share one mutation-rejection trigger function.
- [ ] **Step 5: Synchronize bootstrap.** Increment `SCHEMA_BOOTSTRAP_VERSION`, append idempotent table/constraint/trigger DDL mirroring the migration, and keep `apply_test_schema` model imports sufficient.
- [ ] **Step 6: Run GREEN.** Re-run focused tests plus `uv run pytest --no-cov -q tests/infra/test_schema_barrier.py tests/test_conftest_schema_patches.py`.
- [ ] **Step 7: Commit.** `git add app/models alembic/versions/20260713_rob848_paper_validation.py tests/_schema_bootstrap.py tests/services/paper_validation && git commit -m "feat(ROB-848): add immutable validation audit schema"`.

### Task 3: Transactional transition and idempotency service

**Files:**
- Create: `app/services/paper_validation/service.py`
- Create: `tests/services/paper_validation/conftest.py`
- Create: `tests/services/paper_validation/test_service_transitions.py`
- Create: `tests/services/paper_validation/test_service_concurrency.py`

**Interfaces:**
- Produces: `PaperValidationService.register`, `.transition`, `.get_audit`, `.authorize_order_submission`.
- Produces typed `PaperValidationError(reason_code)` subclasses or results with the stable reason vocabulary.
- Consumes injected actor/frozen-input/policy providers and an `AsyncSession` whose transaction is owned by the caller.

- [ ] **Step 1: Write role/provider/hash matrix RED tests.** Counter fakes prove missing/unmapped actor fails before both evidence providers; missing/failed providers yield `evidence_stamp_unavailable`; verified mismatch yields `evidence_hash_mismatch`; only operator/system register/transition/authorize.
- [ ] **Step 2: Write ordered state/history tests.** Register appends draft sequence 1, each legal edge appends exactly once, state derives from `ORDER BY sequence`, and skip/reversal/terminal requests append zero rows.
- [ ] **Step 3: Write sequential idempotency tests.** Same key and canonical payload returns the same row ID; same key with a changed reason/hash/target returns `idempotency_conflict` and leaves row count unchanged.
- [ ] **Step 4: Run RED.** Run `uv run pytest --no-cov -q tests/services/paper_validation/test_service_transitions.py`; expect missing service failures.
- [ ] **Step 5: Implement minimum service.** Resolve the server actor, call both evidence providers, exact-match stamps, acquire `pg_advisory_xact_lock(hashtextextended(:validation_id, 0))`, check existing idempotency, read latest event, validate the edge, and append with `canonical_sha256` request hash.
- [ ] **Step 6: Run GREEN.** Re-run transition tests.
- [ ] **Step 7: Write two-session concurrency RED tests.** Use two `AsyncSessionLocal` sessions and an `asyncio.Barrier`: identical requests return one row/same event; different target/payload requests have exactly one winner and loser `concurrent_transition_conflict`.
- [ ] **Step 8: Implement conflict normalization only after RED.** Catch named unique constraints only; never rollback or retry unrelated integrity errors.
- [ ] **Step 9: Run GREEN repeatedly.** `uv run pytest --no-cov -q -n 2 --count=1 tests/services/paper_validation/test_service_concurrency.py` if repeat plugin is absent, run the command in a shell loop three times.
- [ ] **Step 10: Commit.** `git add app/services/paper_validation tests/services/paper_validation && git commit -m "feat(ROB-848): enforce transactional validation transitions"`.

### Task 4: Immutable hypothesis/review audit and authorization boundary

**Files:**
- Modify: `app/services/paper_validation/service.py`
- Modify: `tests/services/paper_validation/test_service_transitions.py`
- Create: `tests/services/paper_validation/test_authorization_integration.py`

**Interfaces:**
- Produces: `append_hypothesis`, `append_postmortem_review`, `authorize_order_submission`, `confirm_promotion`, and `reject_or_abort` methods.
- `PaperOrderAuthorization` exact-binds validation, experiment, cohort, strategy, config, policy, input hashes and current allowed state; it performs no broker action.

- [ ] **Step 1: Write RED for narrative boundaries.** Researcher can append only the fixed hypothesis schema; reviewer can append only review text/evidence; neither payload accepts metrics, gate results, active strategy payload, actor, or role.
- [ ] **Step 2: Write RED for author/evaluator/operator audit.** Complete audit output keeps hypothesis author, review evaluator, and transition operator distinct and in deterministic created/sequence order.
- [ ] **Step 3: Write RED for order authorization and confirmation.** Authorization is allowed only in `paper_active|promotion_eligible`; promotion confirmation requires current `promotion_eligible` and exact current hashes. A fake verifier/adapter counter remains zero on role/state/hash failure.
- [ ] **Step 4: Run RED.** `uv run pytest --no-cov -q tests/services/paper_validation/test_authorization_integration.py`.
- [ ] **Step 5: Implement append/read/auth methods.** Copy trusted current hashes from the latest event rather than narrative payload; keep authorization as a frozen return value with no adapter dependency.
- [ ] **Step 6: Run GREEN and the full role matrix.** `uv run pytest --no-cov -q tests/services/paper_validation`.
- [ ] **Step 7: Commit.** `git add app/services/paper_validation tests/services/paper_validation && git commit -m "feat(ROB-848): add role-separated validation audit"`.

### Task 5: Independent MCP validation registrar and exact profile union

**Files:**
- Create: `app/mcp_server/tooling/paper_validation_registration.py`
- Modify: `app/mcp_server/tooling/registry.py`
- Modify: `app/core/config.py`
- Modify: `app/mcp_server/README.md`
- Modify: `.claude/settings.readonly.json`
- Create: `tests/mcp_server/tooling/test_paper_validation_registration.py`
- Modify: `tests/mcp_server/tooling/test_paper_execution_registration.py`
- Modify: `tests/test_mcp_tool_registration_boot.py`
- Modify: `tests/test_mcp_profiles.py`
- Modify: `tests/test_watch_triage_readonly_settings.py`

**Interfaces:**
- Produces: `PAPER_VALIDATION_TOOL_NAMES` and `register_paper_validation_tools(mcp, application_provider=...)`.
- Keeps `PAPER_EXECUTION_TOOL_NAMES` exactly six.
- Existing `McpProfile.PAPER_EXECUTION` allowlist becomes the exact union of the two sets under `PAPER_EXECUTION_ENABLED`.
- Production actor mapping comes from a typed default-empty setting and resolves `get_caller_agent_id()` server-side; empty/unmapped mapping fails closed.

- [ ] **Step 1: Write registrar RED tests.** Assert no handler signature contains actor ID/role, researcher/reviewer forbidden paths produce stable `forbidden`, default composition without role/hash providers fails closed, and fake app results are JSON safe.
- [ ] **Step 2: Write exact-union RED tests.** One assertion keeps the broker façade at six; another asserts the whole enabled profile equals `PAPER_EXECUTION_TOOL_NAMES | PAPER_VALIDATION_TOOL_NAMES`; disabled direct registration remains empty.
- [ ] **Step 3: Write readonly RED tests.** Classify every new mutation and require matching `mcp__auto_trader_local__*` deny entries; keep audit/history reads out of the deny list.
- [ ] **Step 4: Run RED.** Run `uv run pytest --no-cov -q tests/mcp_server/tooling/test_paper_validation_registration.py tests/mcp_server/tooling/test_paper_execution_registration.py tests/test_mcp_tool_registration_boot.py tests/test_mcp_profiles.py tests/test_watch_triage_readonly_settings.py`.
- [ ] **Step 5: Implement registrar and registry composition.** Import the two registrars only in `registry.py`; do not import validation from `paper_execution_registration.py`; add no profile enum or capability registry.
- [ ] **Step 6: Update documentation.** Document exact union, actor mapping fail-close, provider unavailability, role matrix, and rollback by disabling `PAPER_EXECUTION_ENABLED`.
- [ ] **Step 7: Run GREEN and startup/default-off regression.** Re-run Step 4 plus `uv run pytest --no-cov -q tests/test_mcp_server_main.py`.
- [ ] **Step 8: Commit.** `git add app/mcp_server app/core/config.py .claude/settings.readonly.json tests && git commit -m "feat(ROB-848): expose paper validation MCP boundary"`.

### Task 6: Safety guards and real migration lifecycle

**Files:**
- Create: `tests/services/paper_validation/test_safety_guards.py`
- Create: `tests/services/paper_validation/test_postgres_migration_lifecycle.py`
- Modify focused files only when a failing guard identifies a real gap.

**Interfaces:**
- Safety AST allowlist forbids imports/calls matching live order modules, `submit_order` broker-native calls, `order_proposal*` mutations, and any ROB-849 implementation type.
- Disposable PostgreSQL lifecycle upgrades from `20260713_rob866_manual_alerts` to ROB-848, downgrades one revision, then upgrades again with one head throughout.

- [ ] **Step 1: Write AST RED tests.** Parse every `app/services/paper_validation/*.py` and `paper_validation_registration.py`, walk `Import`, `ImportFrom`, and `Call`, and fail on forbidden live/proposal/broker mutation names.
- [ ] **Step 2: Run RED against any deliberately unclassified new surface, then implement the minimum guard-safe correction.** Run `uv run pytest --no-cov -q tests/services/paper_validation/test_safety_guards.py`.
- [ ] **Step 3: Create a disposable PostgreSQL database without reading `.env`.** Use the known local test server, generate a unique database name, and pass an explicit temporary `DATABASE_URL` only to Alembic commands.
- [ ] **Step 4: Verify upgrade/downgrade/upgrade.** Run `uv run alembic upgrade 20260713_rob866_manual_alerts`, `uv run alembic upgrade head`, `uv run alembic downgrade -1`, and `uv run alembic upgrade head`; inspect `alembic current` and table/trigger presence after each boundary, then drop the disposable database.
- [ ] **Step 5: Run actual two-session DB suite.** `uv run pytest --no-cov -q tests/services/paper_validation/test_migration.py tests/services/paper_validation/test_service_concurrency.py tests/services/paper_validation/test_postgres_migration_lifecycle.py`.
- [ ] **Step 6: Run ROB-846/MCP regressions and xdist repetition.** Run the canonical hash/registry suites and the full paper validation/profile set three times with `-n 2`.
- [ ] **Step 7: Commit.** `git add tests/services/paper_validation && git commit -m "test(ROB-848): verify migration and safety boundaries"`.

### Task 7: Verification, independent reviews, PR, and CI

**Files:**
- Modify only files required by review findings, always test-first.
- Update Linear ROB-848 comments; keep status In Progress.

- [ ] **Step 1: Run fresh local verification.** `uv run ruff check app tests`, `uv run ruff format --check app tests`, `uv run ty check app --error-on-warning`, relevant focused tests, full `make test`, xdist repetitions, `uv run alembic heads`, and `git diff --check origin/main..HEAD`.
- [ ] **Step 2: Dispatch four read-only reviewers in parallel.** Give separate agents the approved spec and `origin/main..HEAD` range for spec completeness, code quality, security/authorization, and PostgreSQL concurrency/idempotency.
- [ ] **Step 3: Resolve every P0/P1 and valid P2 with RED first.** Commit follow-ups separately; re-run the reviewer-specific regression and full verification.
- [ ] **Step 4: Fetch and integrate latest main without force/reset/amend/squash.** Use a normal merge commit only if main advanced and preserve all existing commits; re-run verification.
- [ ] **Step 5: Push `rob-848` and create a GitHub PR.** Include Linear link, state/role/concurrency design, migration evidence, MCP exact union, tests, rollback, and remaining ROB-849/850 risks. Do not merge.
- [ ] **Step 6: Watch GitHub CI and review feedback.** Inspect every required check to a terminal state; fix failures test-first, push normal follow-up commits, and repeat until green or an external blocker is proven.
- [ ] **Step 7: Record completion evidence in Linear while keeping In Progress.** Include RED→GREEN commands and actual pass counts, migration starting/final head, disposable lifecycle result, final SHA, PR URL, CI state, independent review outcomes, and remaining risks.
