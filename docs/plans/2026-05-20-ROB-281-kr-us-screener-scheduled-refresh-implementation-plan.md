# ROB-281 — KR/US `/invest/screener` Scheduled Refresh Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add scheduled TaskIQ refreshes + market-aware freshness labeling + commit-time guardrails to `invest_screener_snapshots` so KR and US screeners do not drift stale silently. KR-first then US-second within this PR (or two sub-PRs of the same branch if size warrants).

**Architecture:** Extend the existing `app/tasks/invest_screener_snapshot_tasks.py` (currently dry-run-only, no schedule) with TaskIQ `LabelScheduleSource` cron labels per session slot. Extend `app/services/invest_screener_snapshots/freshness.py` with session-aware slot classification reusing `today_trading_date` + `classify_state` from ROB-277. Add a guard layer (dominant-partition, min-row, suspicious-distribution) in `app/jobs/invest_screener_snapshots.py`. Wire failure/suspicious-distribution-only alerts to `discord_webhook_alerts`. Surface new UI label tokens (`KRX preliminary`, `NXT final`, `US post-close`) in `app/services/invest_view_model/screener_service.py` without regressing ROB-277 served-time vs data-as-of split.

**Tech Stack:** TaskIQ (broker: `app/core/taskiq_broker.py`, scheduler: `app/core/scheduler.py` with `LabelScheduleSource`). `exchange-calendars>=4.7,<5.0` (already a dep) for KR `XKRX` and US `XNYS` session/holiday/half-day handling. Async SQLAlchemy, pytest + `pytest-asyncio`, UV-managed deps.

**Parent / sibling issues:**

- Parent policy: ROB-280
- Sibling (crypto 24/7): ROB-282 — **out of scope here**
- Sibling (investor_flow recurring): ROB-205 — **out of scope here**, dependency reference only
- Predecessor (served-time vs data-as-of split): ROB-277 — **preserve, do not regress**

---

## Locked Decisions (do not re-debate during implementation)

These were agreed in the ROB-280 review thread on 2026-05-20 and locked here per ROB-281 acceptance criteria. Any deviation requires explicit reviewer sign-off **before** code changes.

### D1. KR pre-market repair time: `07:40 KST`

**Decision:** Schedule the KR pre-market catch-up/repair at `07:40 KST` on KR trading days (Mon–Fri minus XKRX holidays).

**Rationale:**

- NXT pre-market opens at `08:00 KST`. `07:40 KST` gives a **20-minute buffer** before NXT pre-market activity could interfere with the repair run.
- Purpose is **repair of the prior day's `20:20 KST` NXT-final run** (e.g. provider outage, partial data, schedule miss), not preparation of new same-day data — same-day data is fetched at the `16:20` / `20:20` slots.
- `07:50 KST` was considered. Rejected as starting point because the 10-min buffer to NXT 08:00 is too tight for retry/backoff on transient failures.
- If `07:40 KST` later proves to be **before** upstream overnight data is reliably available (provider lag), shift to `07:50 KST` is a one-line cron change in a follow-up; the slot's purpose and label do not change.

**Recorded in code/runbook:**

- `app/tasks/invest_screener_snapshot_tasks.py` — docstring on the KR pre-market task explicitly states "repair of prior day's `20:20 KST` NXT-final, NOT same-day data prep".
- Runbook update (TBD path) — note `07:40 KST` choice + escalation path to `07:50 KST` if upstream lag observed.

### D2. US trading calendar source: `exchange-calendars` (`XNYS` + `XKRX`)

**Decision:** Use `exchange-calendars` (already a project dependency at `>=4.7,<5.0`) for both US (`XNYS`) and KR (`XKRX`) holiday and half-day handling. Do **not** introduce `pandas-market-calendars`.

**Rationale:**

- Already a direct dependency in `pyproject.toml` and used in 8+ files including `app/jobs/watch_market_data.py`, `app/services/us_candles_sync_service.py`, `app/services/kr_candles_sync_service.py`, `app/services/kis_ohlcv_cache.py`, `app/services/yahoo_ohlcv_cache.py`, `app/mcp_server/tooling/market_data_indicators.py`. Convention is firmly established.
- `pandas-market-calendars` would add a parallel calendar source with overlapping responsibility — wrong direction.
- Holiday + half-day data quality on `XNYS` in `exchange-calendars` is sufficient for our scheduling needs (skip on holiday, allow normal 17:20 ET on half-days since half-day closes at 13:00 ET — 17:20 ET is post-close regardless).

**Behavior:**

- At task entry, the KR-side tasks check `xcals.get_calendar("XKRX").is_session(today_kst)`. Holiday → fail-closed early-return + alert (suspicious-distribution category? No — holiday is expected. Just info-log, no alert.).
- The US-side task checks `xcals.get_calendar("XNYS").is_session(today_ny)`. Holiday → early-return.
- Half-day: US post-close `17:20 America/New_York` still runs (market closes 13:00 ET on half-days, so 17:20 ET is well after close). No special-case code needed.

### D3. Operational alert channel: `discord_webhook_alerts`

**Decision:** Route failure / suspicious-distribution / schedule-miss alerts to `settings.discord_webhook_alerts` (existing config in `app/core/config.py:239–242`). Do **not** route through Hermes.

**Rationale:**

- Hermes is a **product-level review-trigger contract** (`HERMES_WEBHOOK_URL = http://localhost:18790/hooks/review-trigger`, config in `app/core/config.py:370–376`). Sending ops alerts through Hermes would muddy that contract.
- `discord_webhook_alerts` already exists alongside `discord_webhook_us`, `discord_webhook_kr`, `discord_webhook_crypto`, configured in `app/main.py:260–263` and lifecycle-managed there.
- An existing Discord helper (`send_discord_content_single` / `send_discord_embed_single` in `app/monitoring/trade_notifier/transports.py`) already abstracts the HTTP call. Reuse, do not re-implement.

**Behavior:**

- Success: **no alert** (acceptance: success-only spam forbidden).
- Failure or suspicious-distribution / guard rejection or schedule miss: **single Discord embed** to `discord_webhook_alerts` with: slot name (`kr_pre_market_repair` etc.), market, exception class + message (truncated), snapshot_date_distribution (if available), commit status (`skipped`/`failed`).
- If `discord_webhook_alerts` is unset (`None`), the alert call is a no-op (info-log only). Tests cover both branches.

---

## Hard Boundaries (preserve throughout implementation)

These come from ROB-281 issue body and the user's confirmation. **Do not relax** without explicit reviewer sign-off.

- No broker / order / watch / order-intent mutations.
- No Toss / Naver browser scraping as a production source.
- No buy / sell recommendation logic changes.
- No `investor_flow_snapshots` recurring / backfill / scheduler / partition repair changes — that is **ROB-205 scope**. ROB-281 only references investor_flow as a read-only freshness dependency.
- No crypto recurring refresh — that is **ROB-282 scope**.
- Preserve **ROB-277** served-time vs data-as-of semantics. `방금 갱신` is the **served** label only; snapshot data 기준일 surfaces via `primary.asOfLabel` + the new session label tokens.

---

## Implementation Stages

Stages 1–2 can be developed in parallel (both extend `freshness.py` but in distinct functions). Stages 3–4 are sequential (KR-first → US-second) per the user-confirmed order. Stage 5 is independent and can land alongside any prior stage. Stages 6–7 land after 3–5. Stages 8–9 close out.

### Stage 1 — KR session-aware freshness extension

File: `app/services/invest_screener_snapshots/freshness.py`

- [ ] Add type alias `KRSessionSlot = Literal["pre_market_repair", "krx_preliminary", "nxt_final"]`.
- [ ] Add `classify_kr_session_slot(now_kst: datetime) -> KRSessionSlot` — returns which schedule slot most recently fired at-or-before `now_kst` on the current KR trading day, with fallback to the prior trading day's `nxt_final` for early-morning hours (before `07:40`).
- [ ] Add `kr_session_slot_to_label_token(slot) -> str | None` — `"krx_preliminary" → "KRX preliminary"`, `"nxt_final" → "NXT final"`, `"pre_market_repair" → None` (repair has no user-facing label; surfaced as the prior NXT-final label).
- [ ] Unit tests: `tests/services/invest_screener_snapshots/test_freshness_kr_session.py` — table-driven assertions for boundary times `07:39`, `07:40`, `16:19`, `16:20`, `20:19`, `20:20`, `23:59`, `00:10`, KR holiday + weekend behavior.

### Stage 2 — US session-aware freshness extension

File: `app/services/invest_screener_snapshots/freshness.py`

- [ ] Add `last_completed_us_session_close(now: datetime) -> datetime | None` — uses `xcals.get_calendar("XNYS")` to find the most recent session close at-or-before `now`. Returns `None` if no recent session (extremely defensive — should never happen in practice).
- [ ] Add `is_us_post_close_window(now: datetime) -> bool` — `True` when `now` is after that session's close, i.e. the `US post-close` slot label applies.
- [ ] Add `us_session_label_token(now: datetime) -> str` — `"US post-close"` when in post-close window, else absent (fallback to prior session's post-close label).
- [ ] Unit tests: `tests/services/invest_screener_snapshots/test_freshness_us_session.py` — Mon 17:20 ET, Fri 17:20 ET, holiday (Thanksgiving) skip, half-day (Black Friday 13:00 ET close, 17:20 ET still post-close), DST spring-forward boundary (verify `America/New_York` resolves correctly, no UTC ambiguity).

### Stage 3 — KR TaskIQ scheduled wrappers

File: `app/tasks/invest_screener_snapshot_tasks.py`

- [ ] Keep existing `build_invest_screener_snapshots` task unchanged (it's the manual / dry-run entry point).
- [ ] Add `scheduled_build_invest_screener_snapshots_kr_pre_market_repair` (cron label: `40 7 * * 1-5` in `Asia/Seoul`).
- [ ] Add `scheduled_build_invest_screener_snapshots_kr_krx_preliminary` (cron label: `20 16 * * 1-5` in `Asia/Seoul`).
- [ ] Add `scheduled_build_invest_screener_snapshots_kr_nxt_final` (cron label: `20 20 * * 1-5` in `Asia/Seoul`).
- [ ] Each wrapper:
  1. Checks `XKRX.is_session(today_kst)`. If holiday → info-log + return early (no alert).
  2. Checks `settings.INVEST_SCREENER_SCHEDULE_ENABLED` env gate (NEW config flag, default `False` → dry-run only).
  3. Calls `build_invest_screener_snapshots(market="kr", all_symbols=True, commit=settings.INVEST_SCREENER_SCHEDULE_ENABLED)`.
  4. Passes a `slot: KRSessionSlot` tag through into the build result for observability.
  5. On exception → send Discord alert (Stage 6) → re-raise so TaskIQ records failure.
- [ ] Docstring on the `pre_market_repair` task explicitly states the D1 decision (repair of prior `20:20 KST` run, not same-day prep).
- [ ] Verify TaskIQ `LabelScheduleSource` honors `tz` keyword in cron labels; if not, fall back to UTC cron with KST offset and document the conversion in the docstring.

### Stage 4 — US TaskIQ scheduled wrapper

File: `app/tasks/invest_screener_snapshot_tasks.py`

- [ ] Add `scheduled_build_invest_screener_snapshots_us_post_close` (cron label: `20 17 * * 1-5` in `America/New_York`).
- [ ] Wrapper:
  1. Checks `XNYS.is_session(today_ny)`. Holiday → info-log + return.
  2. Env gate (same `INVEST_SCREENER_SCHEDULE_ENABLED`).
  3. `build_invest_screener_snapshots(market="us", all_symbols=True, common_stocks_only=True, commit=...)`.
  4. On exception → Stage 6 Discord alert.
- [ ] Half-day handling: no special case needed (verified in D2 rationale).
- [ ] Docstring notes the `America/New_York` timezone choice (D2) and the DST safety this provides.

### Stage 5 — Commit-time guards

File: `app/jobs/invest_screener_snapshots.py` (extend `run_snapshot_build` or add a guard layer before commit)

- [ ] Add `_assert_dominant_partition(distribution: Mapping[date, int], threshold: float = 0.70) -> date` — raises `SuspiciousDistributionError` if no single `snapshot_date` holds ≥ 70% of rows. Returns the dominant date on success.
- [ ] Add `_assert_min_row_count(count: int, market: Literal["kr", "us"]) -> None` — KR threshold `2500` (observed: 3867), US threshold `3500` (observed: 5116). Raises `InsufficientRowsError` if violated. **Threshold values locked here; if scrutinized in review, adjust before merge — do not silently soften during implementation.**
- [ ] Both guards run **after** the dry-run pass produces `snapshot_date_distribution`, **before** any commit. On `commit=False` (manual dry-run), guards log + report but do **not** raise (so operators can still see dry-run output on a degraded day).
- [ ] Guards on scheduled (`commit=True`) path: raise → caller catches → Discord alert → no commit.
- [ ] Tests: `tests/jobs/test_invest_screener_snapshots_guards.py` — dominant-partition pass/fail, min-row pass/fail, dry-run-vs-commit different behaviors.

### Stage 6 — Discord ops alert path

New file: `app/services/invest_screener_snapshots/alerts.py`

- [ ] `async def send_screener_refresh_alert(slot: str, market: str, exception: BaseException | None, distribution: Mapping[date, int] | None, *, settings) -> None` — formats Discord embed and posts to `settings.discord_webhook_alerts` via existing `send_discord_embed_single`. No-op when webhook unconfigured.
- [ ] Embed fields: `slot`, `market`, `error_class`, `error_message` (truncated to 1024 chars), `snapshot_date_distribution` (top 3 dates by row count), `commit_status` (`skipped` / `failed`).
- [ ] **Never call on success** — function contract documented + tested.
- [ ] Tests: `tests/services/invest_screener_snapshots/test_alerts.py` — unconfigured webhook → no-op (asserted via mock); configured + exception → expected payload format; success path must never invoke (asserted via grep + integration test).

### Stage 7 — UI label tokens in view-model

File: `app/services/invest_view_model/screener_service.py`

- [ ] When constructing `primary.asOfLabel` for snapshot-backed screener results, append the session label token (`KRX preliminary` / `NXT final` / `US post-close`) derived from `partition_computed_at` via Stage 1 / Stage 2 classifiers.
- [ ] Token appears in `asOfLabel` (existing field) without breaking ROB-277 contract — do not introduce a new top-level field unless needed. Format example: `"2026.05.20 16:20 KST 기준 (KRX preliminary)"`.
- [ ] If `partition_computed_at` is `None` or session classification returns no token (pre_market_repair, early morning fallback), omit the parenthetical and keep prior ROB-277 behavior.
- [ ] Frontend smoke: visit `/invest/screener?market=kr` and `?market=us`, verify label appears once per snapshot-backed result and disappears on stale/missing dataState.
- [ ] Tests: extend `tests/services/invest_view_model/test_screener_service.py` (or matching test file) — label token presence per slot, ROB-277 regression check (`servedAt` / `servedRelativeLabel` unchanged).

### Stage 8 — Test & smoke completeness

- [ ] Unit tests: Stages 1, 2, 5, 6, 7 each have dedicated test modules per above.
- [ ] Integration test: `tests/tasks/test_invest_screener_snapshot_tasks_scheduled.py` — dry-run path (`INVEST_SCREENER_SCHEDULE_ENABLED=False`) yields expected dry-run result; commit path (env var True) hits the guard layer; holiday path returns early.
- [ ] Read-only smoke commands documented in plan footer (see Production rollout below).
- [ ] `make test` passes locally. Skip slow / integration markers per project conventions if necessary.

### Stage 9 — Production rollout

Strictly dry-run-first.

- [ ] **Step A (code merge):** PR merges with `INVEST_SCREENER_SCHEDULE_ENABLED=False` default → scheduled tasks run on cron but only emit dry-run output. Discord alerts only on actual failure / suspicious distribution.
- [ ] **Step B (dry-run evidence):** Capture 2 KR trading days + 1 US trading day of dry-run logs. Confirm: schedule fires at expected wall-clock times (KST + ET), dominant-partition + min-row guards pass, snapshot_date_distribution looks sane, no Discord alerts spammed.
- [ ] **Step C (alert smoke):** Force one intentional failure (e.g. set min-row threshold absurdly high in a feature-flagged test env) → verify Discord alert lands in `discord_webhook_alerts` exactly once.
- [ ] **Step D (commit enable):** Set `INVEST_SCREENER_SCHEDULE_ENABLED=True` in production env. Monitor first 24h KR cycle + first 1 US cycle for unexpected guard failures. Read-only smoke:
  - `curl -sS "$BASE/trading/api/invest/screener/results?market=kr&preset=consecutive_gainers" | jq '.freshness'`
  - `curl -sS "$BASE/trading/api/invest/screener/results?market=us&preset=consecutive_gainers" | jq '.freshness'`
  - `curl -sS "$BASE/trading/api/invest/screener/presets" | jq '.'`
  - Verify `primary.asOfLabel` contains the expected session token, `servedRelativeLabel` shows fresh served-time (ROB-277 preserved).
- [ ] **Step E (rollback procedure):** If a guard triggers a sustained alert storm, flip `INVEST_SCREENER_SCHEDULE_ENABLED=False` (reverts to dry-run only, no DB writes). No code revert needed.

---

## Out of Scope (cross-references)

- **Crypto 24/7 refresh** — ROB-282. Do not touch `app/jobs/invest_crypto_screener_snapshots.py`, `app/services/invest_crypto_screener_snapshots/freshness.py`, or the crypto label `crypto computed_at 기준`.
- **investor_flow recurring / backfill / scheduler / partition repair** — ROB-205. Read-only reference only. Do not modify `app/tasks/investor_flow_snapshot_tasks.py` or `app/jobs/investor_flow_snapshots.py`.
- **ROB-277 served-time vs data-as-of split** — preserve unchanged. New session label tokens append to `asOfLabel`; `servedAt` / `servedRelativeLabel` untouched.

---

## Open Follow-ups (not in this PR)

- `07:40 KST` vs `07:50 KST` empirical re-evaluation after first 2 weeks of production data. Document in runbook escalation section.
- Half-day post-close behavior for US: if early-close (13:00 ET) ever causes data-quality issues at 17:20 ET, consider adding a `13:30 ET` half-day variant slot. Currently expected unnecessary.
- Min-row guard thresholds (`2500` KR, `3500` US) may need tuning after first month. Track row-count distribution and revise via separate PR.
- Long-term: split `app/tasks/invest_screener_snapshot_tasks.py` if it grows beyond ~300 LOC into `manual.py` + `scheduled.py`. Not needed now.

---

## File Change Inventory (estimate)

**Modified:**

- `app/services/invest_screener_snapshots/freshness.py` — KR/US session classifiers + label tokens.
- `app/tasks/invest_screener_snapshot_tasks.py` — 4 new scheduled task wrappers.
- `app/jobs/invest_screener_snapshots.py` — 2 commit-time guards + threshold constants.
- `app/services/invest_view_model/screener_service.py` — session label token in `asOfLabel`.
- `app/core/config.py` — `INVEST_SCREENER_SCHEDULE_ENABLED` env flag (default `False`).

**Added:**

- `app/services/invest_screener_snapshots/alerts.py` — Discord webhook helper.
- `tests/services/invest_screener_snapshots/test_freshness_kr_session.py`
- `tests/services/invest_screener_snapshots/test_freshness_us_session.py`
- `tests/services/invest_screener_snapshots/test_alerts.py`
- `tests/jobs/test_invest_screener_snapshots_guards.py`
- `tests/tasks/test_invest_screener_snapshot_tasks_scheduled.py`

**Read-only references (do not modify in this PR):**

- `app/tasks/investor_flow_snapshot_tasks.py`, `app/jobs/investor_flow_snapshots.py` — ROB-205 scope.
- `app/models/invest_screener_snapshot.py` — schema unchanged; ROB-204 activated.

---

## Estimated effort

Stages 1–2: ~0.5 day (freshness extension, table-driven tests). Stages 3–4: ~1 day (TaskIQ wrappers + cron + env gate). Stage 5: ~0.5 day (guards + threshold validation). Stage 6: ~0.5 day (Discord helper + unit tests). Stage 7: ~0.5 day (label token in view-model + frontend smoke). Stages 8–9: ~1 day (test consolidation + dry-run rollout).

Total: **~4 day** code + **~2 trading day** dry-run observation before enabling commit.
