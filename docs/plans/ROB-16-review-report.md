# ROB-16 Review Report

**AOE_STATUS:** review
**AOE_ISSUE:** ROB-16
**AOE_ROLE:** reviewer-opus
**AOE_NEXT:** authors apply note-level fixups (or accept as-is) and open PR; reviewer is read-only.

- **Commit reviewed:** `791c8436` — "ROB-16 add watch proximity monitor"
- **Diff base:** `origin/main`
- **Worktree / branch:** `feature/ROB-16-prefect-intraday-watch-proximity-monitor`
- **Plan:** `docs/plans/ROB-16-prefect-intraday-watch-proximity-monitor-plan.md`
- **Reviewer mode:** read-only (no edits, no rewrites)

---

## Verdict

**PASS_WITH_NOTES.**

All hard safety checks pass. The code is read-only with respect to watches,
broker state, and orders. Tests (21 new + 21 regression on existing watch
suite) and `ruff check` / `ruff format --check` are green. There are no
secrets-leakage vectors. Functionally correct for the price-proximity
visibility surface.

The implementation **deviates from the plan in several non-safety ways** that
should be acknowledged before merge — see §3 Notes. None of the deviations
are blocking. The most operationally consequential note is **N-3** (alert_type
reuse interacts with n8n's existing watch-alert dedupe).

---

## 1. Files reviewed (diff against `origin/main`)

```
app/jobs/watch_proximity_monitor.py                |  315 +++  (NEW)
app/services/watch_proximity.py                    |  150 ++   (NEW)
app/tasks/__init__.py                              |    2 +    (MODIFIED)
app/tasks/watch_proximity_tasks.py                 |   16 +    (NEW)
docs/plans/ROB-16-…-plan.md                        | 2346 +++   (PLAN, NEW)
tests/jobs/test_watch_proximity_monitor.py         |  241 ++   (NEW)
tests/services/test_watch_proximity.py             |  132 ++   (NEW)
tests/tasks/test_watch_proximity_tasks.py          |   54 +    (NEW)
```

No edits to `app/services/watch_alerts.py`, `app/jobs/watch_scanner.py`,
`app/services/openclaw_client.py`, `app/core/config.py`, `env.example`, or
any TradingAgents / decision-session module. No new dependencies; no
`prefect` import or package addition.

---

## 2. Hard safety checks

All must hold; all do.

| # | Check | Result | Evidence |
|---|---|---|---|
| 1 | No live order placement | ✅ | `grep -nE "place_order"` on the three new modules → no matches |
| 2 | No `place_order(..., dry_run=False)` | ✅ | No `place_order` call site at all |
| 3 | No watch registration | ✅ | `grep -nE "register_watch_alert\|add_watch"` on monitor → no matches |
| 4 | No order intent creation | ✅ | `grep -nE "create_order_intent\|submit_order"` → no matches |
| 5 | No broker side-effect APIs | ✅ | No imports from `kis_trading_service`, `kis_websocket*`, `upbit_trading_service`, `orders`, `order_execution`, `paper_trading_service`, `crypto_trade_cooldown_service`, `fill_notification`, `execution_event` |
| 6 | Monitor must not remove/trigger/mutate watches | ✅ | `grep -nE "trigger_and_remove\|remove_watch"` on `app/jobs/watch_proximity_monitor.py` → no matches; `tests/jobs/test_watch_proximity_monitor.py:115,152,184` assert `watch_service.removed == []` after each scan including the `hit`-band case |
| 7 | Notification text says final user approval is required | ✅ | `app/services/watch_proximity.py:124,138` always appends `"This is an informational alert only; any order requires final user approval."`; `tests/services/test_watch_proximity.py:117` asserts the literal substring; orchestrator test `tests/jobs/test_watch_proximity_monitor.py:114` asserts `"final user approval"` is in the dispatched message |
| 8 | No secrets / env values printed or persisted | ✅ | `grep -nE "API_KEY\|APP_SECRET\|SECRET_KEY\|TOKEN\|os\.environ\|getenv"` on the three new modules → no matches; logging strings reference only `exc` text and field metadata, never config values |

**Module-level import safety:**

`tests/services/test_watch_proximity.py:122` and
`tests/jobs/test_watch_proximity_monitor.py:225` and
`tests/tasks/test_watch_proximity_tasks.py:38` each assert that the
implementation source contains none of: `app.services.orders`,
`kis_trading_service`, `order_execution`, `orders_registration`,
`watch_alerts_registration`, `create_order_intent`, `submit_order`,
`place_order`, `register_watch_alert`. All three pass.

---

## 3. Notes (non-blocking deviations from plan / clarifications)

### N-1 — File layout collapsed (acceptable)

Plan called for separate `watch_proximity_helpers.py`,
`watch_proximity_dedupe.py`, `watch_proximity_notifier.py`. Implementation
collapses helpers into a single `app/services/watch_proximity.py` (150 LOC)
and folds `RedisProximityDedupeStore` and `OpenClawProximityNotifier` into
`app/jobs/watch_proximity_monitor.py` (315 LOC). The single-orchestrator file
is reasonably sized; readability is acceptable; behavior is identical.
Worth noting as a layout drift vs the plan, not a code-quality concern.

### N-2 — Scope narrowed to price-only conditions

The plan covered full-metric proximity (price / index / fx / trade_value /
rsi). The implementation **only** handles `price_above` and `price_below`
(`app/services/watch_proximity.py:13`,
`app/jobs/watch_proximity_monitor.py:194-203`). Non-price watches are
counted under `unsupported` and skipped without raising. The hand-off
described "skips unsupported metrics" so this is a deliberate scope choice,
and the issue text emphasises price proximity to the trigger threshold.

**Consequence:** RSI, trade-value, index, and FX watches do **not** get
proximity coverage. Acceptable for ROB-16 if product agrees; flag for the
next iteration if non-price proximity is wanted.

### N-3 — n8n `alert_type` reuse (operationally significant)

`OpenClawProximityNotifier.__call__` (`app/jobs/watch_proximity_monitor.py:102`)
calls `OpenClawClient.send_watch_alert_to_n8n(...)`, which sends the existing
payload `{"alert_type": "watch", ...}`. The plan recommended adding a new
`send_watch_proximity_alert_to_n8n` method with `alert_type="watch_proximity"`
to keep proximity (advisory) and trigger fires (threshold reached) distinct
in the n8n workflow.

**Why this matters:**

- The downstream n8n workflow already has dedupe logic keyed on
  `{market}:{target_kind}:{symbol}:{condition_type}:{threshold}` with a
  multi-hour cooldown. Proximity events with the same key as a recently
  fired trigger could be silently swallowed by n8n, or vice versa.
- The Discord channel routing on the n8n side currently treats `alert_type:
  watch` as a triggered fire; proximity messages will appear in the same
  channel with the same formatting expectations.
- Our own Redis dedupe (`watch:proximity:sent:…`) is independent of n8n's,
  so the two layers may interact in non-obvious ways.

**Mitigation already present:** the message body itself is distinctive
("Watch proximity alerts (…)" header + the disclaimer line + per-row `band=`
field), and our local cooldown defaults to 1 hour, so duplicate-storms are
contained.

**Recommended follow-up:** add a `send_watch_proximity_alert_to_n8n` method
on `OpenClawClient` (additive, no behavior change to existing callers) and
flip `OpenClawProximityNotifier` to call it. n8n side can then route
`alert_type="watch_proximity"` separately. Non-blocking for ROB-16 because
the safety/disclaimer guarantees still hold, but worth a follow-up ticket.

### N-4 — No feature flag / opt-in

Plan added `WATCH_PROXIMITY_ENABLED` (default `false`) and a short-circuit
in the task wrapper. Implementation has **no flag**: the task is registered
with `cron="*/5 * * * *"` Asia/Seoul and runs immediately once deployed
(`app/tasks/watch_proximity_tasks.py:7-16`, `app/tasks/__init__.py:9,15`
adds it to `TASKIQ_TASK_MODULES`).

**Why this is acceptable in safety terms:** the surface is read-only, has no
broker effects, and emits notifications gated by Redis dedupe. The
disclaimer text is mandatory.

**Why it's still worth noting:** any post-merge regression in the new code
will be visible to recipients of the watch-alert Discord channel within
five minutes of deploy with no operator opt-in. Consider adding a
`WATCH_PROXIMITY_ENABLED` setting (or commenting out the schedule for one
deploy cycle) before turning it on in production.

### N-5 — `distance_abs` field stores signed distance (naming)

`compute_price_proximity` (`app/services/watch_proximity.py:81-89`) computes
`raw_distance = threshold - current` (for `price_above`) or
`current - threshold` (for `price_below`) and assigns it to
`distance_abs` **without** taking absolute value. The hand-off note
explicitly called this "signed remaining distance" so the *value* is correct
and intentional, but the field name `distance_abs` is misleading — a reader
might assume it is unsigned. Tests verify the signed value
(`tests/services/test_watch_proximity.py:39,71`).

**Recommended follow-up:** rename to `distance_signed` (or keep `distance_abs`
and apply `abs()` and add a separate `distance_signed`). Cosmetic; non-blocking.

### N-6 — No manual-run script

Plan added `scripts/run_watch_proximity_monitor.py`. Implementation skipped
it. Manual ad-hoc runs require either invoking the Taskiq task by name or
running the orchestrator from a Python REPL. Not blocking; mention as a
small DX gap.

### N-7 — No `app/core/config.py` / `env.example` changes

Plan added settings entries (cooldown TTL, band thresholds, enabled flag).
Implementation hard-codes:

- `DEFAULT_PROXIMITY_COOLDOWN_SECONDS = 60 * 60` (1 hour) at
  `app/jobs/watch_proximity_monitor.py:23`,
- band thresholds inline at `app/services/watch_proximity.py:38-45`
  (`0.5%` and `1.0%`).

Acceptable for a first iteration; if operators need to tune cooldown or
bands without a code change, lift these into settings later.

---

## 4. Test verification (re-run by reviewer)

**New suite — all passing:**

```
$ uv run pytest tests/services/test_watch_proximity.py \
                tests/jobs/test_watch_proximity_monitor.py \
                tests/tasks/test_watch_proximity_tasks.py -v
…
======================== 21 passed, 2 warnings in 2.91s ========================
```

**Existing watch-alert suite — no regression:**

```
$ uv run pytest tests/test_watch_alerts.py \
                tests/test_watch_scanner.py \
                tests/test_watch_scan_tasks.py
…
======================== 21 passed, 2 warnings in 2.46s ========================
```

(The two `PydanticDeprecatedSince20` warnings originate from
`app/auth/schemas.py` and are unrelated to ROB-16.)

**Lint / format:**

```
$ uv run ruff check app/services/watch_proximity.py \
                    app/jobs/watch_proximity_monitor.py \
                    app/tasks/watch_proximity_tasks.py \
                    app/tasks/__init__.py \
                    tests/services/test_watch_proximity.py \
                    tests/jobs/test_watch_proximity_monitor.py \
                    tests/tasks/test_watch_proximity_tasks.py
All checks passed!

$ uv run ruff format --check app/services/watch_proximity.py \
                             app/jobs/watch_proximity_monitor.py \
                             app/tasks/watch_proximity_tasks.py
3 files already formatted
```

The implementation claims (21 focused passed, 8 safety regressions passed,
ruff check/format passed) match what I observed; the safety-test count
appears to refer to the 8 `assert*_not_awaited` / read-only / forbidden-token
assertions inside the suite, not separate test functions.

---

## 5. Plan-vs-implementation matrix

| Plan item | Implemented? | Notes |
|---|---|---|
| Read active watch alerts (read-only) | ✅ | `WatchAlertService.get_watches_for_market` only; no list_watches mutation, no add/remove |
| Market-hours gating KR/US | ✅ | Reuses `WatchScanner._is_market_open` (XKRX/XNYS via `exchange_calendars`); skips before any quote fetch (asserted) |
| Fetch latest quotes | ✅ | Reuses `WatchScanner._get_current_value` via injected callable |
| Distance to threshold (abs + pct) | ✅ | `distance_abs` (signed — see N-5), `distance_pct` |
| Configurable bands (1%, 0.5%, hit) | ⚠️ | Bands are correct but **inline**, not configurable via settings — see N-7 |
| Dedupe to avoid spam | ✅ | Redis `SET NX EX` in `RedisProximityDedupeStore`; in-memory fake covers cooldown semantics |
| Manual + scheduled run | ⚠️ | Scheduled via Taskiq cron; **no `scripts/run_*.py`** — see N-6 |
| Outside market hours: skip / non-actionable summary | ✅ | `status="skipped", reason="market_closed"`, no quote fetch, no notifier call |
| Disclaimer in notification | ✅ | Always appended in `format_proximity_message`; asserted by both helper and orchestrator tests |
| No live orders / `dry_run=False` | ✅ | Hard safety checks #1, #2, #4, #5 |
| No watch registration / mutation | ✅ | Hard safety check #3, #6 |
| TradingAgents stays advisory_only | ✅ | No imports from `tradingagents_research_service` / `trading_decision_*` |
| No secrets / env / token leakage | ✅ | Hard safety check #8 |
| Feature flag (off by default) | ❌ | Not implemented — see N-4 |
| Prefect-compatible without dep | ✅ | No `prefect` import; orchestrator is a plain async class wrappable later |
| Additive `send_watch_proximity_alert_to_n8n` | ❌ | Implementation reuses existing `send_watch_alert_to_n8n` — see N-3 |
| Pure helpers in own module | ✅ | `app/services/watch_proximity.py` is import-clean |

Eight of the eight hard safety items pass. Three plan items (configurable
bands, manual-run script, additive n8n method, feature flag) are partial /
not implemented; none are safety blockers.

---

## 6. Smoke recommendation (post-merge)

Before flipping the cron to active in production, run a one-shot manual
validation in a non-production environment:

```bash
# 1. Start docker compose stack so Redis is reachable.
docker compose up -d redis

# 2. Insert a temporary watch via the existing watch service so the monitor
#    has a row to evaluate. (Use a clearly-marked test symbol; keep the watch
#    threshold within the proximity band of the current price.)
#    Use the existing manage_watch_alerts MCP tool / API.

# 3. Trigger the proximity scan in-process (no scheduler needed):
uv run python - <<'PY'
import asyncio
from app.jobs.watch_proximity_monitor import WatchProximityMonitor

async def main() -> None:
    monitor = WatchProximityMonitor()
    try:
        result = await monitor.run()
    finally:
        await monitor.close()
    print(result)

asyncio.run(main())
PY

# 4. Verify:
#    - The Discord/n8n notification (if N8N_WATCH_ALERT_WEBHOOK_URL is set
#      for the test env) contains the disclaimer line.
#    - The Redis dedupe key `watch:proximity:sent:watch-proximity:{...}`
#      exists with TTL ≈ 3600s:
docker compose exec redis redis-cli --scan --pattern "watch:proximity:sent:*"
docker compose exec redis redis-cli ttl "<key from above>"

# 5. Clean up the temporary watch and any dedupe keys you created.
```

If the smoke run is clean, the production schedule will activate at the
next `*/5 * * * *` Asia/Seoul tick after deploy.

> **Operational caveat (revisits N-3 + N-4):** because the n8n payload still
> uses `alert_type="watch"`, the existing watch-alert Discord channel /
> n8n routing will receive proximity events. Confirm this is desired with
> the channel owners before merge, or land the additive
> `send_watch_proximity_alert_to_n8n` follow-up first.

---

## 7. PR readiness

**Ready to open PR against `main`** with the following caveats called out
in the PR description:

1. Note **N-3** (n8n `alert_type` reuse) and the recommended follow-up to
   add `send_watch_proximity_alert_to_n8n`. Get the n8n channel owner's
   sign-off, or hold merge until the additive method is in place.
2. Note **N-4** (no feature flag) — confirm with operator that the monitor
   should run automatically on first deploy. If not, either land a flag or
   merge with the cron temporarily commented out.
3. List the deferred items (N-2 non-price metrics, N-6 manual script,
   N-7 settings) as follow-ups so they don't get lost.

Suggested PR title: **`ROB-16: read-only watch proximity monitor (Taskiq)`**.

PR body should include a short test plan:

- [ ] `uv run pytest tests/services/test_watch_proximity.py tests/jobs/test_watch_proximity_monitor.py tests/tasks/test_watch_proximity_tasks.py -v` — 21 passing.
- [ ] `uv run pytest tests/test_watch_alerts.py tests/test_watch_scanner.py tests/test_watch_scan_tasks.py -v` — 21 passing (no regression).
- [ ] `uv run ruff check` and `uv run ruff format --check` clean on the new files.
- [ ] Smoke recommendation above (manual scan) executed in staging.

---

**AOE_STATUS:** review-complete
**AOE_ISSUE:** ROB-16
**AOE_ROLE:** reviewer-opus
**AOE_NEXT:** authors decide on N-3 / N-4 disposition (land follow-ups now or note in PR description), then open PR against `main`.
