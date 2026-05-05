# ROB-122 Watch Alert Router — Reviewer Report (Phase 0)

- **Reviewer:** Claude Code Opus (independent reviewer)
- **Branch:** `feature/ROB-122-watch-alert-router`
- **Implementer commit:** `9ecb64b0` — `fix(ROB-122): isolate watch alert failures and add router seam`
- **Base:** `main` @ `67434624`
- **Verdict:** **PASS**

---

## 1. Acceptance criteria coverage

| AC | Status | Evidence |
|---|---|---|
| US watch scan continues after quote lookup failures | ✅ | `app/jobs/watch_scanner.py:324-409` wraps the entire per-watch evaluation in `try/except Exception`, increments a `failed_lookups` counter, logs at WARNING, and `continue`s. `app/jobs/watch_scanner.py:466-485` adds a defense-in-depth `try/except` around `scan_market(market)` inside `run()` so any unexpected error in one market produces a `{"status": "failed", "reason": "scan_aborted"}` entry and the next market still runs. Tests `test_scan_market_us_yahoo_failure_does_not_abort_other_watches`, `test_run_continues_other_markets_when_scan_market_raises`, `test_scan_market_records_failed_lookups_in_result` exercise both layers. |
| `WATCH_ALERT_ROUTER_URL` with backward-compat fallback to `N8N_WATCH_ALERT_WEBHOOK_URL` | ✅ | `app/services/openclaw_client.py:125-134` (`_resolve_watch_alert_url`) prefers `WATCH_ALERT_ROUTER_URL` and falls back to `N8N_WATCH_ALERT_WEBHOOK_URL`. `app/core/config.py:347-355` declares both fields with `""` defaults; `env.example:122-127` documents them with the legacy var marked deprecated. Tests `test_send_watch_alert_to_router_prefers_router_url_over_legacy` and `test_send_watch_alert_to_router_falls_back_to_legacy_n8n_url` lock in the precedence. |
| Production/staging configurable for new route without n8n | ✅ | The router URL is a free-form HTTP endpoint; the receiver is implementation-neutral. Operator can point `WATCH_ALERT_ROUTER_URL` at any service that accepts the documented JSON contract (`{alert_type, correlation_id, as_of, market, triggered, intents, message}`). |
| Prefect receives/routes synthetic event OR documented Phase 0 stop | ✅ | Plan §2.50–§2.59 explicitly scopes this PR to Phase 0 (auto_trader router contract only). Phase 1 Prefect receiver, Phase 2 Hermes follow-up, Phase 3 cleanup are documented as out-of-scope follow-ups in `docs/plans/ROB-122-watch-alert-router-plan.md`. The ROB-122 acceptance criteria summary explicitly permits "Phase 0 may intentionally stop at the auto_trader router contract if clearly documented". |
| Hermes follow-up optional/config-gated, separated from immediate alert | ✅ | No Hermes/LLM coupling on the synchronous alert path. `OpenClawClient.send_watch_alert_to_router` only POSTs the JSON contract. Plan §2 explicitly relegates Hermes follow-up to Phase 2 inside the Prefect receiver. |
| Tests cover scanner failure isolation, config fallback, delivery results, payload contract | ✅ | See §2 verification evidence below. |
| No broker/order mutation; secrets not printed | ✅ | See §4 Safety review. |

---

## 2. Verification evidence

Commands run from the worktree:

```text
$ git status --short --branch
## feature/ROB-122-watch-alert-router

$ git diff main...HEAD --stat
 app/core/config.py                            |   6 +-
 app/jobs/watch_proximity_monitor.py           |   2 +-
 app/jobs/watch_scanner.py                     | 172 ++++---
 app/services/openclaw_client.py               |  38 +-
 docs/plans/ROB-122-watch-alert-router-plan.md | 673 ++++++++++++++++++++++++++
 env.example                                   |   6 +-
 tests/test_openclaw_client.py                 |  95 +++-
 tests/test_watch_scanner.py                   | 126 ++++-
 8 files changed, 1009 insertions(+), 109 deletions(-)

$ uv run pytest tests/test_watch_scanner.py tests/test_openclaw_client.py -q
... 47 passed, 11 skipped, 2 warnings in 1.94s

$ uv run pytest tests/test_watch_alerts.py tests/test_mcp_watch_alerts.py tests/test_watch_scan_tasks.py -q
... 18 passed, 2 warnings in 2.90s

$ uv run ruff check app/jobs/watch_scanner.py app/services/openclaw_client.py app/core/config.py app/jobs/watch_proximity_monitor.py tests/test_watch_scanner.py tests/test_openclaw_client.py
All checks passed!

$ uv run ruff format --check app/jobs/watch_scanner.py app/services/openclaw_client.py app/core/config.py app/jobs/watch_proximity_monitor.py tests/test_watch_scanner.py tests/test_openclaw_client.py
6 files already formatted
```

The reviewer did not run live broker/order calls, deploys, schedulers, or the Prefect receiver. Only read-only inspection and unit tests were executed.

### Test meaningfulness audit

- `test_scan_market_us_yahoo_failure_does_not_abort_other_watches` (`tests/test_watch_scanner.py:328-368`) seeds *two* watch rows on `us`, makes the first symbol's `_get_price` raise, asserts the second symbol still triggers (`alerts_sent == 1`, `failed_lookups == 1`) and that *only* the surviving field is removed from the watch service. This is a real isolation test, not a tautology.
- `test_run_continues_other_markets_when_scan_market_raises` (`tests/test_watch_scanner.py:371-396`) monkeypatches `scan_market` to raise on `us` and asserts `crypto`/`kr` still produced result entries and `us` is reported as `status="failed", reason="scan_aborted"`. Exercises the outer `run()` wrapper directly.
- `test_scan_market_records_failed_lookups_in_result` (`tests/test_watch_scanner.py:399-441`) asserts the `failed_lookups` counter is surfaced in the per-market dict even when no alert is sent. Good for downstream observability.
- `test_send_watch_alert_to_router_prefers_router_url_over_legacy` and `test_send_watch_alert_to_router_falls_back_to_legacy_n8n_url` (`tests/test_openclaw_client.py:1335-1408`) assert the exact target URL the HTTP client receives, so the precedence is observable end-to-end and not just a getter unit test.
- `test_send_watch_alert_to_router_skips_when_no_url_configured` updates the skipped reason from `n8n_webhook_not_configured` to `router_not_configured`, and `tests/test_watch_scanner.py:test_scan_market_keeps_watch_records_when_n8n_delivery_skipped` (kept) confirms the scanner does *not* remove watches on `router_not_configured` skip.

---

## 3. Cross-cutting consistency

- The `OpenClawClient.send_watch_alert_to_n8n` → `send_watch_alert_to_router` rename is fully propagated:
  - `app/jobs/watch_scanner.py:278`
  - `app/jobs/watch_proximity_monitor.py:102` (this caller was not listed in the plan §3.1 but had to be updated because of the method rename — correctly done)
  - `tests/test_watch_scanner.py` `_FakeOpenClawClient.send_watch_alert_to_router`
  - `tests/test_openclaw_client.py` (4 tests renamed + 2 new tests added)
  - Deprecated `_send_market_alert(category="watch")` and `send_watch_alert(message)` legacy paths kept untouched as planned; their inline comments now reference `send_watch_alert_to_router`.
- `grep` confirms zero remaining `send_watch_alert_to_n8n` references in `app/` and `tests/`. Surviving mentions in `docs/superpowers/specs/`, `docs/superpowers/plans/`, `docs/plans/ROB-16-*`, and `n8n/README.md` are historical plans/specs and not load-bearing on runtime behavior.

---

## 4. Safety review

- **Broker/order mutation:** None. Diff is limited to (a) per-watch try/except boundaries, (b) URL resolution helper, (c) HTTP POST seam rename. The `WatchOrderIntentService.emit_intent` path is untouched. No `dry_run=False`, no Alpaca / KIS / Upbit calls, no scheduler edits.
- **Secrets:** No real secrets in the diff. Tests use placeholder URLs (`http://127.0.0.1:5678/webhook/watch-alert`, `http://127.0.0.1:9999/router/watch-alert`) and obvious placeholder strings (`test-token`, `cb-token`). The router URL is read from settings and only printed in payload-target form during HTTP `post()`; it is never logged. Config docstring and `env.example` describe roles, not values.
- **Logging:** New log strings (`"Watch alert router send start/sent/failed/error"`) include `correlation_id`, `request_id`, `market`, attempt number, and exception object only — no secret material. The skipped-reason code (`router_not_configured`) is a stable enum, not a URL.
- **Network:** No new outbound destinations. Behavior with `WATCH_ALERT_ROUTER_URL=""` and `N8N_WATCH_ALERT_WEBHOOK_URL` set is byte-for-byte the legacy n8n path (verified via `test_send_watch_alert_to_router_falls_back_to_legacy_n8n_url` + `test_send_watch_alert_to_router_posts_payload` payload assertion).

---

## 5. Non-blocking notes / risks

These are **not** blockers; they are observations for the implementer / next phase.

1. **`tests/conftest.py:74-76`** sets only `N8N_WATCH_ALERT_WEBHOOK_URL` to `""` for tests; it does not also default `WATCH_ALERT_ROUTER_URL`. In practice the Pydantic field default is `""`, so the test environment behaves correctly today, but adding `"WATCH_ALERT_ROUTER_URL": ""` next to the existing line would make the intent explicit and immune to future env-leak surprises in CI containers. *(Documentation/hygiene only — not a correctness issue.)*
2. **`app/jobs/watch_scanner.py:401-409`** catches `Exception` per watch. The current scope is correct — it is the user-visible bug fix. As a follow-up, consider also incrementing a metric/counter (Prometheus or otherwise) so a watch silently failing for many cycles is visible without grepping warnings. Could be picked up alongside the Phase 1 receiver work.
3. **`app/jobs/watch_proximity_monitor.py:102`** was renamed to `send_watch_alert_to_router` correctly even though it was not enumerated in plan §3.1. This is the right decision (mandatory because of the method rename) and the existing proximity monitor tests would have caught a missed rename, but the plan should mention this caller in any retrospective so future readers don't think the rename is incomplete.
4. **`docs/plans/ROB-122-watch-alert-router-plan.md` line 32** still references "`app/core/config.py:351`, `app/services/openclaw_client.py:381`" — historical line numbers from before this PR's edits. Cosmetic only; the plan is otherwise consistent with the implementation.

---

## 6. Production activation steps requiring explicit operator approval

The following steps are **not** done by this PR and **must** be approved separately before any production rollout:

1. **Set `WATCH_ALERT_ROUTER_URL` in production/staging `.env.prod`.** Until set, the legacy `N8N_WATCH_ALERT_WEBHOOK_URL` fallback continues to deliver to n8n exactly as today. Any cutover to a new receiver is a config change, not a code change — but it is still a config change in a privileged environment.
2. **Bring the Phase 1 Prefect-side webhook receiver online** before pointing `WATCH_ALERT_ROUTER_URL` at it. Until that receiver exists and has been verified for ≥ 2 weeks of side-by-side delivery against n8n (per plan §2 migration path), the legacy n8n webhook should remain the active receiver.
3. **Hermes/LLM follow-up flow (Phase 2)** is intentionally out of scope here and requires its own design + approval inside `~/services/prefect`. It must remain async and decoupled from the synchronous alert path.
4. **Decommission of `paperclip-watch-alert.json` and removal of `N8N_WATCH_ALERT_WEBHOOK_URL`** is Phase 3 and requires explicit approval after Phase 1 stability is demonstrated.
5. **Scheduler / cron changes** (`app/tasks/watch_scan_tasks.py` 5-min taskiq cron) are explicitly out of scope. Any frequency, deploy, or scheduler activation change requires a separate ticket and approval.
6. **No broker activation** is implied or enabled by this PR. The watch-order-intent path is unchanged; any move from `dry_run=True` to live order placement is gated elsewhere and remains so.

---

## 7. Final AOE status block

```
AOE_STATUS: review_passed
AOE_ISSUE: ROB-122
AOE_ROLE: reviewer
AOE_AGENT: claude-code-opus
AOE_REPORT_PATH: docs/plans/ROB-122-watch-alert-router-review.md
AOE_NEXT: pr_ci_or_deploy
```
