# ROB-13 — TradingAgents Production Advisory-Only DB Smoke · Review Report

- **Reviewer:** Claude Opus (planner / reviewer)
- **Implementer:** Codex (`codex --yolo` exec)
- **Branch:** `feature/ROB-13-tradingagents-production-db-smoke` (3 commits ahead of `origin/main`)
- **Commits reviewed:**
  - `9fc0d8e7` test(rob-13): add unit tests for tradingagents smoke harness (failing)
  - `54d3116e` feat(rob-13): add advisory-only tradingagents production smoke harness
  - `1301d848` chore(rob-13): ruff format and minor fixups
- **Worktree:** `/Users/mgh3326/work/auto_trader-worktrees/feature-ROB-13-tradingagents-production-db-smoke`
- **Plan reviewed against:** `docs/plans/ROB-13-tradingagents-production-db-smoke-plan.md`
- **Verdict:** ✅ **Pre-smoke code review PASSED.** No blockers. One non-blocking nit (§5.1) and one administrative action (§5.2) noted. Production DB smoke (Tasks 5–9 of the plan) may proceed.

---

## 1. Verifications run by reviewer

| Check | Result |
|---|---|
| `git log --oneline origin/main..HEAD` | 3 commits, all `rob-13`-prefixed (above) |
| `git diff --stat origin/main..HEAD` | 3 files changed, +725 / −0. Files: `scripts/smoke_tradingagents_db_ingestion.py`, `tests/scripts/__init__.py`, `tests/scripts/test_smoke_tradingagents_db_ingestion.py` |
| `uv run pytest tests/scripts/ -v` | 7 passed (1.08s) |
| `uv run pytest tests/scripts/ tests/services/test_tradingagents_research_service.py tests/services/test_tradingagents_research_service_safety.py -q` | 25 passed (1.75s) |
| Source grep for forbidden imports (`from app.services.kis`, `upbit`, `brokers`, `order_service`, `watch_alerts`, `paper_trading_service`, `openclaw_client`, `crypto_trade_cooldown_service`, `fill_notification`, `execution_event`, `redis_token_manager`, `kis_websocket`, `app.tasks`) over the diff | EMPTY (only hit was the test-name string `test_argv_rejects_place_order_flag`, which references the `--place-order` argv-refusal constant, not an import) |
| Source grep for `place_order`, `manage_watch_alerts`, `dry_run=False` in the diff | None as imports/calls; only test names + argv-refusal string constants |
| Source grep for `os.environ` / `environ[` in the harness or its tests | EMPTY |
| Live import-time forbidden-module check (`uv run python -c "import scripts.smoke_…; …"`) | `forbidden_loaded: []` |
| Hermes-reported `uv run ruff check` + `ruff format --check` | PASS (Hermes summary in handoff) |

Note on the grep "match count of 1" earlier: it was the line
`def test_argv_rejects_place_order_flag()` — a *test function name* containing the
substring `place_order`. The harness/tests neither import nor call any
broker/order/watch surface.

---

## 2. Plan ↔ implementation diff (file-by-file)

| Plan section | Implementation file | Status |
|---|---|---|
| §5.1 harness module | `scripts/smoke_tradingagents_db_ingestion.py` (422 lines) | ✅ |
| §5.2 unit tests | `tests/scripts/test_smoke_tradingagents_db_ingestion.py` (302 lines) + `tests/scripts/__init__.py` | ✅ |
| §5.3 no-touch list (`app/services/tradingagents_research_service.py`, `app/schemas/tradingagents_research.py`, `app/core/config.py`, prod `.env`, every `app/services/*` execution surface) | None of these appear in `git diff --stat` | ✅ |

### 2.1 Harness shape (per plan §5.1)

| Plan requirement | Implementation evidence | Status |
|---|---|---|
| `_FORBIDDEN_PREFIXES` matches ROB-9 list verbatim | lines 20–34, exact 13 entries | ✅ |
| `_FORBIDDEN_ARGV` covers `--dry-run=False`, `--place-order`, `--register-watch`, `--order-intent`, `--no-advisory`, `--execute` | lines 36–43 | ✅ |
| `_refuse_forbidden_argv` called in `main()` BEFORE argparse | line 412 (before line 413 `_parse_args`) | ✅ |
| `_refuse_forbidden_modules` called at module-import time | line 418 (after every other import) | ✅ |
| `_refuse_forbidden_modules` also called inside `_run` after the service import | line 331 (immediately after `from app.services import tradingagents_research_service as svc`) | ✅ |
| Argparse: `--symbol`, `--as-of`, `--instrument-type {equity_kr,equity_us,crypto}`, `--user-id`, `--analysts`, mutually-exclusive `--keep-on-success`/`--delete-on-success` | lines 86–106 | ✅ |
| Symbol regex `^[A-Za-z0-9._/-]{1,32}$` | lines 46, 108 | ✅ |
| `--user-id` positivity check | lines 115–116 | ✅ |
| `--as-of` parsed via `date.fromisoformat` with error message | lines 110–113 | ✅ |
| `TRADINGAGENTS_PYTHON` missing → exit 78 with the documented message | lines 130–135 | ✅ |
| `TRADINGAGENTS_REPO_PATH` missing → exit 78 | lines 136–141 | ✅ |
| `tradingagents_runner_path` defaults to `<repo>/scripts/run_auto_trader_research.py` | lines 145–149 | ✅ |
| Existence checks for python/repo/runner before subprocess | lines 150–164 | ✅ |
| Single `AsyncSession` for ingest + invariant verification + commit | lines 342–376 | ✅ |
| Invariant assertions BEFORE commit | lines 355–365 (rollback + return 1 on failure) | ✅ |
| Side-effect counts via `text()` joined to proposals (actions / counterfactuals / outcomes) | lines 180–201 (parameterised `:session_id`; table names from a fixed tuple, not from user input) | ✅ |
| Proposal-count via ORM `func.count` | lines 167–177 | ✅ |
| Post-commit re-query in a fresh `AsyncSessionLocal` before printing the redacted JSON report | lines 393–406 | ✅ |
| JSON report keys: `ok`, `session{id, session_uuid, source_profile, market_scope, advisory_only, execution_allowed, generated_at}`, `proposal{id, symbol, instrument_type, proposal_kind, side, user_response, original_payload_advisory_only, original_payload_execution_allowed}`, `side_effect_counts{actions, counterfactuals, outcomes}` | `_build_report` at lines 291–323; matches plan shape (key order is alphabetised by `sort_keys=True` but the keys are exactly the documented set) | ✅ |
| Failure JSON shape `{"ok": false, "problems": [...]}` (no env values) | line 364 | ✅ |
| `logging.getLogger("smoke_tradingagents")` only | line 48; `basicConfig(level=INFO)` only at `main` | ✅ |
| `--keep-on-success` default; `--delete-on-success` deletes session row only (cascades to proposal via FK) | lines 95–106, 366–370; `db.delete(session_obj)` then `db.commit()` (proposal FK has `ondelete="CASCADE"`); user row untouched | ✅ |

### 2.2 Tests (per plan §5.2)

All seven tests from §5.2 are present and passing:

| Plan test | Implementation | Status |
|---|---|---|
| `test_argv_rejects_dry_run_false` | line 126; asserts `SystemExit(64)` | ✅ |
| `test_argv_rejects_place_order_flag` | line 135; asserts `SystemExit(64)` | ✅ |
| `test_argv_rejects_register_watch_flag` | line 144; asserts `SystemExit(64)` | ✅ |
| `test_module_import_does_not_load_forbidden_prefixes` | line 153; subprocess clean-import check matching ROB-9 safety pattern; asserts `violations == []` | ✅ |
| `test_settings_missing_tradingagents_python_exits_78` | line 187; asserts `SystemExit(78)` | ✅ |
| `test_invariant_violation_rolls_back` | line 209; stub returns session with `execution_allowed=True`; asserts `db.rollback` was awaited and `db.commit` was NOT awaited; exit code 1 | ✅ |
| `test_success_path_prints_redacted_json_report` | line 245; asserts exit 0, commit awaited, JSON report contains expected ids/counts, and **no** secret markers (`OPENAI_API_KEY`, `KIS_`, `UPBIT_`, `GOOGLE_API_KEY`, `DATABASE_URL`, `TELEGRAM_TOKEN`, `OPENDART_API_KEY`) appear in stdout | ✅ |

The test file imports only `app.models.trading{,_decision}` (declarative models;
no service surface), `pytest`, stdlib, and `unittest.mock`. No forbidden
service imports.

---

## 3. Safety boundary review

| Constraint (handoff + plan §3) | Verdict | Evidence |
|---|---|---|
| TradingAgents must remain `advisory_only=true` and `execution_allowed=false` | ✅ | The harness validates BOTH the session-level `market_brief.advisory_only/execution_allowed` AND the proposal-level `original_payload.advisory_only/execution_allowed`. On any deviation it rolls back and exits 1. ROB-9's Pydantic Literal pins enforce this earlier in the call stack. |
| No live orders | ✅ | `place_order` is not imported or called. Test name `test_argv_rejects_place_order_flag` is a string constant used only to assert the harness *refuses* such argv. |
| No `dry_run=False` | ✅ | `dry_run` is not referenced anywhere in the harness. The harness's `_FORBIDDEN_ARGV` explicitly refuses `--dry-run=False` argv tokens with `SystemExit(64)`. |
| No watch registration | ✅ | `manage_watch_alerts` not imported. `app.services.watch_alerts` in `_FORBIDDEN_PREFIXES`; refuses if loaded into `sys.modules`. |
| No order-intent creation | ✅ | `_FORBIDDEN_ARGV` includes `--order-intent`. No `TradingDecisionAction`/`Counterfactual`/`Outcome` row constructors are referenced in the harness; the invariant block actively asserts those tables have **zero** rows for the new session. |
| No broker side-effect imports/calls | ✅ | `_FORBIDDEN_PREFIXES` covers `kis*`, `upbit*`, `brokers*`, `order_service`, `paper_trading_service`, `openclaw_client`, `crypto_trade_cooldown_service`, `fill_notification`, `execution_event`, `redis_token_manager`, `kis_websocket*`, `app.tasks`. Live import-time check (`forbidden_loaded: []`) verifies importing the harness loads zero of these. |
| No secret printing | ✅ | No `os.environ` access anywhere in the harness or its tests. The JSON report builder pulls only `id`/`uuid`/`scope`/flags/`generated_at` (an ISO timestamp) — no API keys, no DATABASE_URL, no env values. The success-path test asserts none of `OPENAI_API_KEY`, `KIS_`, `UPBIT_`, `GOOGLE_API_KEY`, `DATABASE_URL`, `TELEGRAM_TOKEN`, `OPENDART_API_KEY` appear in stdout. |
| No `subprocess.Popen`/`shell=True` shortcuts | ✅ | Subprocess invocation is delegated to ROB-9's already-reviewed service. The harness itself spawns no subprocess (the safety-test in §5.2 uses `subprocess.run` with a fixed argv list, not user input). |
| Caller owns DB transaction | ✅ | Harness performs its own `db.commit()` ONLY after all 11 invariants pass. On any failure: `db.rollback()` and exit 1. ROB-9's service uses only `flush()`, so the transaction boundary stays in the harness. |
| `--delete-on-success` cleanup is bounded | ✅ | Deletes only the just-created `TradingDecisionSession` (cascade drops the single proposal). Does not delete the user. Default is `--keep-on-success`. |
| No production env file modified | ✅ | `git diff --stat` does not touch `~/services/auto_trader/shared/.env.prod.native` or any `.env*`. |
| No `app/` modification | ✅ | `git diff --stat` only touches `scripts/` and `tests/scripts/`. |

**No safety-boundary violations identified.**

---

## 4. Linear acceptance criteria cross-check

| Acceptance criterion | Will be checked at smoke time? | How |
|---|---|---|
| `ingest_tradingagents_research()` succeeds in deployed runtime | Yes | Harness calls it; `try/except` raises any failure to exit 1 |
| `source_profile == "tradingagents"` | Yes | Invariant 1 in `_validate_invariants` |
| `market_scope == "kr"` (KR smoke) | Yes | Invariant 2 (only when `instrument_type=equity_kr`) |
| `market_brief.advisory_only is True` | Yes | Invariant 3 |
| `market_brief.execution_allowed is False` | Yes | Invariant 4 |
| Exactly one proposal | Yes | `_count_proposals` + invariant 5 |
| `proposal_kind == "other"` | Yes | Invariant 6 |
| `side == "none"` | Yes | Invariant 7 |
| `payload.advisory_only is True` | Yes | Invariant 8 |
| `payload.execution_allowed is False` | Yes | Invariant 9 |
| `user_response == pending` | Yes | Invariant 10 + `user_*` field None checks (lines 273–284) |
| Zero `TradingDecisionAction` / `Counterfactual` / `Outcome` rows | Yes | `_count_side_effects` + side-effect-count invariants (line 285–287) |
| No live order, no dry-run, no watch, no order-intent | Yes | argv refusals + forbidden-module check + no broker imports |
| Linear / Discord smoke report | Planner posts after smoke succeeds | Plan §6 + §8 task 9 |

All acceptance gates have a code path that enforces them or a planner step
that reports them.

---

## 5. Non-blocking observations

These are **not must-fix** for code-review pass. The first is a small code-hygiene
note; the second is a procedural reminder before opening the PR.

### 5.1 Dead helper `_redact_env_value` (lines 54–57)

The harness defines `_redact_env_value(key, value)` that masks values whose key
matches `_SECRET_KEY_RE` (`(KEY|SECRET|TOKEN|PASSWORD|URL)$`, case-insensitive),
but it is never called anywhere in the harness because the harness never
emits env values to logs/stdout (which is the correct behavior). The helper is
defensive vestigial code from the plan's logging-policy clause.

Recommendation: leave it in. It costs nothing, it documents the intended
masking rule for any future maintainer who adds env logging, and removing it
would require another commit. Optional follow-up: the next harness change
should either delete it or call it from a new logging path.

### 5.2 Plan doc is untracked

`docs/plans/ROB-13-tradingagents-production-db-smoke-plan.md` and
`docs/plans/ROB-13-review-report.md` (this file) are not yet committed. ROB-9
landed both `…-plan.md` and `…-review-report.md` in the merged PR (see
`docs/plans/ROB-9-…-plan.md` + `docs/plans/ROB-9-review-report.md` on `main`).

Recommendation: include both of these markdown files in the PR before pushing
(planner will create the smoke evidence commit at plan Task 8 anyway —
`docs(rob-13): production advisory-only smoke evidence` — and can include the
plan + review report in the same or an adjacent `docs(rob-13): plan + review`
commit). This keeps the audit trail visible on `main` without expanding code
scope.

### 5.3 Reminder — planner-only steps remaining

Per the plan, the implementer's mandate ended at Task 4. The following remain
for the planner (this reviewer) to execute personally:

- Task 1.1 release-SHA parity check (already verified pre-implementation; will
  re-verify immediately before smoke)
- Task 5 — pick smoke `--user-id` from `manage_users.py list`
- Task 6 — run the smoke
- Task 7 — SQL re-verification
- Task 8 — write `docs/plans/ROB-13-smoke-report.md`
- Task 9 — push, open PR, post Linear/Discord

None of these require code changes; they are operational.

---

## 6. PR-readiness checklist (plan §9 cross-check, code-only items)

- [x] `git diff origin/main` touches only `scripts/smoke_tradingagents_db_ingestion.py`, `tests/scripts/__init__.py`, `tests/scripts/test_smoke_tradingagents_db_ingestion.py` (plan + review markdown will be added by planner before PR; see §5.2)
- [x] No `app/services/kis*`, `upbit*`, `brokers*`, `order_service`, `watch_alerts`, `paper_trading_service`, `openclaw_client`, `crypto_trade_cooldown_service`, `fill_notification`, `execution_event`, `redis_token_manager`, `kis_websocket*`, `app.tasks` import in the diff
- [x] No `place_order`, `manage_watch_alerts`, `dry_run=False`, broker construction, watch registration, order-intent call
- [x] `_FORBIDDEN_PREFIXES` matches ROB-9 list verbatim
- [x] No `os.environ` access anywhere in the diff
- [x] No write to `~/services/auto_trader/shared/.env.prod.native` and no edit of any `.env*`
- [x] `_FORBIDDEN_ARGV` and `_FORBIDDEN_PREFIXES` enforced both at import time and inside `_run`
- [x] Invariant block runs BEFORE `db.commit()`; failure path rolls back and exits 1
- [x] All 7 plan §5.2 unit tests pass; full ROB-9+ROB-13 sweep is 25/25 green
- [x] `ruff check` + `ruff format --check` clean (Hermes-confirmed)
- [ ] PR description must explicitly include "advisory-only, no execution path" — to be enforced by planner at PR-create time
- [ ] Smoke report (`docs/plans/ROB-13-smoke-report.md`) — to be added by planner after smoke passes

---

## 7. Conclusion

The harness faithfully implements the ROB-13 plan with no scope drift, no
execution-path leakage, no secret-handling regressions, and complete coverage
of all 14 safety invariants both at code-time (forbidden imports / forbidden
argv / module-import boundary check) and at run-time (in-DB invariant
re-verification + side-effect-row counts). Tests prove the rollback path on
invariant violation and the redaction expectation on the success path.

**Recommendation:** proceed to plan §6 (production DB smoke) under the
planner's direct execution. After the smoke passes, planner adds the smoke
evidence + plan + this review-report to the PR, opens the PR with `main` as
the base and "advisory-only, no execution path" in the description, and posts
the Linear/Discord update.

```
AOE_STATUS: review_passed
AOE_ISSUE: ROB-13
AOE_ROLE: reviewer
AOE_REPORT_PATH: docs/plans/ROB-13-review-report.md
AOE_NEXT: run_production_db_smoke
```
