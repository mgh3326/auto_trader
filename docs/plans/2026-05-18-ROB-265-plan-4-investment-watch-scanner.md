# ROB-265 Plan 4 — Investment watch scanner re-wire + Hermes review-trigger notifications

> Stacked on Plan 3 (PR #869). Plan 1–3 schema/services/MCP are unchanged; this plan adds the new scanner and the Hermes notification client.

**Goal:** Stand up the new investment-watch scanner that reads DB-backed `investment_watch_alerts`, writes `investment_watch_events` with the immutable trigger-identity snapshot, and emits Hermes review-trigger notifications. The legacy `WatchScanner` is left in place for the cron-replaced scheduler hand-off; Plan 5 deletes it along with the rest of the OpenClaw / watch-order-intent-ledger surface.

**Architecture:**
- `app/services/hermes_client.py` — `HermesNotificationClient` plus a `ReviewTriggerPayload` Pydantic schema. POSTs to `settings.HERMES_WEBHOOK_URL` with an Authorization Bearer token. **No new OpenClaw API surface is introduced.**
- `app/jobs/watch_market_data.py` — stateless async helpers extracted from the value-fetch logic the legacy scanner uses (price / RSI / trade-value / index / FX, market-open check, target_kind × metric dispatcher). Same code, callable from both scanners without subclassing or coupling to OpenClawClient.
- `app/jobs/investment_watch_scanner.py` — `InvestmentWatchScanner` class. For each `market`:
  1. Skip closed markets (except `fx`).
  2. Read active alerts via `InvestmentReportsRepository.list_active_alerts(market=, valid_at=now)`.
  3. For each alert: fetch the current value, evaluate the operator/threshold, skip if not triggered.
  4. Compose the event idempotency key as `event:{alert_uuid}:{kst_date}:{threshold_key}`. Re-firing the same threshold on the same KST day is a no-op (dedup via the unique index on `idempotency_key`).
  5. Outcome depends on `action_mode`: `notify_only` → `notified`; `approval_required` → `review_required`; `preview_only` → `preview_attached`.
  6. Insert one `InvestmentWatchEvent` carrying the **full immutable trigger snapshot** (market, target_kind, symbol, metric, operator, threshold, threshold_key, intent, action_mode, current_value, scanner_snapshot, outcome, correlation_id, kst_date) plus the source_report_uuid / source_item_uuid linkage.
  7. Flip the alert to `status='triggered'` (one-shot — operator creates a new alert to re-arm; matches the locked "watches are review triggers, not automatic instructions" semantics).
  8. Send a Hermes review-trigger notification carrying the event payload.
- `app/tasks/watch_scan_tasks.py` — the existing `scan.watch_alerts` cron task is left untouched (legacy will be dropped in Plan 5). A new task `scan.investment_watch_alerts` (same 5-minute cron, KST) drives `InvestmentWatchScanner.run()`. Running both during the transition is safe because their data sources (legacy Redis vs DB-backed alerts) don't overlap.

**Tech stack:** httpx async (matches OpenClawClient pattern), Pydantic v2, pytest-asyncio. PostgreSQL `test_db` via the shared `_investment_reports_helpers` plugin.

---

## Locked requirements (from Plan 4 review)

1. Scanner reads DB-backed `investment_watch_alerts`, **not** legacy Redis watch alerts.
2. Scanner writes `investment_watch_events` with the **immutable trigger identity snapshot** the Plan 1 patch enforced.
3. Scanner emits **Hermes** notification/review-trigger payloads — not OpenClaw payloads.
4. The notification payload includes: `source_report_uuid`, `source_item_uuid`, `alert_uuid`, `event_uuid`, `correlation_id`, the trigger snapshot, `scanner_snapshot`, `outcome`, `action_mode`. `correlation_id` semantics are preserved across event and notification.
5. Watch remains a **review trigger**, never an automatic order instruction.
6. No broker / live order mutation from the scanner path.
7. **No new OpenClaw contract names or new OpenClaw-facing APIs** are introduced in this plan.
8. The legacy `OpenClawClient` / `WatchScanner` / `WatchOrderIntentService` / `watch_order_intent_ledger` references in the codebase are treated as **legacy / deferred-migration surface**, not extended.

---

## File Structure

**Create:**
- `app/services/hermes_client.py` — `HermesNotificationClient` + `ReviewTriggerPayload` Pydantic schema.
- `app/jobs/watch_market_data.py` — stateless helpers: `is_market_open`, `get_current_value`, plus the internal `get_price` / `get_rsi` / `get_trade_value` / `get_index_price` / `get_fx_price` / `normalize_crypto_symbol`. Sourced from the legacy scanner's existing logic, refactored to module functions.
- `app/jobs/investment_watch_scanner.py` — `InvestmentWatchScanner` class (`scan_market`, `run`).
- `tests/test_hermes_client.py` — payload schema + delivery happy-path / failure tests with `httpx` MockTransport.
- `tests/test_investment_watch_scanner.py` — end-to-end scanner tests with a stub Hermes client and the shared `session` fixture.

**Modify:**
- `app/core/config.py` — add `HERMES_WEBHOOK_URL`, `HERMES_TOKEN`, `HERMES_ENABLED` settings.
- `app/tasks/watch_scan_tasks.py` — add a `scan.investment_watch_alerts` task next to the legacy one (no replacement yet).

---

## Tasks

### Task 1 — Settings + Hermes client + payload schema
- Add `HERMES_WEBHOOK_URL` (default local stub), `HERMES_TOKEN` (default empty), `HERMES_ENABLED` (default `False`) to `Settings`.
- `app/services/hermes_client.py`:
  - `ReviewTriggerPayload` Pydantic v2 model with the required fields.
  - `HermesNotificationClient`:
    - `__init__(webhook_url=None, token=None, enabled=None)` reading from `settings` by default.
    - `async send_review_trigger(payload: ReviewTriggerPayload) -> HermesDeliveryResult` — POSTs with Bearer auth, 10 s timeout, returns delivery status (matches the `WatchAlertDeliveryResult` shape concept; new class, new naming).
    - If `HERMES_ENABLED` is `False`, the client logs and returns `status="skipped"` without making the request. Useful for tests and dev.
- `tests/test_hermes_client.py` — exercise: payload validation, disabled path, success path (httpx MockTransport returning 200), failure path (500 / network error).

### Task 2 — Stateless market-data helpers
- `app/jobs/watch_market_data.py` re-exposes the legacy scanner's value-fetch logic as plain module functions:
  - `is_market_open(market: str) -> bool`
  - `normalize_crypto_symbol(symbol: str) -> str`
  - `get_price(symbol: str, market: str) -> float | None`
  - `get_trade_value(symbol: str, market: str) -> float | None`
  - `get_index_price(symbol: str, market: str) -> float | None`
  - `get_fx_price(symbol: str) -> float | None`
  - `get_rsi(symbol: str, market: str) -> float | None`
  - `get_current_value(target_kind: str, metric: str, symbol: str, market: str) -> float | None`
- The legacy `WatchScanner` is **not** modified — these are duplicated logic that takes over the new scanner; Plan 5 deletes the legacy scanner and the duplication goes away.
- No new tests at this layer — covered transitively by the scanner tests.

### Task 3 — InvestmentWatchScanner
- `app/jobs/investment_watch_scanner.py`:
  - `__init__(hermes_client=None, repository_factory=None)` — injectable for tests.
  - `async scan_market(market: str) -> dict` — per-market scan as described in the architecture section above.
  - `async run() -> dict[str, dict]` — iterates `("crypto", "kr", "us")` and aggregates.
  - `async close()` — closes the Hermes client.
- Idempotency: relies on the unique index on `investment_watch_events.idempotency_key` (Plan 1 schema). A re-fire on the same KST day for the same threshold is a no-op (the DB raises `IntegrityError` and the scanner records `outcome="ignored"` for that loop iteration).
- Status transition: alert moves to `triggered` on first fire. Plan 4 does not implement re-arm — operator drives that via a fresh activate-watch MCP call.
- Notification: payload built from the event row + alert linkage. Sent via `HermesNotificationClient`. Failed delivery is logged and the event row is preserved.

### Task 4 — Tests
- `tests/test_investment_watch_scanner.py`:
  - Stub `HermesNotificationClient` that records `send_review_trigger` calls.
  - Stub market-data layer via `monkeypatch` on `watch_market_data.get_current_value` / `is_market_open`.
  - Seed an `investment_watch_alert` row through the Plan 2 services.
  - Tests: not triggered (no event written, no Hermes call), triggered notify_only (event with `outcome="notified"` + Hermes called once + alert status → `triggered`), triggered approval_required (event `outcome="review_required"`), re-fire on same day is idempotent (one event row, no second Hermes call), Hermes delivery failure does not roll back the event, market closed skips scan.
- `tests/test_hermes_client.py` — see Task 1.

### Task 5 — Scheduler wiring
- `app/tasks/watch_scan_tasks.py` — add the new task:

```python
@broker.task(
    task_name="scan.investment_watch_alerts",
    schedule=[{"cron": "*/5 * * * *", "cron_offset": "Asia/Seoul"}],
)
async def run_investment_watch_scan_task() -> dict:
    scanner = InvestmentWatchScanner()
    try:
        return await scanner.run()
    finally:
        await scanner.close()
```

Legacy task is **not** removed in Plan 4 (Plan 5).

### Task 6 — Final lint / typecheck / PR
- `ruff format` + `ruff check` clean on new + modified files.
- `ty check` clean on new modules.
- Full P1+P2+P3+P4 test sweep + legacy guard.
- Grep `app/jobs/investment_watch_scanner.py app/services/hermes_client.py` for `OpenClaw` / `WatchOrderIntentService` / `watch_order_intent_ledger` — expected: zero hits.
- Push `rob-265-plan-4`, open PR with base `rob-265-plan-3`.

---

## Out of scope (Plans 5+)

- Removing legacy `WatchScanner`, `WatchOrderIntentService`, `watch_order_intent_ledger`, the legacy `scan.watch_alerts` task, and the OpenClaw notification surface — Plan 5.
- Frontend `/invest/reports` UI + NXT advisory-only pilot — Plan 5.
- A Hermes inbound callback (operator decision → auto_trader) — out of scope. The MCP `investment_report_decide_item` tool from Plan 3 already covers the operator-decision write path.
- Adapting `watch_proximity_monitor` to read the new alert source — out of scope; deferred to Plan 5 with the rest of the legacy cleanup (proximity monitor still works against the legacy Redis-based alert source).
