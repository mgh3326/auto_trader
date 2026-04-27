# ROB-9 — TradingAgents Advisory Integration · Review Report

- **Reviewer:** Claude Opus (planner/reviewer)
- **Implementer:** Codex (Hermes auto mode)
- **Branch:** `feature/ROB-9-tradingagents-advisory-integration` (1 commit ahead of `origin/main`)
- **Commit:** `3ae981d1 feat: ingest TradingAgents advisory research`
- **Worktree:** `/Users/mgh3326/work/auto_trader-worktrees/feature-ROB-9-tradingagents-advisory-integration`
- **Plan reviewed against:** `docs/plans/ROB-9-tradingagents-auto-trader-integration-plan.md`
- **Verdict:** ✅ **PR-ready** — no blockers; non-blocking observations in §5.

---

## 1. Verifications run

| Check | Result |
|---|---|
| `git log --oneline origin/main..HEAD` | 1 commit (`3ae981d1`) |
| `git show --stat HEAD` | 10 files, +1796 lines (service, schema, config delta, plan, fixtures, three test files, CLAUDE.md note) |
| Hermes-reported `uv run pytest tests/services/test_tradingagents_research_service{,_safety}.py -q` | 18 passed |
| Hermes-reported `uv run pytest tests/services/test_tradingagents_research_service{,_safety,_integration}.py -q` | 18 passed, 6 skipped (DB-gated integration suite) |
| Hermes-reported `uv run ruff check / ruff format --check` on touched files | PASS |
| Source grep for forbidden surfaces (`place_order`, `manage_watch_alerts`, `order_service`, `watch_alerts`, `paper_trading`, `brokers`, `kis.`, `upbit.`, `redis`, `paperclip`) over `app/services/tradingagents_research_service.py` + `app/schemas/tradingagents_research.py` | EMPTY |
| Source grep for `TradingDecisionAction`/`TradingDecisionCounterfactual`/`TradingDecisionOutcome` in service module | EMPTY (service never constructs execution-track rows) |
| Source grep for `shell=`, `os.system`, `subprocess.Popen` in service module | EMPTY (only `asyncio.create_subprocess_exec` is used) |
| `CLAUDE.md` env-vars section | Carries the new `TRADINGAGENTS_*` block pointing to this plan, no secret values |

---

## 2. Plan ↔ implementation diff (file-by-file)

| Plan section | Implementation file | Status |
|---|---|---|
| §4.1 Schema | `app/schemas/tradingagents_research.py` (49 lines) | ✅ Mirrors §6 exactly. `Literal["ok"]`, `Literal[True]`, `Literal[False]` are pinned on `status`/`advisory_only`/`execution_allowed`. `warnings` uses `extra="allow"` for forward-compat; rest use `extra="ignore"`. |
| §4.1 Service | `app/services/tradingagents_research_service.py` (330 lines) | ✅ Public surface (`run_tradingagents_research`, `ingest_tradingagents_research`, three exception classes) matches §7.1. |
| §4.2 Settings | `app/core/config.py` (+15 lines) | ✅ All 13 `tradingagents_*` settings with the documented defaults; no other settings touched. |
| §4.3 No-touch list | `app/services/__init__.py`, `app/models/trading_decision.py`, `app/routers/trading_decisions.py`, KIS/Upbit/brokers/order_service/watch_alerts/paper_trading | ✅ None of these files appear in `git show --stat`. |
| §6 Pydantic contract | `tests/fixtures/tradingagents/runner_ok_nvda.json`, `runner_invariant_violation.json` | ✅ Both fixtures exercise the schema, including the `execution_allowed: true` rejection path. |
| §7.2 argv hardening | `_validate_symbol`, `_analysts_or_default`, `_build_argv` | ✅ Symbol regex `^[A-Za-z0-9._/-]{1,32}$`, analyst regex `^[a-z_]{1,32}$`. argv is built as a `list[str]` with no f-string interpolation. `cwd` set to repo path. |
| §7.3 timeout/cleanup | `run_tradingagents_research` lines 232–240 | ✅ `asyncio.wait_for` → on `TimeoutError`: `proc.kill()` + `await proc.wait()` + `TradingAgentsRunnerError("tradingagents runner timed out")`. |
| §7.4 JSON parse + validation | lines 250–255, `_validate_runner_payload` | ✅ Catches `UnicodeDecodeError`, `json.JSONDecodeError`. Routes `status`/`advisory_only`/`execution_allowed` Pydantic failures to `AdvisoryInvariantViolation` (a subclass of `TradingAgentsRunnerError`); other validation errors raise the parent class. |
| §7.5 DB mapping | `ingest_tradingagents_research` lines 285–330 | ✅ One `TradingDecisionSession` (`source_profile="tradingagents"`, `market_brief` carries advisory invariants) + one `TradingDecisionProposal` (`proposal_kind=other`, `side="none"`, all numeric `original_*` fields `None`, `original_payload` carries advisory invariants + decision text). No actions/counterfactuals/outcomes created. |
| §7.6 Memory log | `_write_memory_log` | ✅ Guarded by setting; `is_relative_to(base)` traversal check; OSError → WARNING (does not abort). |
| §8.1 unit tests | `tests/services/test_tradingagents_research_service.py` | ✅ Covers all 11 cases listed in plan plus two memory-log tests and a parametrized invariant-violation test. |
| §8.2 integration tests | `tests/services/test_tradingagents_research_service_integration.py` | ✅ Covers all 6 cases listed in plan; uses `_create_user`/`_cleanup_user` pattern from ROB-1 tests; gates on `to_regclass(...)` so suite skips cleanly without DB. |
| §8.3 forbidden-import safety | `tests/services/test_tradingagents_research_service_safety.py` | ✅ Subprocess-import test extends the ROB-1 forbidden list with `watch_alerts`, `paper_trading_service`, `openclaw_client`, `crypto_trade_cooldown_service`. |

No plan section is missing an implementation. No implementation files exist outside the plan's allow-list.

---

## 3. Safety boundary review

| Constraint (from plan §9 + handoff) | Verdict | Evidence |
|---|---|---|
| Service module does **not** import any KIS / Upbit / brokers / order_service / watch_alerts / paper_trading / openclaw / crypto_trade_cooldown / fill_notification / execution_event / redis_token_manager / kis_websocket / app.tasks module | ✅ | Source grep empty; safety test (§8.3) enforces this in CI via subprocess clean-import. |
| No `place_order`, `manage_watch_alerts`, broker construction, Paperclip write, Redis write | ✅ | Source grep empty. Service touches only `app.services.trading_decision_service` (which itself was already proven boundary-safe in ROB-1). |
| No `TradingDecisionAction` / `TradingDecisionCounterfactual` / `TradingDecisionOutcome` row creation | ✅ | Source grep empty in the service module. Integration test `test_ingest_does_not_create_action_or_counterfactual_or_outcome` asserts post-ingest counts of all three child tables are 0. |
| `subprocess` invocation uses explicit argv list, no shell, no string interpolation | ✅ | Only `asyncio.create_subprocess_exec(*argv, ...)` is used; no `shell=True` / `Popen` / `os.system` anywhere in the service. argv is built from typed settings + regex-validated caller args. |
| Symbol / analysts / date validation before subprocess | ✅ | `_validate_symbol` rejects shell metachars (`AAPL; rm -rf /` blocked, subprocess never called — proven by `test_symbol_argv_validation_rejects_shell_metachars`). `_ANALYST_RE` rejects `market;bad`. `as_of_date` is typed `date` so spoofing via string is impossible. |
| Env filtering: only `PATH`, `HOME`, `LANG`, `LC_ALL`, `PYTHONPATH`, `TRADINGAGENTS_*`, `OPENAI_API_KEY` forwarded | ✅ | `_filtered_child_env` uses an allow-list. `test_filtered_env_does_not_leak_unrelated_vars` verifies an unrelated `ROB9_UNRELATED_SECRET` does **not** appear in the child env. |
| Timeout → kill+wait → no DB write | ✅ | `proc.kill()` + `await proc.wait()` on `TimeoutError`; `TradingAgentsRunnerError` raised before any DB call. `test_runner_timeout_kills_and_raises` asserts `kill_called` and `wait.assert_awaited_once()`. |
| Non-zero exit / non-JSON stdout → no DB write | ✅ | Both raise `TradingAgentsRunnerError` before `ingest_tradingagents_research` reaches DB calls. Tested. Integration test `test_ingest_runner_failure_rolls_back` proves end-to-end DB rollback. |
| `advisory_only=False` / `execution_allowed=True` / `status="error"` → rejected pre-persist | ✅ | Pydantic `Literal` pins enforce this at validation. `_validate_runner_payload` re-wraps as `AdvisoryInvariantViolation`. Parametrized test covers all three deviations. |
| `warnings.structured_output` preserved | ✅ | Asserted in unit test (`test_warnings_structured_output_preserved`) and integration test (`test_ingest_preserves_warnings_structured_output`). Stored in both `session.market_brief["warnings"]` and `proposal.original_payload["warnings"]`. |
| `advisory_only`/`execution_allowed` flags persisted at session AND proposal level | ✅ | Confirmed in `test_ingest_persists_advisory_invariants_in_market_brief_and_payload`. |
| No `os.environ` value echoed in logs / exceptions / persisted JSON | ✅ | `_redact_stderr` filters lines matching `(key|token|secret|authorization)` (case-insensitive) and truncates to 4 KiB before a single `logger.debug` call. Exception messages contain only Pydantic error structures + return codes — never raw stdout. `original_payload` / `market_brief` only ingest validated Pydantic-model dumps. |
| Caller owns DB commit | ✅ | Service uses only `session.flush()` (transitively via `trading_decision_service`). No `await db.commit()` in `tradingagents_research_service.py`. Integration tests confirm rollback works. |

**No safety-boundary violations identified.**

---

## 4. Test-coverage review

Unit (`test_tradingagents_research_service.py` — 13 cases):

- ✅ `test_settings_parse_tradingagents_env_values`
- ✅ `test_schema_accepts_ok_payload_and_rejects_invariant_violation`
- ✅ `test_runner_ok_returns_validated_result` (also asserts argv shape and PIPE wiring)
- ✅ `test_runner_nonzero_exit_raises`
- ✅ `test_runner_timeout_kills_and_raises`
- ✅ `test_runner_non_json_stdout_raises`
- ✅ `test_runner_advisory_invariant_violations_are_rejected` (parametrized over `status="error"`, `advisory_only=False`, `execution_allowed=True`)
- ✅ `test_warnings_structured_output_preserved`
- ✅ `test_symbol_argv_validation_rejects_shell_metachars` + `test_analyst_argv_validation_rejects_shell_metachars`
- ✅ `test_settings_missing_repo_path_raises`
- ✅ `test_filtered_env_does_not_leak_unrelated_vars` + `test_default_openai_api_key_injected_when_missing`
- ✅ `test_memory_log_disabled_writes_no_file` + `test_memory_log_enabled_writes_validated_payload_under_configured_path`

Integration (`test_tradingagents_research_service_integration.py` — 6 cases, all `@pytest.mark.integration`):

- ✅ session+single proposal shape
- ✅ advisory invariants in `market_brief` AND `original_payload`
- ✅ warnings preserved end-to-end
- ✅ no actions/counterfactuals/outcomes
- ✅ runner failure rollback
- ✅ user-response fields untouched

Safety (`test_tradingagents_research_service_safety.py` — 1 subprocess test):

- ✅ Forbidden-prefix list extends ROB-1's set with `watch_alerts`, `paper_trading_service`, `openclaw_client`, `crypto_trade_cooldown_service`. Test imports the new service in a clean Python and asserts no forbidden module loaded as a transitive consequence.

Hermes-reported counts (18 + 18+6 skipped) match what the source files produce. CI on `main`-style runs will pass the unit + safety suites without DB; the 6 integration tests will skip cleanly via `_ensure_trading_decision_tables`.

---

## 5. Non-blocking observations

These are **not must-fix** for this PR but are worth noting for follow-ups.

1. **`OPENAI_API_KEY="no-key-required"` placeholder injection** — `_filtered_child_env` (line 121) sets a fallback `OPENAI_API_KEY` when none is present. The plan §5/§9 documented forwarding of `OPENAI_API_KEY` if present, but did not specify a placeholder default. The implementer chose this so the OpenAI SDK inside TradingAgents does not crash when pointed at a no-auth local shim. The placeholder is a literal string, not a real secret, so this does not violate the no-secret-leak constraint. Consider: making this either (a) opt-in via a new setting, or (b) explicitly documented in `CLAUDE.md` and the plan, in a follow-up doc PR. Not a blocker.

2. **`_redact_stderr` not directly unit-tested** — the redaction helper is called only from `logger.debug`, so the security surface is small (debug-level logging is typically off in production). If the redaction logic ever expands, add a focused unit test asserting that lines containing `key`, `token`, `secret`, `authorization` are dropped and that the result is truncated to ≤4096 chars.

3. **Schema `extra="ignore"` on top-level result** — future runner additions (e.g., a new `tools_used` list) are silently dropped before they reach `original_payload`. Acceptable for an advisory-only ingestion (we control which fields we persist), but if a follow-up wants to surface new runner fields in the UI, the schema will need an explicit update.

4. **`AdvisoryInvariantViolation` field detection** — `_validate_runner_payload` matches the first element of `error["loc"]` against `{"status","advisory_only","execution_allowed"}`. This works because all three are top-level fields. If a future schema move turns one of these into a nested field (e.g. `safety.advisory_only`), the routing would silently fall back to the parent `TradingAgentsRunnerError` class and callers checking `except AdvisoryInvariantViolation` would miss the violation. Keep this in mind if the schema is ever restructured.

None of the above changes the verdict.

---

## 6. PR-readiness checklist (plan §12 cross-check)

- [x] No `app/services/kis*`, `upbit*`, `brokers`, `order_service`, `watch_alerts`, `paper_trading_service` import.
- [x] No `place_order`, `manage_watch_alerts`, broker construction, Redis write, Paperclip call.
- [x] No `TradingDecisionAction` / `Counterfactual` / `Outcome` row created by service or tests.
- [x] `advisory_only=True` and `execution_allowed=False` present at three layers (Pydantic Literal pin, `session.market_brief`, `proposal.original_payload`).
- [x] No `os.environ` value appears in any log / exception message / persisted row / memory-log file.
- [x] No `subprocess.run(..., shell=True)` or f-string argv interpolation.
- [x] All §8 tests pass locally per Hermes verification (18 + 6 skipped).
- [x] `make lint && make typecheck` parity confirmed via Hermes ruff check + format pass on touched files.
- [ ] PR description should explicitly state **"advisory-only, no execution path"** in the summary — recommend the PR author include this phrasing when opening the GitHub PR.

---

## 7. Conclusion

The implementation faithfully realizes the ROB-9 plan with no scope drift, no execution-path leakage, and comprehensive test coverage of the safety invariants. All non-negotiable constraints in the handoff (advisory-only invariants, no broker / watch / order / Paperclip / Redis side effects, no env leakage, subprocess hardening) are enforced both in code and in CI-runnable tests.

**Recommendation:** ship as-is. Open the PR with `main` as the base, ensure the description explicitly includes "advisory-only, no execution path", and link Linear ROB-9.

```
AOE_STATUS: review_passed
AOE_ISSUE: ROB-9
AOE_ROLE: reviewer
AOE_REPORT_PATH: docs/plans/ROB-9-review-report.md
AOE_NEXT: create_pr
```
