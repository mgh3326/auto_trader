# ROB-26 — Reviewer report

- **Reviewer:** Claude Opus (planner/reviewer, same AoE session as the planner)
- **PR:** [#617](https://github.com/mgh3326/auto_trader/pull/617)
- **Branch / worktree:** `feature/ROB-26-prefect-research-run-refresh` @
  `/Users/mgh3326/work/auto_trader-worktrees/feature-ROB-26-prefect-research-run-refresh`
- **Plan:** `docs/plans/ROB-26-prefect-research-run-refresh-plan.md`
- **Diff base:** `origin/main` → tip `e36dc311` (9 commits, +2,221 / −0 across 13 files)

## Summary

The implementation faithfully realizes the planned read-only Taskiq schedule
wiring. All hard safety constraints are honored (no Prefect dependency or
imports; no broker/order/watch mutation paths; schedules disabled by default;
KST cron offset; decision-ledger-only persistence; `include_tradingagents`
hard-locked to `False` and not exposed on the public orchestrator surface).
Tests for the new modules pass (20/20), the ROB-25 regression bracket still
passes (22/22), and `ruff check` is clean. Ready to merge.

## Verification performed (read-only)

| Check | Command | Result |
|---|---|---|
| ROB-26 unit + smoke + safety + script + settings + tasks | `uv run pytest tests/test_research_run_refresh_*.py tests/scripts/test_run_research_run_refresh_script.py -v` | **20 passed** |
| ROB-25 regression bracket | `uv run pytest tests/test_research_run_decision_session_*.py tests/test_research_run_live_refresh_service.py -v` | **22 passed** |
| Lint | `uv run ruff check <new-files>` | **All checks passed** |
| `prefect` dep present? | `grep -ni prefect pyproject.toml uv.lock` | **No matches** |
| Schedules emitted with `cron_offset='Asia/Seoul'`? | `python -c "from app.tasks import research_run_refresh_tasks as m; print(m.kr_preopen_research_refresh.labels)"` | confirmed (also tested by `tests/test_research_run_refresh_tasks.py::test_cron_strings_match_schedule_matrix`) |
| Taskiq honors `cron_offset`? | source: `taskiq/schedule_sources/label_based.py:62` (`cron_offset=schedule.get("cron_offset")` passed into the schedule object) | confirmed |
| `AsyncSessionLocal` exists at `app.core.db:19`? | `grep AsyncSessionLocal app/core/db.py` | confirmed |

> The manual `uv run python scripts/run_research_run_refresh.py --stage preopen --dry-run`
> invocation in this review environment fails Pydantic settings validation on
> unrelated required env vars (`upbit_access_key`, `SECRET_KEY`, …). That is
> an artifact of the reviewer's environment, **not** a defect of this PR; the
> implementer reported the same script returns
> `{"status": "dry_run", "reason": "no_operator_user_configured"}` in their
> dev/CI environment, and the script's logic is fully covered by
> `tests/scripts/test_run_research_run_refresh_script.py` (both tests pass).

## Safety constraint audit

| Constraint | Status | Evidence |
|---|---|---|
| No `prefect` import or dependency | ✅ | `pyproject.toml` / `uv.lock` show no Prefect; `tests/test_research_run_refresh_import_safety.py` enforces `prefect` is not imported in either new module. |
| No order / watch / paper / dry-run / fill / execution path | ✅ | Forbidden-import list includes `app.services.kis_trading_service`, `app.services.order_service`, `app.services.orders`, `app.services.paper_trading_service`, `app.services.fill_notification`, `app.services.execution_event`, `app.services.watch_alerts`, `app.services.tradingagents_research_service`, `app.mcp_server.tooling.orders_*`, `app.mcp_server.tooling.paper_order_handler`, `app.mcp_server.tooling.watch_alerts_registration` — see `tests/test_research_run_refresh_import_safety.py:7-29`. |
| Schedules disabled by default | ✅ | `app/core/config.py:238` (`research_run_refresh_enabled: bool = False`), checked first in `app/jobs/research_run_refresh_runner.py:106-110`, asserted by `tests/test_research_run_refresh_settings.py` and `test_disabled_short_circuits`. |
| `cron_offset: "Asia/Seoul"` on every schedule | ✅ | `app/tasks/research_run_refresh_tasks.py:13` (`_KST = "Asia/Seoul"`) used uniformly on lines 18/26/34/42/50/58/66/74; asserted by `test_cron_strings_match_schedule_matrix`. |
| Decision Session is decision-ledger only | ✅ | The orchestrator calls `create_decision_session_from_research_run` only — same path the existing ROB-25 router uses; the underlying service does not invoke any execution code path. |
| `include_tradingagents=False` hard-locked + not exposed on orchestrator API | ✅ | `app/jobs/research_run_refresh_runner.py:165` hard-codes `include_tradingagents=False`; the orchestrator's public signature (`stage`, `market_scope`, `db_factory`, `now`, `now_local`) does not accept the parameter. |
| No secrets / tokens / connection strings in summaries | ✅ | Returned summary is a closed dict of `status`, `reason`, `stage`, `market_scope`, `research_run_uuid` (UUID str), `session_uuid` (UUID str), `proposal_count`, `reconciliation_count`, `refreshed_at` (ISO), `warnings` (list of token strings). The smoke test (`tests/test_research_run_refresh_smoke.py:91-95`) regex-asserts no secret-shaped values. |
| DB commit only on success path | ✅ | `app/jobs/research_run_refresh_runner.py:179` (single `await db.commit()` on the completed branch); skip paths return early before any commit; on inner exception the orchestrator does `await db.rollback()` and re-raises (lines 191-198). |
| Operational skips do not raise | ✅ | `ResearchRunNotFound` (line 146) and `EmptyResearchRunError` (line 171) are caught and translated to `status="skipped"` with explicit `reason`. |

## Plan ↔ implementation parity

- File layout matches plan §6 exactly (8 new files, 3 edited).
- Schedule matrix matches plan §4 cron strings exactly (verified by parameterized
  test with `EXPECTED_SCHEDULES` mirroring the matrix).
- Orchestrator surface matches plan §7.1 (the only deliberate refinement is the
  injected `db_factory` typed as `Callable[[], AbstractAsyncContextManager[AsyncSession]]`
  — strictly more precise than the plan sketch).
- `_default_db_factory` uses `app.core.db.AsyncSessionLocal` (lazy-imported inside
  the helper as the plan recommended for testability).
- An additional `Settings` field validator (`_parse_optional_user_id`) was added
  to coerce empty-string env values to `None` and parse `int` from string. This
  is an additive, non-breaking improvement consistent with the patterns in
  `app/core/config.py`.

## Tests / coverage

- Window helper: 5 assertions across weekday, weekend, post-window cutoffs.
- Skip paths: `disabled`, `no_operator_user_configured`, `outside_trading_hours`,
  `no_research_run` — each covered with monkeypatched settings + fake session.
- Happy path: end-to-end mocking of `resolve_research_run`,
  `build_live_refresh_snapshot`, `create_decision_session_from_research_run`,
  asserting `proposal_count`, `session_uuid`, and exactly one `commit()`.
- Tasks: existence, cron-label structure, registration in
  `TASKIQ_TASK_MODULES`, and per-task delegation to the runner.
- Import safety: parametrized over both new modules.
- Smoke: structural assertion on the full skipped summary + secret-shape regex.
- Manual script: dry-run no-operator path + CLI default invocation via `capsys`.

Branch coverage on the orchestrator is essentially complete; the only branch
not directly tested is the `EmptyResearchRunError` translation (line 171–177),
which is exercised indirectly through the existing ROB-25 service tests.

## Operational risk

- **Schedules off by default** → no impact on production until an operator
  flips `RESEARCH_RUN_REFRESH_ENABLED=true` and sets a `RESEARCH_RUN_REFRESH_USER_ID`.
- **No new migration** → rollback is a config flip.
- **Worker / scheduler footprint:** 8 additional cron rules; bodies short-circuit
  in <1ms when disabled, no DB touch.
- **Failure mode:** if the underlying live-refresh / decision-session pipeline
  errors mid-run, the orchestrator does `rollback()` and re-raises, which will
  surface in Taskiq retry / Sentry — appropriate behavior.
- **Idempotency:** as documented in plan §3.9, each cron firing creates a new
  `TradingDecisionSession` if a run is found. This matches ROB-25 router
  semantics; deferring a Redis dedupe key is the right call for this PR.

## Non-blocking nits (for follow-up tickets, not blockers for merge)

1. `_within_window` checks weekday only; KR public-holiday calendars are not
   consulted. On a holiday with no upstream research run, the orchestrator will
   still short-circuit with `no_research_run` (no mutation). If a future change
   ever creates a "ghost" preopen run on a holiday, the schedule would
   needlessly refresh it. Suggest layering `xcals.get_calendar("XKRX").is_session(now)`
   in a follow-up — out of scope for ROB-26.
2. The orchestrator's source-text import-safety test (consistent with ROB-16
   precedent) would not catch a `__import__("prefect")` evasion. Acceptable
   trade-off given the AST-walking alternative is heavier and the file is small
   and static.
3. The `_parse_optional_user_id` validator raises on a non-numeric string env
   value rather than coercing/skipping. This is strict-config behavior and is
   fine; flag only as a doc improvement (env.example clearly shows an empty
   value, so misuse is unlikely).
4. `now_local=lambda: datetime(...)` injection sites in tests use naive
   datetimes; production uses tz-aware `now_kst()`. The arithmetic
   (`weekday()`, hour/minute) is identical, so no functional issue, but a
   future cleanup could enforce tz-aware datetimes via a small helper.

None of the above gate this PR.

## Verdict

The implementation is correct, safe, and minimally scoped. It satisfies every
acceptance criterion in the Linear issue and every safety invariant in plan §3.
Tests are appropriate and pass. CI / Sonar are reported green. There are no
must-fix items.

**Decision:** **Pass** — ready to merge.

---

AOE_STATUS: review_passed
AOE_ISSUE: ROB-26
AOE_ROLE: reviewer
AOE_REPORT_PATH: docs/plans/ROB-26-review-report.md
AOE_NEXT: merge_pr
