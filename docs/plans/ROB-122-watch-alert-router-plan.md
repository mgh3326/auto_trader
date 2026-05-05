# ROB-122 Watch Alert Router Implementation Plan

> **For agentic workers:** Implement this plan task-by-task in the assigned ROB-122 worktree/branch. Use the repository's normal Hermes/AoE workflow: planner/reviewer = Opus, implementer = Sonnet, one implementer editing at a time. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Restore reliable delivery of `scan.watch_alerts` (so a single Yahoo/yfinance quote failure can no longer abort the whole scan) and rename the delivery seam from `*_n8n` / `N8N_WATCH_ALERT_WEBHOOK_URL` to a transport-neutral router seam (`WATCH_ALERT_ROUTER_URL`) with backward compatibility, paving the way for an eventual Prefect-side webhook receiver without coupling this PR to it.

**Architecture:** Scope this PR (Phase 0) to the auto_trader side only — per-watch resilience + router rename. Keep the existing n8n webhook (`paperclip-watch-alert.json`) as the active receiver until a follow-up PR brings the Prefect-side webhook receiver online. Initial router transport (Phase 1, follow-up PR) is a webhook receiver in `~/services/prefect`, not DB outbox or direct deployment-run, because it preserves the immediate-alert latency budget, mirrors the existing n8n contract for an easy A/B cutover via a single env var, and stays out of the auto_trader hot path. Hermes/LLM follow-up is explicitly *not* on the synchronous alert path; it is a Phase 2 async flow on the Prefect side.

**Tech Stack:** Python 3.13, FastAPI/httpx, taskiq, pytest, Pydantic-Settings; Prefect 3.6 for Phase 1 follow-up.

---

## 1. Current code path and failure modes

### 1.1 Code path

1. **Schedule** — `app/tasks/watch_scan_tasks.py:run_watch_scan_task` runs every 5 min (`*/5 * * * *`, Asia/Seoul) via taskiq.
2. **Scanner.run()** — `app/jobs/watch_scanner.py:WatchScanner.run` iterates markets `("crypto", "kr", "us")`, calling `scan_market(market)` sequentially.
3. **Per-watch loop** — `scan_market` enumerates `WatchAlertService.get_watches_for_market`, then for each watch calls `_get_current_value` → `_get_price` / `_get_rsi` / `_get_trade_value` / `_get_index_price` / `_get_fx_price`, all of which delegate to `app/services/market_data.get_quote` / `get_ohlcv`.
4. **Trigger evaluation** — `_is_triggered(current, operator, threshold)` decides; on hit the watch is added to `triggered`/`intents` lists.
5. **Delivery** — `_send_alert` builds a UUID `correlation_id`, posts JSON to `OpenClawClient.send_watch_alert_to_n8n`, which POSTs `{alert_type, correlation_id, as_of, market, triggered, intents, message}` to `settings.N8N_WATCH_ALERT_WEBHOOK_URL` with up to 4 attempts (1 + 3 retries, 1→2→4 s exponential). The `_send_alert` wrapper also catches all exceptions and converts them to `WatchAlertDeliveryResult(status="failed", reason="request_failed")`.
6. **Removal** — On `status == "success"`, each triggered field is removed via `WatchAlertService.trigger_and_remove`. On non-success, watches are kept for the next cycle.
7. **Receiver** — `n8n/workflows/paperclip-watch-alert.json` validates, dedupes (6 h cooldown), branches by market, posts to Discord, then writes `sentMap` only on Discord success (ROB-178 hardening).

### 1.2 Failure modes

| # | Symptom | Root cause | Where it surfaces |
|---|---------|------------|-------------------|
| **A** | One Yahoo/yfinance failure aborts the entire scan task — including unrelated crypto/kr watches still in queue. | `app/jobs/watch_scanner.py:_get_price` for `market == "us"` raises `RuntimeError("US watch price fetch failed for ... invalid close")` when `market_data.get_quote` returns no close, and `market_data.get_quote(equity_us, …)` itself raises `SymbolNotFoundError`. The exception escapes the `for watch in watches:` loop in `scan_market`, escapes `scan_market`, and escapes `run()`. Whichever markets had not yet been scanned are silently skipped. | `tests/test_watch_scanner.py::test_get_price_us_raises_when_yahoo_fails` documents the current raise contract. |
| **B** | RSI / trade-value / OHLCV transient failures abort the scan with the same blast radius as **A**. | `_get_rsi` and `_get_trade_value` use `market_data_service.get_ohlcv` / `get_quote` and propagate raw exceptions. | Same loop, same lack of per-watch boundary. |
| **C** | n8n outage or Discord persistent 5xx silently drops the alert until the operator notices missing Discord notifications. | Delivery is single-receiver. `_send_alert` returns `failed` and watches are kept, but if Sentry/log review is delayed the operator has no proactive signal. | `OpenClawClient.send_watch_alert_to_n8n` retry exhaustion. (Out of scope for Phase 0; tracked for Phase 1.) |
| **D** | Naming lock-in: env var `N8N_WATCH_ALERT_WEBHOOK_URL` and method `send_watch_alert_to_n8n` make the receiver swap a code change instead of a config change. | The seam is named after the implementation, not the role. | `app/core/config.py:351`, `app/services/openclaw_client.py:381`, `tests/test_openclaw_client.py`. |

The fix for **A**/**B** is a per-watch try/except boundary inside `scan_market` so a single symbol's lookup failure becomes a counted, logged event and the loop continues. The fix for **D** is to introduce `WATCH_ALERT_ROUTER_URL` (preferred), fall back to `N8N_WATCH_ALERT_WEBHOOK_URL` (back-compat), and rename the client method to a transport-neutral name. **C** is addressed in Phase 1 by the Prefect-side receiver, which can fan out to alternative sinks (Telegram already mirrored elsewhere) and emit its own observability.

---

## 2. Initial router transport choice

We pick **Option 1: webhook receiver** for the eventual Prefect-side router.

### Tradeoff matrix

| Option | Latency from trigger to Discord | Auto-trader code surface | Operability / rollback | Fits "Hermes is follow-up only" | Verdict |
|---|---|---|---|---|---|
| **(1) Webhook receiver** (Prefect-side FastAPI/Prefect-webhook-trigger HTTP endpoint mirroring the n8n contract). | < 1 s, equivalent to n8n today. | One env-var flip to point `WATCH_ALERT_ROUTER_URL` at it; client/method rename only. | Operator can roll back to n8n by re-pointing the env var to `N8N_WATCH_ALERT_WEBHOOK_URL` and re-deploying. | Yes — receiver returns 200 immediately after Discord; Hermes is a separate flow spawned async. | **Chosen.** |
| **(2) DB outbox polling** (auto_trader writes pending alerts to Postgres; Prefect polls and dispatches). | Bounded by poll interval; ≥ 5 s realistic; loses the "immediate" property. | New `watch_alert_outbox` table, migration, writer, reconciliation. | Highest durability but the largest change set — Postgres schema changes are also the highest-blast-radius rollbacks. | Yes, but the latency cost is real. | Rejected — overkill for current volume; the existing 4-attempt retry + n8n `sentMap` already gives at-least-once with reasonable durability. Revisit only if Phase 1 reveals real durability gaps. |
| **(3) Direct Prefect deployment run via API** (auto_trader calls `POST /api/deployments/{id}/create_flow_run`). | Cold-start of Prefect deployment run: seconds to tens of seconds. | Auto-trader gains a Prefect API auth dependency and library. | Rollback also requires code changes (HTTP client → Prefect API client switch). | Yes, but per-flow-run overhead is heavy for a 5-min cron. | Rejected — wrong tool for synchronous alerts; correct tool for the *Hermes follow-up* flow in Phase 2. |

### Migration path

- **Phase 0 (this plan, this PR)** — Resilience fix in auto_trader + rename seam to `WATCH_ALERT_ROUTER_URL` with `N8N_WATCH_ALERT_WEBHOOK_URL` as fallback. n8n workflow stays the active receiver. **No Prefect repo changes.**
- **Phase 1 (follow-up PR, NOT in this plan's scope)** — `~/services/prefect` adds a webhook receiver flow + handler that consumes the same JSON contract and forwards to Discord. Operator points `WATCH_ALERT_ROUTER_URL` at it; n8n stays warm as a fallback. Verified for ≥ 2 weeks against the n8n side-by-side via Discord comparison. The receiver layout sketch (for reference, do **not** create in this PR):
  - `~/services/prefect/flows/auto_trader/watch_alert_router.py` — new `@flow` + FastAPI/HTTP receiver entrypoint.
  - `~/services/prefect/src/robin_automation/watch_alert_router.py` — validation/dedupe/Discord forward (mirrors `paperclip-watch-alert.json` JS).
  - `~/services/prefect/tests/test_watch_alert_router.py`.
  - Optional `launchd` plist for the receiver process.
- **Phase 2 (follow-up PR)** — Hermes async follow-up flow spawned from inside the Prefect receiver after Discord is sent. Auto-trader still untouched.
- **Phase 3 (cleanup PR)** — Once Phase 1 is stable for ≥ 2 weeks, decommission `paperclip-watch-alert.json` and drop the `N8N_WATCH_ALERT_WEBHOOK_URL` fallback. Out of scope here.

---

## 3. Files to change in this PR (Phase 0)

### 3.1 auto_trader (in scope)

- **Modify** `app/core/config.py` — add `WATCH_ALERT_ROUTER_URL: str = ""` next to `N8N_WATCH_ALERT_WEBHOOK_URL`. Both fields kept; resolution helper lives in `openclaw_client.py`.
- **Modify** `app/services/openclaw_client.py` — rename `send_watch_alert_to_n8n` → `send_watch_alert_to_router`; introduce private `_resolve_watch_alert_url()` that returns `WATCH_ALERT_ROUTER_URL` if non-empty, else `N8N_WATCH_ALERT_WEBHOOK_URL`. Update all log strings from `"N8N watch alert ..."` to `"Watch alert router ..."` so log breadcrumbs reflect the new naming. Adjust the `request_failed` reason code on the 0-URL skip to `router_not_configured` (was `n8n_webhook_not_configured`).
- **Modify** `app/jobs/watch_scanner.py` —
  1. Update `_send_alert` to call `send_watch_alert_to_router`.
  2. Wrap the per-watch evaluation block (everything from `_get_current_value` through `policy`/`emission`) in `try/except Exception` so a single failed lookup is logged + counted but does not abort the loop. Accumulate `failed_lookups: list[dict[str, str]]` and surface a `failed_lookups` count in the per-market result dict for observability. Re-raise nothing.
  3. Inside the `for market in (...)` loop in `run`, also wrap each `scan_market` call in `try/except Exception`, return a `{"status": "failed", "reason": "scan_aborted"}` per-market entry, and continue with the next market. Belt-and-suspenders so even a non-watch error (e.g. WatchAlertService Redis blip) cannot kill the cron.
- **Modify** `env.example` — add a `WATCH_ALERT_ROUTER_URL=` line above `N8N_WATCH_ALERT_WEBHOOK_URL=` with a comment that `N8N_WATCH_ALERT_WEBHOOK_URL` is the deprecated fallback.

### 3.2 auto_trader tests (in scope)

- **Modify** `tests/test_openclaw_client.py` —
  - Rename three tests (`test_send_watch_alert_to_n8n_*`) to `test_send_watch_alert_to_router_*`.
  - Update method calls and `WATCH_ALERT_ROUTER_URL` env-var references.
  - Add `test_send_watch_alert_to_router_prefers_router_url_over_legacy` (both set → router wins).
  - Add `test_send_watch_alert_to_router_falls_back_to_legacy_n8n_url` (only `N8N_WATCH_ALERT_WEBHOOK_URL` set → uses it).
  - Update the skipped-reason assertion to `router_not_configured`.
- **Modify** `tests/test_watch_scanner.py` —
  - Replace `test_get_price_us_raises_when_yahoo_fails` with `test_scan_market_us_yahoo_failure_does_not_abort_other_watches`. (The helper `_get_price` itself may keep raising; the scan loop is what needs to keep going. The new test sets up two `us` watches where the first raises and the second triggers, and asserts the second's alert was sent and the first was reported as a failed lookup but did NOT short-circuit the loop.)
  - Add `test_run_continues_other_markets_when_scan_market_raises` (defense-in-depth for the `run()` wrapper).
  - Add `test_scan_market_records_failed_lookups_in_result` (per-market dict contains `failed_lookups` count).
  - Update `_FakeOpenClawClient.send_watch_alert_to_n8n` → `send_watch_alert_to_router`, adjust `monkeypatch` calls accordingly.

### 3.3 Out of scope for this PR (documented for traceability)

- `~/services/prefect/flows/auto_trader/watch_alert_router.py` — Phase 1.
- `~/services/prefect/src/robin_automation/watch_alert_router.py` — Phase 1.
- `~/services/prefect/tests/test_watch_alert_router.py` — Phase 1.
- `n8n/workflows/paperclip-watch-alert.json` — unchanged in Phase 0; decommissioned in Phase 3.
- `tests/test_n8n_watch_alert_workflow.py` — unchanged in Phase 0; deleted in Phase 3.
- `app/tasks/watch_scan_tasks.py` cron — unchanged. Scheduler change is an explicit non-goal.

---

## 4. Minimum safe deliverable

**Phase 0 = this PR**, which is independently shippable and delivers the reliability fix (the actual user-visible bug) without depending on the Prefect router being online. Concretely the MSD is:

1. The per-watch `try/except` boundary in `scan_market` (fixes the **A/B** failure modes).
2. The `run()`-level `try/except` boundary (defense in depth).
3. The `WATCH_ALERT_ROUTER_URL` env var + `_resolve_watch_alert_url` helper with fallback to `N8N_WATCH_ALERT_WEBHOOK_URL`.
4. The `send_watch_alert_to_n8n` → `send_watch_alert_to_router` rename and log-string update.
5. Test updates above.

If even this PR is too large for one review, the smaller MSD-of-MSD is **just (1)+(2)+test changes** — leave the rename for a second PR. The router rename is a non-functional change and can ride alone safely once the resilience fix is in.

---

## 5. Non-goals and hard stops

This PR MUST NOT:

- Place or modify any live, paper, or mock orders. **No `dry_run=False`.** Watch scanner does not directly place orders, and the `WatchOrderIntentService.emit_intent` path is left exactly as-is — the only change near it is the surrounding try/except, which preserves its current return semantics.
- Mutate any broker/order/Alpaca/KIS/Upbit endpoint. The `OpenClawClient` method-rename touches *only* the watch-alert delivery seam; `send_fill_notification`, `request_analysis`, `send_scan_alert`, and `send_watch_alert` (deprecated) are left alone.
- Change any watch threshold, condition, dedupe window, or `WatchAlertService` schema.
- Print or log any secret, token, DSN, API key, or Redis password. Test fixtures use placeholder URLs (e.g. `http://127.0.0.1:5678/webhook/watch-alert`). Operator env files are not read or written by this plan.
- Touch the taskiq cron schedule (`*/5 * * * *` Asia/Seoul) in `app/tasks/watch_scan_tasks.py`. Scheduler changes are explicitly out of scope.
- Add a Prefect dependency to `auto_trader`. The Prefect-side receiver is a follow-up PR in `~/services/prefect`.
- Modify `n8n/workflows/paperclip-watch-alert.json` or its tests. The receiver is unchanged in Phase 0.
- Introduce backward-compat shims beyond the documented `N8N_WATCH_ALERT_WEBHOOK_URL` fallback. No re-export aliases for the old method name; just rename and update callers/tests in one go (per CLAUDE.md "avoid backwards-compatibility hacks").

---

## 6. TDD step-by-step implementation

Branch: `feature/ROB-122-watch-alert-router`. Use the active Kanban/AoE workspace if one is already assigned. If creating a fresh local worktree manually, use the canonical repo paths below.

### Setup

- [ ] **Step 0.1: Create the worktree**

```bash
cd /Users/mgh3326/work/auto_trader && git switch main && git pull
git worktree add /Users/mgh3326/work/auto_trader-worktrees/feature-ROB-122-watch-alert-router -b feature/ROB-122-watch-alert-router main
cd /Users/mgh3326/work/auto_trader-worktrees/feature-ROB-122-watch-alert-router
uv sync --all-groups
```

Expected: worktree exists; `uv sync` succeeds.

- [ ] **Step 0.2: Establish baseline test pass**

Run: `uv run pytest tests/test_watch_scanner.py tests/test_openclaw_client.py tests/test_watch_alerts.py tests/test_mcp_watch_alerts.py tests/test_watch_scan_tasks.py -v`

Expected: all green on `main`. Record the count for comparison.

### Task 1: Per-watch resilience in `scan_market`

**Files:**
- Modify: `app/jobs/watch_scanner.py`
- Test: `tests/test_watch_scanner.py`

- [ ] **Step 1.1: Write the failing test for per-watch resilience**

Append to `tests/test_watch_scanner.py`:

```python
@pytest.mark.asyncio
async def test_scan_market_us_yahoo_failure_does_not_abort_other_watches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scanner = WatchScanner()
    scanner._watch_service = _FakeWatchService()
    scanner._watch_service._rows_by_market["us"] = [
        {
            "target_kind": "asset",
            "symbol": "BADTKR",
            "condition_type": "price_below",
            "threshold": 100.0,
            "field": "asset:BADTKR:price_below:100",
        },
        {
            "target_kind": "asset",
            "symbol": "AAPL",
            "condition_type": "price_below",
            "threshold": 200.0,
            "field": "asset:AAPL:price_below:200",
        },
    ]
    scanner._openclaw = _FakeOpenClawClient(status="success")

    monkeypatch.setattr(scanner, "_is_market_open", lambda market: True)

    async def _price_side_effect(symbol: str, market: str) -> float:
        if symbol == "BADTKR":
            raise RuntimeError("US watch price fetch failed for BADTKR: invalid close")
        return 150.0

    monkeypatch.setattr(scanner, "_get_price", AsyncMock(side_effect=_price_side_effect))

    result = await scanner.scan_market("us")

    assert result["alerts_sent"] == 1
    assert result["status"] == "success"
    assert result.get("failed_lookups") == 1
    assert scanner._watch_service.removed_fields == [
        ("us", "asset:AAPL:price_below:200"),
    ]
```

- [ ] **Step 1.2: Run the new test and confirm it fails**

Run: `uv run pytest tests/test_watch_scanner.py::test_scan_market_us_yahoo_failure_does_not_abort_other_watches -v`

Expected: FAIL — `RuntimeError("US watch price fetch failed for BADTKR ...")` propagates out of `scan_market`.

- [ ] **Step 1.3: Implement the per-watch try/except**

In `app/jobs/watch_scanner.py:scan_market`, inside the `for watch in watches:` loop, wrap the work from `target_kind = ...` through the end of the loop body in `try/except Exception`. On exception, log a warning with `symbol`, `market`, and the exception, increment a local `failed_lookups` counter, and `continue`. After the loop, surface `failed_lookups` in every return dict (including the `success`, `skipped`, and `failed` branches). Sketch:

```python
async def scan_market(self, market: str) -> dict[str, object]:
    normalized_market = str(market).strip().lower()
    market_open = self._is_market_open(normalized_market)

    watches = await self._watch_service.get_watches_for_market(normalized_market)
    if not watches:
        # ... unchanged early-return branches; add failed_lookups=0 to each ...

    triggered: list[dict[str, object]] = []
    intents: list[dict[str, object]] = []
    triggered_fields: list[str] = []
    failed_lookups = 0
    kst_date = now_kst().date().isoformat()

    for watch in watches:
        try:
            # ... existing per-watch body, unchanged ...
        except Exception as exc:
            logger.warning(
                "Watch lookup failed (continuing): market=%s symbol=%s error=%s",
                normalized_market,
                str(watch.get("symbol") or "").strip().upper(),
                exc,
            )
            failed_lookups += 1
            continue

    if not triggered and not intents:
        # ... existing early-return; include failed_lookups in dict ...

    # ... existing send_alert + return; include failed_lookups in success and failed dicts ...
```

Every return dict from `scan_market` MUST include the key `"failed_lookups"` (int).

- [ ] **Step 1.4: Run the new test and confirm it passes**

Run: `uv run pytest tests/test_watch_scanner.py::test_scan_market_us_yahoo_failure_does_not_abort_other_watches -v`

Expected: PASS.

- [ ] **Step 1.5: Re-run the full watch scanner suite to catch regressions**

Run: `uv run pytest tests/test_watch_scanner.py -v`

Expected: every test from step 0.2 still passes. Two existing tests will likely break because they assert `result["reason"] == "no_triggered_alerts"` etc. without `failed_lookups`; those need a one-line update to add `result["failed_lookups"] == 0`. Update only those tests; do not change behavior under test.

- [ ] **Step 1.6: Replace the deprecated raise-contract test**

Remove `test_get_price_us_raises_when_yahoo_fails` (the helper-level raise contract is no longer load-bearing). The helper may still raise; that is fine because the scanner now catches. We only assert behavior at the public boundary (`scan_market`).

- [ ] **Step 1.7: Commit**

```bash
git add app/jobs/watch_scanner.py tests/test_watch_scanner.py
git commit -m "$(cat <<'EOF'
fix(ROB-122): isolate watch lookup failures per-symbol in scan_market

Yahoo/yfinance failures on a single US watch were aborting scan.watch_alerts
entirely, silently dropping unrelated crypto/kr triggers. Wrap per-watch
evaluation in try/except, count failed_lookups, continue the loop.

Co-Authored-By: Paperclip <noreply@paperclip.ing>
EOF
)"
```

### Task 2: Defense-in-depth wrapper around `scan_market` calls in `run()`

**Files:**
- Modify: `app/jobs/watch_scanner.py`
- Test: `tests/test_watch_scanner.py`

- [ ] **Step 2.1: Write the failing test for `run()` resilience**

Append:

```python
@pytest.mark.asyncio
async def test_run_continues_other_markets_when_scan_market_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scanner = WatchScanner()
    scanner._watch_service = _FakeWatchService(rows=[])
    scanner._openclaw = _FakeOpenClawClient(status="success")

    original_scan_market = scanner.scan_market

    async def _scan_market(market: str) -> dict[str, object]:
        if market == "us":
            raise RuntimeError("simulated unexpected scanner error")
        return await original_scan_market(market)

    monkeypatch.setattr(scanner, "scan_market", _scan_market)
    monkeypatch.setattr(scanner, "_is_market_open", lambda market: True)
    monkeypatch.setattr(scanner, "_get_price", AsyncMock(return_value=None))

    result = await scanner.run()

    assert set(result.keys()) == {"crypto", "kr", "us"}
    assert result["us"]["status"] == "failed"
    assert result["us"]["reason"] == "scan_aborted"
    assert result["crypto"]["alerts_sent"] == 0
    assert result["kr"]["alerts_sent"] == 0
```

- [ ] **Step 2.2: Run and confirm failure**

Run: `uv run pytest tests/test_watch_scanner.py::test_run_continues_other_markets_when_scan_market_raises -v`

Expected: FAIL — `RuntimeError` escapes `run()`.

- [ ] **Step 2.3: Implement the `run()` wrapper**

```python
async def run(self) -> dict[str, dict[str, object]]:
    results: dict[str, dict[str, object]] = {}
    for market in ("crypto", "kr", "us"):
        try:
            market_result = await self.scan_market(market)
        except Exception as exc:
            logger.error(
                "scan_market raised unexpectedly: market=%s error=%s",
                market,
                exc,
            )
            market_result = {
                "market": market,
                "status": "failed",
                "reason": "scan_aborted",
                "alerts_sent": 0,
                "details": [],
                "failed_lookups": 0,
            }
        results[market] = dict(market_result)
    return results
```

- [ ] **Step 2.4: Confirm pass**

Run: `uv run pytest tests/test_watch_scanner.py -v`

Expected: all green.

- [ ] **Step 2.5: Commit**

```bash
git add app/jobs/watch_scanner.py tests/test_watch_scanner.py
git commit -m "$(cat <<'EOF'
fix(ROB-122): trap unexpected scan_market errors in WatchScanner.run

Defense-in-depth: even if scan_market raises (Redis blip, watch service
failure, etc.), surface the failure as one market entry and keep the
remaining markets running.

Co-Authored-By: Paperclip <noreply@paperclip.ing>
EOF
)"
```

### Task 3: Add `WATCH_ALERT_ROUTER_URL` config

**Files:**
- Modify: `app/core/config.py`
- Modify: `env.example`
- Test: covered by Task 4 OpenClaw tests.

- [ ] **Step 3.1: Add the setting**

In `app/core/config.py`, near the existing `N8N_WATCH_ALERT_WEBHOOK_URL: str = ""` line:

```python
    # Watch Alert router (Phase 0 of ROB-122 — transport-neutral seam).
    # When set, takes precedence over N8N_WATCH_ALERT_WEBHOOK_URL.
    # N8N_WATCH_ALERT_WEBHOOK_URL is the deprecated backward-compat fallback.
    WATCH_ALERT_ROUTER_URL: str = ""
    # N8N Watch Alert webhook (deprecated fallback for WATCH_ALERT_ROUTER_URL).
    N8N_WATCH_ALERT_WEBHOOK_URL: str = ""
```

- [ ] **Step 3.2: Update `env.example`**

In `env.example`, replace the existing `N8N_WATCH_ALERT_WEBHOOK_URL=` block with:

```bash
# Watch Alert Router URL (transport-neutral seam, ROB-122).
# 예시: http://127.0.0.1:5678/webhook/watch-alert  또는  Prefect 라우터 URL
WATCH_ALERT_ROUTER_URL=
# Deprecated fallback when WATCH_ALERT_ROUTER_URL is unset.
N8N_WATCH_ALERT_WEBHOOK_URL=
```

- [ ] **Step 3.3: Sanity-run the settings load**

Run: `uv run python -c "from app.core.config import settings; print('router=', repr(settings.WATCH_ALERT_ROUTER_URL), ' legacy=', repr(settings.N8N_WATCH_ALERT_WEBHOOK_URL))"`

Expected: both print as empty strings (or whatever is in `.env`). **Do not echo any other settings.** If the operator's `.env` happens to have a non-empty value, treat the displayed value as `[REDACTED]` when discussing it.

- [ ] **Step 3.4: Commit**

```bash
git add app/core/config.py env.example
git commit -m "$(cat <<'EOF'
feat(ROB-122): add WATCH_ALERT_ROUTER_URL with N8N_WATCH_ALERT_WEBHOOK_URL fallback

Introduces a transport-neutral router seam so the watch alert receiver can be
swapped (n8n today, Prefect-side webhook receiver in a follow-up) by env var
alone. Backward-compatible: empty router URL falls through to the legacy n8n
URL.

Co-Authored-By: Paperclip <noreply@paperclip.ing>
EOF
)"
```

### Task 4: Rename `send_watch_alert_to_n8n` → `send_watch_alert_to_router`

**Files:**
- Modify: `app/services/openclaw_client.py`
- Modify: `app/jobs/watch_scanner.py`
- Test: `tests/test_openclaw_client.py`
- Test: `tests/test_watch_scanner.py`

- [ ] **Step 4.1: Write failing tests for router URL resolution**

Add to `tests/test_openclaw_client.py` (next to the existing watch alert tests):

```python
@pytest.mark.asyncio
@patch("app.services.openclaw_client.httpx.AsyncClient")
async def test_send_watch_alert_to_router_prefers_router_url_over_legacy(
    mock_httpx_client_cls: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        settings,
        "WATCH_ALERT_ROUTER_URL",
        "http://127.0.0.1:9999/router/watch-alert",
    )
    monkeypatch.setattr(
        settings,
        "N8N_WATCH_ALERT_WEBHOOK_URL",
        "http://127.0.0.1:5678/webhook/watch-alert",
    )

    mock_cli = AsyncMock()
    mock_res = MagicMock(status_code=200)
    mock_res.raise_for_status.return_value = None
    mock_cli.post.return_value = mock_res
    mock_client_instance = AsyncMock()
    mock_client_instance.__aenter__.return_value = mock_cli
    mock_client_instance.__aexit__.return_value = None
    mock_httpx_client_cls.return_value = mock_client_instance

    result = await OpenClawClient().send_watch_alert_to_router(
        message="m",
        market="kr",
        triggered=[{"symbol": "X", "condition_type": "price_below"}],
        as_of="2026-04-17T00:00:00Z",
        correlation_id="corr-prefer-router",
    )

    assert result.status == "success"
    assert mock_cli.post.call_args.args[0] == "http://127.0.0.1:9999/router/watch-alert"


@pytest.mark.asyncio
@patch("app.services.openclaw_client.httpx.AsyncClient")
async def test_send_watch_alert_to_router_falls_back_to_legacy_n8n_url(
    mock_httpx_client_cls: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "WATCH_ALERT_ROUTER_URL", "")
    monkeypatch.setattr(
        settings,
        "N8N_WATCH_ALERT_WEBHOOK_URL",
        "http://127.0.0.1:5678/webhook/watch-alert",
    )

    mock_cli = AsyncMock()
    mock_res = MagicMock(status_code=200)
    mock_res.raise_for_status.return_value = None
    mock_cli.post.return_value = mock_res
    mock_client_instance = AsyncMock()
    mock_client_instance.__aenter__.return_value = mock_cli
    mock_client_instance.__aexit__.return_value = None
    mock_httpx_client_cls.return_value = mock_client_instance

    result = await OpenClawClient().send_watch_alert_to_router(
        message="m",
        market="kr",
        triggered=[{"symbol": "X", "condition_type": "price_below"}],
        as_of="2026-04-17T00:00:00Z",
        correlation_id="corr-fallback",
    )

    assert result.status == "success"
    assert mock_cli.post.call_args.args[0] == "http://127.0.0.1:5678/webhook/watch-alert"


@pytest.mark.asyncio
async def test_send_watch_alert_to_router_skips_when_no_url_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "WATCH_ALERT_ROUTER_URL", "")
    monkeypatch.setattr(settings, "N8N_WATCH_ALERT_WEBHOOK_URL", "")

    result = await OpenClawClient().send_watch_alert_to_router(
        message="m",
        market="crypto",
        triggered=[{"symbol": "BTC", "condition_type": "price_above"}],
        as_of="2026-04-17T00:00:00Z",
        correlation_id="corr-skip",
    )

    assert result.status == "skipped"
    assert result.reason == "router_not_configured"
```

Also rename the three existing `test_send_watch_alert_to_n8n_*` tests to `test_send_watch_alert_to_router_*`, switch them to set `WATCH_ALERT_ROUTER_URL` (not `N8N_WATCH_ALERT_WEBHOOK_URL`), call `send_watch_alert_to_router`, and update the skipped reason assertion from `n8n_webhook_not_configured` to `router_not_configured`.

- [ ] **Step 4.2: Run and confirm failure**

Run: `uv run pytest tests/test_openclaw_client.py -v -k "watch_alert_to_router"`

Expected: every new test FAILs because `send_watch_alert_to_router` does not exist yet.

- [ ] **Step 4.3: Implement the rename**

In `app/services/openclaw_client.py`:

```python
def _resolve_watch_alert_url() -> str:
    """Resolve the watch-alert router URL.

    WATCH_ALERT_ROUTER_URL wins when set; otherwise falls through to the
    deprecated N8N_WATCH_ALERT_WEBHOOK_URL for backward compatibility.
    """
    router = settings.WATCH_ALERT_ROUTER_URL.strip()
    if router:
        return router
    return settings.N8N_WATCH_ALERT_WEBHOOK_URL.strip()
```

Rename `send_watch_alert_to_n8n` to `send_watch_alert_to_router`. Replace the body's URL load (`n8n_webhook_url = settings.N8N_WATCH_ALERT_WEBHOOK_URL.strip()`) with `router_url = _resolve_watch_alert_url()`. Replace the skipped-reason `"n8n_webhook_not_configured"` with `"router_not_configured"`. Replace all `"N8N watch alert ..."` log strings with `"Watch alert router ..."`.

- [ ] **Step 4.4: Update the only caller**

In `app/jobs/watch_scanner.py:_send_alert`, change `self._openclaw.send_watch_alert_to_n8n(...)` to `self._openclaw.send_watch_alert_to_router(...)`.

In `tests/test_watch_scanner.py`, rename `_FakeOpenClawClient.send_watch_alert_to_n8n` to `send_watch_alert_to_router`. Update any `monkeypatch.setattr(scanner._openclaw, "send_watch_alert_to_n8n", ...)` to use `send_watch_alert_to_router`. Update assertions that check `result["reason"] == "n8n_webhook_not_configured"` to `"router_not_configured"`.

- [ ] **Step 4.5: Run the full suite**

Run: `uv run pytest tests/test_openclaw_client.py tests/test_watch_scanner.py tests/test_watch_alerts.py tests/test_mcp_watch_alerts.py tests/test_watch_scan_tasks.py -v`

Expected: all green.

- [ ] **Step 4.6: Commit**

```bash
git add app/services/openclaw_client.py app/jobs/watch_scanner.py tests/test_openclaw_client.py tests/test_watch_scanner.py
git commit -m "$(cat <<'EOF'
refactor(ROB-122): rename send_watch_alert_to_n8n -> send_watch_alert_to_router

Transport-neutral seam. Resolves URL from WATCH_ALERT_ROUTER_URL first, then
N8N_WATCH_ALERT_WEBHOOK_URL for backward compatibility. Skipped reason renamed
to router_not_configured. Log strings updated.

Co-Authored-By: Paperclip <noreply@paperclip.ing>
EOF
)"
```

### Task 5: Final guardrails

- [ ] **Step 5.1: Verify nothing else imports the old name**

Run: `rg "send_watch_alert_to_n8n|n8n_webhook_not_configured" app tests`

Expected: zero hits. If there are stragglers, fix them and add to the previous commit (or amend if not yet pushed).

- [ ] **Step 5.2: Lint + typecheck**

Run: `make lint && make typecheck`

Expected: green.

- [ ] **Step 5.3: Full watch-related test suite**

Run: `uv run pytest tests/test_watch_scanner.py tests/test_openclaw_client.py tests/test_watch_alerts.py tests/test_mcp_watch_alerts.py tests/test_watch_scan_tasks.py tests/test_n8n_watch_alert_workflow.py -v`

Expected: all green. The n8n workflow test is unchanged in this PR and must still pass since the JSON contract `OpenClawClient` posts is identical.

- [ ] **Step 5.4: Push and open PR**

```bash
git push -u origin feature/ROB-122-watch-alert-router
gh pr create --base main --title "fix(ROB-122): resilient watch alerts + WATCH_ALERT_ROUTER_URL seam" --body "..."
```

---

## 7. Smoke / rollback / observability

### 7.1 Pre-merge smoke (local, no live order paths)

1. **Unit + integration tests** — `uv run pytest tests/test_watch_scanner.py tests/test_openclaw_client.py tests/test_watch_alerts.py tests/test_mcp_watch_alerts.py tests/test_watch_scan_tasks.py tests/test_n8n_watch_alert_workflow.py -v`. Expect all green.
2. **Settings load** — `uv run python -c "from app.core.config import settings; assert hasattr(settings, 'WATCH_ALERT_ROUTER_URL'); print('OK')"`. Expect `OK`. Do not print any actual URL values.
3. **Manual scan dry-run** (operator only, in a non-prod shell) — temporarily set `WATCH_ALERT_ROUTER_URL` to a local httpbin (`http://127.0.0.1:8080/post`) and run `uv run python -c "import asyncio; from app.jobs.watch_scanner import WatchScanner; print(asyncio.run(WatchScanner().run()))"` against a Redis with a single dummy watch. Confirm the request reaches httpbin with the expected JSON contract. Confirm crypto/kr/us all return dicts with `failed_lookups` keys. Tear down the test watch via the manage_watch_alerts MCP tool. **Do not** test against production Redis or production Discord.

### 7.2 Post-merge smoke (operator)

1. Deploy with `WATCH_ALERT_ROUTER_URL` empty so the deprecated `N8N_WATCH_ALERT_WEBHOOK_URL` fallback path is exercised. Confirm the next 5-min `scan.watch_alerts` cron tick logs `"Watch alert router send start"` (new log line) and that Discord still receives alerts unchanged.
2. After the second successful tick, confirm Sentry / log dashboard for any `failed_lookups` warnings — if a US Yahoo failure occurs, the warning must appear without `scan_market` aborting.

### 7.3 Rollback

Pure code rollback: `git revert <merge-sha>`. No DB migration, no schema change, no infra change — single revert is sufficient.

Operationally, rollback at the env-var layer is also free: if a future Phase 1 cutover misbehaves, the operator simply unsets `WATCH_ALERT_ROUTER_URL` and the code falls back to `N8N_WATCH_ALERT_WEBHOOK_URL` automatically on the next scan — no redeploy required.

### 7.4 Observability hooks (already covered, no new code in Phase 0)

- `logger.warning("Watch lookup failed (continuing): market=%s symbol=%s error=%s", ...)` — emitted per failed lookup. Surfaces in existing log infra and Sentry breadcrumbs.
- `logger.info("Watch alert router send start: ... attempt=%s", ...)` — emitted on each delivery attempt.
- `logger.info("Watch alert router sent: ... status=%s", ...)` — emitted on success.
- `logger.error("Watch alert router failed after retries: ...", ...)` — emitted on retry exhaustion.
- Per-market result dicts now include `failed_lookups` integer; surface this in Phase 1 dashboards.
- Existing Sentry config (`SENTRY_DSN`, `SENTRY_ENVIRONMENT`) is unchanged — uncaught exceptions in `run()` are now caught and converted to a per-market failure dict, but the underlying log line is still ERROR-level.

---

## 8. Acceptance criteria

A reviewer should be able to confirm all of the following before approving:

1. `tests/test_watch_scanner.py::test_scan_market_us_yahoo_failure_does_not_abort_other_watches` exists, passes, and asserts that **two watches** are evaluated even when the first raises.
2. `tests/test_watch_scanner.py::test_run_continues_other_markets_when_scan_market_raises` exists and passes.
3. `WatchScanner.scan_market` returns a dict that always contains a `failed_lookups: int` key, and `WatchScanner.run` returns `{"crypto": ..., "kr": ..., "us": ...}` even when individual markets fail.
4. `app/core/config.py` defines `WATCH_ALERT_ROUTER_URL: str = ""` and keeps `N8N_WATCH_ALERT_WEBHOOK_URL: str = ""`.
5. `OpenClawClient.send_watch_alert_to_router` exists; `send_watch_alert_to_n8n` does **not**. URL resolution prefers `WATCH_ALERT_ROUTER_URL`.
6. `tests/test_openclaw_client.py` includes `test_send_watch_alert_to_router_prefers_router_url_over_legacy`, `test_send_watch_alert_to_router_falls_back_to_legacy_n8n_url`, and `test_send_watch_alert_to_router_skips_when_no_url_configured`, all passing.
7. `rg "send_watch_alert_to_n8n|n8n_webhook_not_configured" app tests` returns zero results.
8. `tests/test_n8n_watch_alert_workflow.py` is unchanged and still passes (proves the JSON contract on the wire is byte-compatible).
9. `app/tasks/watch_scan_tasks.py` is unchanged (no scheduler change).
10. `make lint && make typecheck && uv run pytest tests/test_watch_scanner.py tests/test_openclaw_client.py tests/test_watch_alerts.py tests/test_mcp_watch_alerts.py tests/test_watch_scan_tasks.py tests/test_n8n_watch_alert_workflow.py` all green.
11. PR description explicitly calls out non-goals (no Prefect changes in this PR; no order side effects; no scheduler change).

---

## Self-review notes

- **Spec coverage:** every numbered item in the planner prompt has a section above. (1) §1, (2) §2, (3) §3, (4) §4, (5) §5, (6) §6, (7) §7, (8) this file is at the requested path.
- **Placeholders:** none — every step has either exact code, an exact command, or an exact file diff target.
- **Type consistency:** `failed_lookups` is `int` everywhere it appears; `WatchAlertDeliveryResult` field names are unchanged; the renamed method keeps the same kwarg signature as the original (`message`, `market`, `triggered`, `as_of`, `correlation_id`, `intents`).
- **Hard stops verified:** no live/paper/mock order calls anywhere in the diff; no schema migrations; no scheduler change; n8n workflow JSON untouched; all test URLs are loopback placeholders.
