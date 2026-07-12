# Changelog

## [Unreleased]

### Added (ROB-838 — immutable analysis snapshot bundles; migration 0)
- **Re-analysis sessions can consume one fixed, write-once input bundle.** `analysis_bundle_create` captures holdings, cash, quotes, order books, indicators, support/resistance, flow, decision history, and gate inputs once through read-only service boundaries; each section retains its collection source, as-of timestamp, completion timestamp, and explicit unavailable error instead of being repaired on read.
- **Bundle reads are DB-only and integrity checked.** `analysis_bundle_get` verifies the stored `content_hash`, returns the exact persisted document without provider calls or recomputation, and optionally projects `sections=[...]` from those stored values. Freshness is response metadata (`created_at`, per-section as-of/age/stale state), so old or incomplete evidence cannot masquerade as current data.
- **The new MCP surface is isolated default-off.** `ANALYSIS_SNAPSHOT_BUNDLES_MCP_ENABLED=false` hides creation and retrieval until explicitly enabled; the read-only profile exposes retrieval only. ROB-833 can reuse the contract as `watch/fill event → bundle creation → same bundle ID passed to every claude -p session`, keeping model comparisons on identical inputs.

### Added (ROB-832 — order proposal replace/cancel actions; migration 1)
- **Operators can approve one-order replace or cancel proposals in Telegram.** Replace proposals bind one target broker-order snapshot to one new-order specification; cancel proposals bind one target snapshot to one cancel action. Existing place proposals keep multi-rung behavior and legacy `NULL` actions behave as `place`.
- **Replace is fail-closed across cancel-and-new.** Approval re-fetches broker evidence, reruns preview/profit guards, confirms the old order cancellation, then submits the replacement. Any target drift, cancellation failure, missing confirmation, or ambiguous broker response prevents a new order and preserves reconciliation lineage.
- **Manual broker orders are valid targets.** Proposal creation verifies the unattributed order and remaining quantity directly with Upbit or KIS, rejects unsupported account/market/action combinations, and serializes concurrent proposals that target the same broker order without blocking independent ladder orders.
- **Approval messages show the bound before→after change.** Replace/cancel reuse the existing reconfirm diff rendering, while confirmed cancels and replacement lineage are retained in the proposal audit trail. The additive migration adds nullable `action` and `target_broker_order_id` columns to `review.order_proposals`.

### Added (ROB-816 — loss-cut completion and Toss proposal routing; migration 0)
- **Telegram loss-cut approvals now carry an explicit service identity.** `ORDER_PROPOSALS_SUBMIT_AGENT_ID` is empty by default and is scoped only to callback revalidation/submission; operators must add the same value to `LOSS_CUT_ALLOWED_AGENT_IDS`. Empty or whitespace configuration masks any outer identity and fails closed. Upbit crypto loss-cut proposals are supported for the residual KRW-DOT canary while preserving the existing retrospective, approval issue, sell-limit, and ROB-800 checks.
- **Toss proposals use Toss-native preview and placement.** `toss_live` Korean and US equity proposals route through `toss_preview_order`/`toss_place_order`, preserve exact `str | int` decimal inputs, consume Toss-normalized preview price/quantity, and round-trip the approval token, rung, canonical digest, stable proposal/rung client ID, and exact proposal correlation without exposing idempotency controls in the public MCP schema.
- **Ambiguous live outcomes stay non-retryable.** Explicit Toss rejection terminalizes, accepted sends remain `acked`/`resting` rather than filled, and any post-send timeout or ledger failure remains `unverified` with broker evidence preserved. Incomplete or malformed Toss preview capability envelopes fail closed before mutation.

### Fixed (ROB-827 — KIS VTS token cache; migration 0)
- **VTS OAuth tokens now survive MCP client churn.** Mock KIS clients share a Redis token manager scoped by normalized VTS host and a non-secret SHA-256 appkey fingerprint, so cache and lock keys cannot collide with live KIS or another VTS credential scope.
- **Slow VTS issuance remains single-flight.** The VTS-only OAuth request timeout is 10 seconds and VTS lock contenders wait 11 seconds, covering the observed 4–6 second issuance latency without changing the live KIS 5-second request timeout, 3-second contender wait, Redis keys, or order paths.

### Added (ROB-757 — Toss REST fill poller; migration 1)
- **Default-off TaskIQ poller detects Toss fills without a websocket.** `toss_live.poll_fills_periodic` scans Toss `GET /orders` (read-only), seeds app-direct orders missing from `review.toss_live_order_ledger` as `accepted` rows, then reuses `toss_reconcile_orders_impl(dry_run=False)` to book confirmed fill deltas. Activation gates: `TOSS_FILL_POLL_ENABLED=true` (default `False`), `TOSS_API_ENABLED=true`, valid Toss credentials. The task never places, modifies, or cancels broker orders.
- **Toss fills write to `review.execution_ledger`.** The reconcile booked branch now upserts Toss fill deltas via `ExecutionLedgerRepository` with `broker='toss'`, `account_mode='live'`, `source='reconciler'`, `venue='toss_kr'`/`'toss_us'`. `filled_qty` stores the newly discovered delta (not broker cumulative); `fill_seq` is derived from `(broker_order_id, previous_cumulative_qty, new_cumulative_qty)` so partial fills create distinct rows. ROB-755 triage reads these with `source='reconciler', broker='toss'`.
- **Poll-state tracking.** New `review.toss_fill_poll_state` table records `last_success_at` and `last_error` per scan scope. The discovery service wraps collection+seeding in try/except: on failure it records the error (best-effort) then re-raises so TaskIQ sees the exception. Incomplete CLOSED scans (page cap or repeated cursor) do not advance `last_success_at`, so the next cycle retries from the previous successful window.
- **Market-session gate.** `_toss_fill_poll_market_gate` skips polling outside KR session hours (09:00–20:00 KST, covering NXT) and US extended hours (premarket/regular/afterhours). Disablable via `TOSS_FILL_POLL_MARKET_GATE_ENABLED=false`.
- **ROB-755 triage surface.** CLI (`scripts/list_recent_fill_events.py`) and MCP tool (`execution_ledger_fill_events_list_recent`) help text updated to document `broker='toss'` with `source='reconciler'`. Runbooks (`fill-event-claude-triage.md`, `toss-live-order-reconcile.md`) and MCP README document the new query pattern.
- **Schema + migration.** `ExecutionLedgerUpsert.broker` Literal and ORM check constraint extended to include `'toss'`. New `TossFillPollState` ORM model. Migration `20260707_rob757_toss_fill_poller` (revises `20260707_rob755_source_id_idx`).

### Added (ROB-751 — `decision_rules.sell.trim_preplace` policy tie-break; migration 0)
- **Lane-scoped decision rules in the policy YAML.** `TradingPolicyDocument` gains an optional `decision_rules` map (`PolicyDecisionRule` → `PolicyDecisionRuleTier` with `id`/`conditions`/`action`/`sizing`, plus `tie_breaks` and `exclusions`). Field defaults to `{}` via `Field(default_factory=dict)`, so existing configs without the block still validate — purely additive. `get_policy_for(market, lane)` now echoes lane-filtered `decision_rules` alongside `thresholds`; rules whose `lanes` list does not contain the queried lane are omitted. `get_trading_policy(market, lane)` MCP tool returns the same `decision_rules` key, empty when no rule applies (e.g. `buy`/`discovery` today).
- **`sell.trim_preplace` tie-break (option A from the issue).** When `sell.resistance_near_pct=6` (PLACE direction) conflicts with `sell.upside_place_max_pct=45` (WATCH direction), resistance proximity wins but compromises on size. Three tiers: `rsi_confirmed_resistance` (RSI ≥ `sell.rsi_place_min` AND resistance within `sell.resistance_near_pct`) → `preplace_small_trim_ladder`; `ultra_near_resistance` (RSI below gate AND resistance ≤2%) → same small-trim action; `watch_zone` (RSI below gate AND resistance 2–6%) → `register_watch` with no pre-placed trim. `sell.upside_place_max_pct` becomes a size-limit-only tie-break, not an eligibility blocker. Exclusions: `single_share_position`, `no_resistance_reference`, `composite_gates`.
- **Authority scope widened.** `config/trading_policy.yaml` `authority.scope` moves from `judgment_thresholds_only` to `judgment_policy_only` and now governs "advisory judgment thresholds, decision rules, and the sector-cluster concentration cap". Fail-closed code guards (loss guard, ladder near-market, RSI scoring) remain in code, untouched. Version bumped `2026-07-02.1` → `2026-07-07.1`; content_hash recomputed. Version-stamping contract (`evidence_snapshot`, `trade_retrospectives`, `forecast`, `get_operating_briefing.policy_version`) inherits automatically.
- **Docs synced.** `docs/playbooks/trading-decision-playbook.md` sell lane gains the ROB-751 tie-break step; the policy_keys authority note now mentions decision-rule blocks. `app/mcp_server/README.md` `get_trading_policy` spec documents the new `decision_rules` field. `CLAUDE.md` trading-policy section reflects the widened scope. 4 test files updated (schema, service, mcp tool, operating briefing) + new schema-acceptance test for arbitrary `decision_rules` blocks.

### Changed (ROB-710 — `/invest` batch quotes Toss-first flip, flag-gated; migration 0)
- **Per-market layer-order flip for `/invest` current-price reads.** `PriceFallbackResolver` gains an `order` parameter (default `KIS_FIRST_ORDER = ("kis", "toss", "snapshot")` = today's byte-identical behavior). Two new `Settings` flags — `invest_quotes_toss_first_kr` / `invest_quotes_toss_first_us` (both default `False`) — flip a market to `TOSS_FIRST_ORDER = ("toss", "kis", "snapshot")`: one ≤200-symbol Toss `MARKET_DATA` batch first, KIS only for Toss-misses, snapshot tail unchanged. When Toss resolves everything, KIS is never called — reserving scarce KIS app-key TPS for OHLCV / US-intraday / live orders. Toss is already the production primary for FX + market calendar + KR warnings.
- **Flag is inert without Toss configured.** When `toss_api_enabled=False` (or Toss client construction fails), `toss_fetch` is `None`; the Toss-first order skips the `None` layer → effectively KIS → snapshot, same as today. The flag only bites when Toss is armed.
- **Revertible via env + process restart (no code deploy).** The flags are read from the `Settings` singleton, which loads env **at import**, so flipping — or reverting — a flag takes effect on the next **process restart**, not on a live in-process `/invest` load. Set the env var and restart to change ordering; set it back to `false` and restart to return to byte-identical KIS-first. No code change, no migration. Both flags ship `False`; prod stays KIS-first until an operator flips them. Canary sequence (KR first, observe, then US) is operator-gated, not code-gated. Data gates already cleared 2026-07-06: ROB-709 A/B go bars passed both markets (KR 0-tick exact; US median 0 bps / max ~1.45 bps) and ROB-708 (US live-last endpoint) is merged.
- **Scope: layer-order swap only.** `get_quote` single-symbol rich quotes and daily-200 OHLCV stay KIS (Toss has no OHLC). The `dict[str, float | None]` contract, per-layer fail-open, and the snapshot tail are preserved for both orderings. An invalid `order` fails loud (`ValueError`). The 8 pre-existing resolver/circuit tests + 7 quote-service tests stay green **unmodified** — that is the byte-identical proof.

### Fixed (ROB-744 — Mirror pairing cohort closure; migration 0)
- **Read-time mirror cohort closure.** Unstamped `kis_mock` sell rows now project onto open mirror buy lots via conservative FIFO ownership rule in `load_fills`, so counterfactual paired samples accumulate even when the exit sell is an ordinary mock sell without `mirror_cohort` stamping. Non-mirror lots keep FIFO priority over later mirror lots.
- **Pairability diagnostics.** `build_counterfactual_delta_scoreboard` now returns `pairing_diagnostics` (closed-trade counts, key coverage, unpaired counts, missing `report_item_uuid`) and `pairing_health` (`ok` / `warming_up` / `needs_design_review`) keyed off a `min_pair_threshold` (default 20). `paired_count == 0` can no longer masquerade as a valid neutral result when closed samples exist.
- **MCP contract.** `get_trading_scoreboard` forwards `min_pair_threshold` into the delta builder when `include_counterfactual_delta=True`. README documents that report-originated live orders must pass `report_item_uuid` for counterfactual pairing.

## [0.3.6] - 2026-07-06

### Added (ROB-734 — KIS Mock Mirror Counterfactual implementation; migration 1)
- **Mirror Order Plan Generation:** Created `build_mirror_order_plans` service to derive target quantities/prices from `InvestmentReportItem` using original sizing and KIS priority rules.
- **Mock Execution & Idempotency:** Implemented `execute_mirror_order_plans` and `execute_mirror_for_report` to route and stamp orders under `account_mode="kis_mock"`.
- **MCP Execution Tool:** Exposed `kis_mock_mirror_execute_report` to allow counterfactual execution from Model Context Protocol.
- **Delta Scoreboard:** Added cohort-scoped fill loader and pairing logic (`build_counterfactual_delta_scoreboard`) to compute paired expectancy PnL %/hit-rate differences between `live_gated` and `mock_counterfactual` cohorts.
- **Operating Briefing & Decision History:** Embedded the scoreboard metrics into `get_operating_briefing` response and updated `build_decision_context` to handle mock account mode and cohorts.

### Added (ROB-662 — `/invest` 회고 read-only browser; migration 0)
- **Read-only GET surface for trade retrospectives.** Two new session-cookie-authed endpoints mirror the ROB-591 watch router: `GET /trading/api/invest/retrospectives` (filterable by `market`/`trigger_type`/`root_cause_class`/`symbol`/`days`, with `limit`/`offset` pagination and a `total` filtered-count echo) and `GET /trading/api/invest/retrospectives/next-actions` (bounded-scan incomplete-action checklist with `scan_limit` echo). `trigger_type`/`root_cause_class` are validated against `VALID_TRIGGER_TYPES`/`VALID_ROOT_CAUSE_CLASSES` (invalid → 422); US symbols normalize via `to_db_symbol` in the router, not the service. No writes, no migrations — all reads go through the existing `trade_retrospective_service`.
- **Service helpers.** `get_retrospectives` gains `trigger_type`/`root_cause_class`/`offset` kwargs + a `total` filtered count (existing callers unaffected — all new kwargs are optional, `total` is additive). New `get_open_next_actions` flattens incomplete next_actions (`status != "done"`) across the `limit` most-recent retrospectives, enriching each with parent context (symbol/market/trigger/realized_pnl/correlation_id). The bounded scan is made explicit via the `scan_limit` echo and a guard against malformed entries.
- **`/my` 회고 탭.** A new `retrospectives` portfolio tab (desktop + mobile) renders a pinned incomplete next-action checklist above a filterable retrospective table (market chips + trigger filter on desktop). Mirrors the ROB-591 watch tab wiring.
- **종목상세 회고 카드.** `StockDetailPage` gains a `RetrospectiveCard` next to the ROB-592 `WatchCard`, showing per-symbol postmortems (trigger/cause pills, lesson, incomplete next actions).

### Fixed (ROB-665 — `expired` semantics alignment: KIS cancel evidence + retrospective scan + docs + US is_live; migration 0)
- **KIS order-history cancel evidence uses the real fields.** `_map_kis_status` previously gated `cancelled` on `prcs_stat_name == "주문취소"`, a key the repo's own live-verified notes (`live_order_expiry.py`, `domestic_orders.py`) confirm does **not** exist on real KIS responses — so an operator-cancelled unfilled order was mislabelled `expired` (indistinguishable from a 15:30 EOD expiry, polluting retrospective semantics; `status="cancelled"` KR queries were effectively empty). New per-row predicate `row_has_cancel_evidence` (reuses the `cncl_yn` / '취소' side-name / `rvse_cncl_dvsn_name` signals) feeds `_map_kis_status(cancel_evidence=…)`, which now wins over the ROB-657 death rule. The read-path keeps ROB-657's **time-ungated** death rule (the full `classify_day_order_expiry` gates `expired` on NXT close, which would regress the read path).
- **Retrospective scan now sees ledger `status="expired"` rows.** KIS reconcile writes raw `expired` to `review.kis_live_order_ledger` (the expired→cancelled collapse only applies to `lifecycle_state`), so those rows were invisible to `trade_retrospective_pending` in **both** modes and missing from the `excluded_by_filter` count — a silent third bucket. `"expired"` is added to `_KIS_LIVE_CANCEL_TERMINAL` (cancel-family): scanned, hidden by default, counted in `excluded_by_filter`, and surfaced under `include_cancelled=true`.
- **US (overseas) dead orders no longer report `is_live=True`.** `_normalize_kis_overseas_order` now uses the broker's `nccs_qty` (미체결수량) for `remaining` instead of synthesizing `ordered - filled`, and passes cancel evidence — so a cancelled/dead unfilled US order resolves to `expired`/`cancelled` with `is_live=False` (falls back to `ordered - filled` when `nccs_qty` is absent).
- **`get_order_history` / `kis_live_get_order_history` / `kis_mock_get_order_history` document + accept `status="expired"`.** The `status` Literal gains `expired` (wired through fetch + filter), and the tool descriptions now spell out `expired` (dead day orders, distinct from an operator cancel) and the per-order `is_live` flag.
- Read-path + scan-filter constants only; no broker/order/watch mutation, migration 0.

### Added (ROB-660 — sell lane account-routing: KIS sell + Toss cancel + order-history helpers; migration 0)
- **Sell lane now routes to the holding account.** The read-only advisory sequence for `route_request(profit_taking)` gains two ordered steps: `kis_live_place_order` (sell KIS holdings from the holding account, `dry_run` preview → live) and `toss_cancel_order` (clear a same-symbol Toss buy-pending limit **before** the sell, honoring the Toss two-sided constraint). Previously the sell lane only emitted `toss_place_order`, so a session holding the name at KIS had no advisory path to sell it. `route_request` stays a read-only advisory router — no enforcement is added; the MCP tools themselves gate live orders.
- **Allowed-only order-history helpers.** New `LANE_EXTRA_ALLOWED` constant surfaces `kis_live_get_order_history` / `toss_get_order_history` in the sell lane as read-only confirmation helpers (cancel-took-effect, fill status) — allowed but **not** sequenced and **not** added to the playbook YAML. This parallels ROB-658's `MARKET_EXECUTION_TOOLS` allowed-supplement pattern and un-blocks tools that `build_route_plan` would otherwise reject because they're bucketed in `MUTATION_TOOLS` for registry partitioning despite being read-only in reality.
- **Self-documenting hard constraints.** Two new `HARD_CONSTRAINTS["sell"]` lines spell out holding-account routing (Toss holdings → `toss_place_order`, KIS holdings → `kis_live_place_order`) and cancel-first-before-sell, so an operating session sees the two-sided rule next to the sequence that encodes it.
- **Code ↔ playbook YAML kept atomic.** The trading-decision playbook's sell-lane YAML + prose are updated in the same change; the `test_lane_sequences_match_playbook` invariant and 5 new sell-lane tests (sequence insert + contiguity, allowed-but-not-sequenced helpers, no cross-lane leak into buy, crypto profile drops KR-only tools while ROB-658 generic `place_order` injection still fires, hard-constraint text) all pass. Buy / discovery / bootstrap lanes are untouched.

## [0.3.5] - 2026-07-03

### Changed (ROB-659 — ROB-643~653 batch verification close-out; all minor, migration 0)
- **approval-hash mode enum validation (fail-loud).** `TOSS_APPROVAL_HASH_MODE` and `ORDER_APPROVAL_HASH_MODE` are now validated at settings load against `{off, optional, warn, required}` (case/whitespace normalized). A typo like `requird` previously passed `mode != "off"` but matched no branch, silently degrading to optional-level behavior — it now raises at boot.
- **`required`-mode gate scoped to LIVE (`not is_mock`).** The `ORDER_APPROVAL_HASH_MODE=required` fail-close in `_place_order_impl` now exempts mock/automation callers (mock scalping, watch auto-execute, `kis_mock_*`), so flipping `required` cannot break internal loops. New runbook §6 (`docs/runbooks/order-approval-hash.md`) documents the cutover checklist, including the still-unplumbed live `ScreenerService` REST path as the remaining gate; `warn`-mode soak recommended before `required`.
- **MCP tool descriptions** for `toss_preview_order`/`toss_place_order` and `place_order`/`kis_live_place_order` now document the `approval_hash`/`rung`/TTL parameters and the mode contract, so operating sessions can discover the binding.
- **`route_request` fixes:** an executing lane no longer lists its own dry-run/approval-minting precursor (`toss_preview_order`) in `blocked_actions` — a self-contradiction in `required` mode (the preview mints the hash `toss_place_order` requires). Missing `intent`/`market` now return a deterministic `success=false` envelope (`missing_intent`/`missing_market`) instead of a FastMCP schema error.
- **Playbook ↔ ROB-658 sync:** the trading-decision playbook now documents the market-aware execution divergence (crypto/US route through the generic `place_order`, KR lanes stay the single source) instead of leaving it only in code/README.
- **forecast ledger hardening (ROB-650):** `list_forecasts`/`get_forecast_calibration` symbol filters normalize via `to_db_symbol` so an external `BRK-B` query matches the stored `BRK.B` (crypto pairs like `KRW-BTC` preserved); the default `policy_version` now comes from the ROB-646 `policy_version_stamp()` (fail-open to the legacy literal) instead of the stale `forecast.v1`; new real-path integration test for `_read_window_candles` (dev-DB gated).
- **docs/hygiene:** README `analysis_artifact_list` signature now lists `correlation_id`/`account_scope`; Upbit order-submission path-suffix detection has a constraint note; backfilled the missing 0.3.3 CHANGELOG entry (below).

## [0.3.4] - 2026-07-02

### Added (ROB-650 — resolvable forecast ledger + deterministic resolve + Brier calibration)
- New `review.trade_forecasts` table + `app/services/trade_journal/forecast_service.py` (repository is the only write surface): records a resolvable probabilistic claim (a buy thesis or a profit-taking WATCH→PLACE verdict) with `forecast_id` idempotency key, artifact/journal/report_item/correlation links, `forecast_target` JSONB, `probability` (+ optional range with a DB cross-column check), `review_date`, `status` (open/closed), resolution outputs (outcome/observed_value/brier_score/resolved_at), and a `policy_version` stamp. Composition stays a Claude session (LLM boundary); recording/resolution/scoring are fully deterministic.
- `resolve_forecast` is idempotent (a closed forecast is never re-scored): `price_target` claims resolve deterministically against DB-first daily OHLCV (ROB-639), non-price claims require a manual outcome + evidence. Brier = `(probability - outcome)²`.
- `build_forecast_calibration_aggregate` groups closed, scored forecasts by `created_by` / `session_label` / `model_label` / KST `day` → average Brier, hit-rate, and calibration_gap (avg_probability − hit_rate) — the objective metric behind an operator's "does another LLM reach the same result" comparison.
- New read-only `DailyCandlesRepository.fetch_range` window query for deterministic resolution (KR/US/crypto).
- New MCP tools `forecast_save`, `forecast_resolve` (dry_run-default), `get_forecasts`, `get_forecast_calibration`, registered always-on next to the trade retrospective tools.
- Single alembic migration `20260702_rob650` also merges the two heads left on main by ROB-647 and ROB-648 (both branched from `20260702_rob641`) back into one head.
- Follow-up (out of scope): scheduleless auto-resolve TaskIQ job (ROB-405/475 convention); `policy_version` reads a local constant until ROB-646 lands.

## [0.3.3] - 2026-07-02

> Backfilled by ROB-659 — this entry was omitted when ROB-648 (PR #1362, squash `ed94078e`) merged.

### Added (ROB-648 — analysis_artifacts lifecycle + fresh-artifact soft-gate)
- Server-computed `content_hash` (SHA-256 over the canonical payload) on `analysis_artifact_save`: a `correlation_id` re-save whose payload hashes identical returns `action="unchanged"` with no write and the `version` preserved; a changed payload bumps `version` in place (`action="updated"`).
- Reduced-surface advisory `readiness_label` enum and per-kind default TTL when `valid_until` is omitted (price/screen kinds expire at end of the `as_of` KST day; `session_summary`/`briefing` at end of the next KST day) so an artifact is never never-stale.
- `analyze_stock_batch` surfaces a fail-open `fresh_artifact_exists` hint (&&-overlap) so a session can skip duplicate analysis.
- Single alembic migration `20260702_rob648` (down_revision `20260702_rob641`).

## [0.3.2] - 2026-07-02

### Fixed (ROB-645 — order POST timeout retry → live double-submit exposure removed)
- KIS order-mutation callsites (domestic + overseas: order/cancel/modify) now pass `retry_request_errors=False` and `max_retries_override=0` to the shared transport, so a timed-out **or** rate-limited (EGW00215 '초과' / HTTP 429) order POST is sent exactly once and never re-POSTed. Read paths (quotes/balance/history) keep their existing RequestError and rate-limit retries.
- Upbit order-creation POSTs (`POST /v1/orders`) are excluded from the `_retry_with_backoff` RequestError retry (a timed-out order may have reached the broker); GET reads and DELETE cancels keep retrying. Each Upbit order now carries a unique `identifier` client idempotency key (uuid4 per order) so a resent/duplicate order is rejected by the broker.
- A timed-out/network-failed order send now surfaces an explicit, non-blank error (`outcome_unknown: true` + `reconcile_tool`) telling the caller to run `kis_live_reconcile_orders` (KR) / `live_reconcile_orders` (US·crypto) instead of re-sending — never a blank error, never an auto-retry.

### Added (ROB-585 absorbed by ROB-645 — KIS batch order pre-send throttle)
- Order TR_IDs (domestic/overseas order-cash + order-rvsecncl) throttled to 8/s in `DEFAULT_KIS_API_RATE_LIMITS`. With order re-POST retries removed, this pre-send wait is the sole guard against the KIS 초당 거래건수 limit; orders that still exceed it fail fast rather than being re-sent. Supersedes PR #1331 (its `max_retries_override=3` would have re-POSTed on EGW00215).

## [0.3.1] - 2026-06-17

### Added (ROB-592 — stock detail per-symbol watch card + fill detail upgrade)
- Per-symbol watch card on the stock detail page (`/invest/stocks/:market/:symbol`): new `WatchCard` reuses the ROB-591 watch read endpoint via a new optional `symbol` query param on `GET /trading/api/invest/watches` (no new endpoint). Backend canonicalizes US separator forms (`BRK-B`/`BRK/B` → `BRK.B`) like the ROB-559 order-ledger endpoint; KR/crypto matched raw.
- Extract watch row presentation helpers into shared `components/my/watchPresentation.ts` (status/proximity pill tones + labels, money/date/condition formatters); `WatchAlertsPanel` and the new `WatchCard` both consume them (single source of truth).
- Upgrade the stock detail 체결 내역 (`OrdersCard`) from a bare `side quantity` list to a full table (일시 · 구분(매수/매도) · 수량 · 단가 · 총액 · 출처) using the existing `/orders` data; currency inferred from market (KR/crypto → ₩, US → $), source labelled 실시간/보정/수동.

### Changed (ROB-591 follow-up)
- `WatchPanelService.list_watches` accepts an optional `symbol` filter (forwarded to `repository.list_alerts(symbol=...)`).

## [0.3.0] - 2026-06-17

### Added (ROB-591 — /invest/my watch tab + watch read endpoint)
- New read-only endpoint `GET /trading/api/invest/watches` with `market` (all/kr/us/crypto) and `status` (all/active/triggered/expired/canceled) filters, backed by `InvestmentReportsRepository.list_alerts`.
- Generalize `list_active_alerts` into `list_alerts` (new `status` param) with backward-compatible delegation from `list_active_alerts`; all existing callers unaffected (522 regression tests pass).
- New `WatchPanelService` enriches rows with symbol names (fail-open), price proximity for `price_above`/`price_below` metrics via `compute_price_proximity`, latest trigger event lookup for non-active rows, and `near_expiry` flag (active alerts with `valid_until` ≤ 2 days).
- New Pydantic schemas `WatchesResponse`, `WatchAlertRow`, `WatchEventSummary` with `ConfigDict(extra="forbid")` and Decimal field serializers matching invest schema conventions.
- New frontend `WatchAlertsPanel` with market/status filters, proximity pill, status pill, near_expiry badge, and symbol links to stock detail page.
- Wire `watchAlerts` tab into desktop and mobile portfolio pages (`portfolioTabs.ts` union, `PORTFOLIO_TABS` array, and `parsePortfolioTab` string check).
- Current price resolved from `market_quote_snapshots` table; data_state reflects snapshot availability (ok/degraded/unavailable).

## [0.2.4] - 2026-06-16

### Changed (ROB-589 — allocation/crypto discovery polish)
- `get_portfolio_allocation`: add `by_currency` roll-up (KRW vs USD with `fx_conversion_needed`) and unify KIS domestic/overseas cash + holdings into a single `kis` account row.
- `get_crypto_order_flow`: description now advertises multi-window + `consensus`; ticks are defensively sorted newest-first before windowing.
- `get_upbit_altseason` constituents now include RS=0 (rate == BTC) rows to match the `get_top_stocks(relative_strength)` boundary; the `alts_beating_btc` ratio stays strict `>`.

## [0.2.3] - 2026-06-16

### Added (ROB-582 — Cross-asset allocation roll-up)
- New read-only MCP tool `get_portfolio_allocation`: unified KRW-based asset-class weights (US/KR equity, crypto, cash) across KIS/Toss/Samsung/Upbit holdings and cash, with optional `target_weights` over/underweight drift flags and per-asset-class P&L.
- KR-listed ETF look-through reclassifies US-index ETFs (e.g. TIGER/KODEX/SOL/RISE 미국S&P500·나스닥100) into effective US-equity exposure via KRX ETF metadata; fail-open to the surface label when metadata is unavailable. No order/mutation path.

## [0.2.2] - 2026-06-16

### Added (ROB-581 — Crypto Discovery Tools)
- Expose Upbit altseason constituents list with relative strength calculations (vs. BTC).
- Add crypto `relative_strength` ranking sorting and screening options.
- Expose a dedicated `get_crypto_top_movers` MCP tool on FastMCP for real-time asset discovery.
- Update `app/mcp_server/README.md` documentation.
- Add comprehensive test coverage in `tests/test_upbit_index_service.py`, `tests/test_mcp_top_stocks.py`, and `tests/test_mcp_profiles.py`.

## [0.2.1] - 2026-06-16

### Added (ROB-580 — Multi-window crypto order flow)
- Multi-window analysis (50, 200, 500 ticks) derived from a single atomic fetch.
- Disjoint trend consensus (recent 50 vs older 450 ticks) with `strengthening`, `weakening`, and `reversing` categorization.
- Noise filtering with 0.10 net-flow deadband.
- Confidence scoring based on trade density and "whale" trade dominance (>35% of volume).
- Response metadata: `span_seconds`, `largest_trade_share`, `as_of`, and `default_window`.

## [0.2.0] - 2026-02-12
### Changed
- **Breaking**: `get_open_orders` tool removed. Replaced by `get_order_history(status="pending")`.
- **Breaking**: `get_order_history` updated to v2 spec.
  - Added `status`, `order_id`, `side` arguments.
  - Retained `market` argument as optional hint.
  - Default `limit` changed to 50.
  - `symbol` became optional if `status="pending"`.
  - `days` became optional (no longer defaults to 7).

### Added
- `truncated` boolean field in `get_order_history` response.
- `total_available` integer field in `get_order_history` response.

## Unreleased

### Fixed (invest stock detail — crypto bare symbols)
- `/invest/stocks/crypto/BTC` now resolves to the canonical Upbit market `KRW-BTC` instead of returning `/invest/api/stock-detail/crypto/BTC 404`. Crypto detail links from the right panel, recent symbols, watchlist, and realtime rows now canonicalize bare Upbit base symbols before navigation, while the backend accepts `BTC`, `btc`, `BTC-KRW`, and `KRW-BTC` route inputs.

### Added (ROB-305 — Futures Demo `status=NEW` MARKET reconcile)
- `BinanceFuturesDemoExecutionClient.get_order` — signed `GET /fapi/v1/order?symbol=&origClientOrderId=` single-order status query, plus a `FuturesDemoOrderStatusResult` DTO. This is the bounded fill-evidence source for §4 reconciliation.

### Fixed (ROB-305 — Futures Demo `status=NEW` MARKET reconcile)
- `scripts/binance_futures_demo_smoke.py` no longer treats a MARKET submit response of `status=NEW` as an immediate success/failure. A `submitted` ledger row is never advanced straight to `closed` (the locked state machine forbids `submitted → closed`); previously a `NEW` open response left the row in `submitted` and the unconditional `record_closed(open)` raised an illegal transition. Fill evidence is now proven — in order — via submit status, a **bounded** `GET /fapi/v1/order` poll (`_FILL_RECONCILE_MAX_POLLS`, no unbounded loop), then a non-flat `GET /fapi/v2/positionRisk` — before the row reaches `filled` and then `closed`/`reconciled`. Applies to both the open and reduceOnly close legs.
- When the post-close account is flat with zero open orders but the close fill could not be proven, the close row is recorded as a **safe anomaly** and `--confirm` exits `2`. A benign final account state is never reported as a clean success without fill evidence.
- The bounded fill poll tolerates a transient `GET /fapi/v1/order` error: demo-fapi returns `400` for an order it has just accepted but not yet indexed for lookup (the same order returns `200 FILLED` a moment later). The poll logs and keeps retrying within `_FILL_RECONCILE_MAX_POLLS` (still bounded, still fail-closed after exhaustion) instead of giving up on the first error — found and fixed via the live Demo smoke.

### Documentation (ROB-305)
- Spot Demo runbook now documents the canonical shared `BINANCE_DEMO_API_KEY` / `BINANCE_DEMO_API_SECRET` pair (matching the Futures runbook and the ROB-302 resolver); the legacy `BINANCE_SPOT_DEMO_API_*` names are described as a transitional alias, not a Spot-only credential or a Futures fallback. Both Demo runbooks and `CLAUDE.md` document the §4 `NEW`-reconcile lifecycle.

### Fixed (ROB-303 — Futures Demo confirm reconcile: v2 positionRisk)
- `BinanceFuturesDemoExecutionClient.get_position` now calls `GET /fapi/v2/positionRisk` instead of `/fapi/v1/positionRisk`. `demo-fapi.binance.com` rejects the v1 path with `404 {"code": -5000, "msg": "Path /fapi/v1/positionRisk, Method GET is invalid"}`, which aborted `--confirm` after a real Demo position had been opened — leaving the ledger row in `anomaly`. v2 returns the same `positionAmt` / `entryPrice` / `leverage` list shape, so parsing is unchanged. Constant, docstrings, and the position DTO doc are updated to match; the smoke runbook already documented v2.


- Futures Demo preflight now calls `GET /fapi/v2/account` instead of `/fapi/v1/account`. `demo-fapi.binance.com` returns `404` for v1; v2 returns the same redacted summary fields (`canTrade`, nonzero asset/position counts) so evidence shape is unchanged.
- `scripts/binance_futures_demo_smoke.py` now selects the requested symbol's row from the `exchangeInfo` response instead of `symbols[0]`. demo-fapi does not honor the `symbol=` query param and can lead the array with BTCUSDT, so XRPUSDT was being sized against BTCUSDT's step/precision/min-notional (cap-10 falsely blocked at `MIN_NOTIONAL=50`). The requested symbol is now matched and the helper fails closed if it is absent.
- The submitted MARKET quantity is quantized to the symbol's `quantityPrecision` on **both** the open leg and the reduceOnly close leg. A step-floored `Decimal` carried the `exchangeInfo` step string's trailing zeros (`"0.10000000"` → `"30.00000000"`) which `format(qty, "f")` emitted verbatim, triggering Binance `-1111 Precision is over the maximum`. The close leg sizes from `abs(positionAmt)` and is now quantized identically, so a confirmed open can no longer be left with a failing close.
- LIMIT confirm orders floor to `LOT_SIZE` while MARKET orders floor to `MARKET_LOT_SIZE`, so a coarser MARKET step no longer over-floors or blocks a LIMIT smoke order.

### Added (ROB-302)
- Canonical shared Demo credential resolution (`app/services/brokers/binance/demo/credentials.py`): set `BINANCE_DEMO_API_KEY` / `BINANCE_DEMO_API_SECRET` once and both the Spot Demo and Futures Demo lanes use it — no duplicate secret per lane. Per-product vars (`BINANCE_{SPOT,FUTURES}_DEMO_API_*`) remain optional overrides that win when set. Credential pairs resolve by source: a half-set override (key without secret, or vice versa) fails closed and is never completed from the canonical pair. Each lane's `*_ENABLED` flag still gates activation independently, and a Spot-specific override never resolves for Futures (crossing happens only through the explicit canonical pair).
- `--readiness` evidence now reports `credential_source` (`futures_demo_env` / `shared_demo_env`) and `credential_incomplete`, so operators can confirm which credential pair would be used without any value being printed.

### Added (ROB-299 — Binance Demo smoke hardening + Futures env readiness)
- Spot Demo `--confirm` close path is now fee-aware: the closing SELL sizes from the live free base-asset balance (step-floored, min-notional gated) instead of reusing the original BUY quantity, so a commission-reduced balance no longer triggers an insufficient-balance failure that needs manual remediation.
- New `--readiness` mode on `scripts/binance_futures_demo_smoke.py`: a no-secret, no-HTTP report of `BINANCE_FUTURES_DEMO_{ENABLED,API_KEY,API_SECRET,BASE_URL}` presence/truthiness and host-allowlist judgment, surfacing every missing var at once. Reads only the Futures Demo namespace — Spot Demo and legacy testnet env never leak in.
- New narrow `BinanceSpotDemoExecutionClient.get_asset_balance(asset)` signed read-side method returning only the requested asset's free/locked amounts; the full account payload never enters logs or evidence.
- Structured `spot_demo_smoke_report` evidence event summarizing deployed SHA, env readiness, buy/close quantities and status, open-order count, residual dust, reconciliation status, and blockers.

### Changed (ROB-299)
- Spot Demo close reconciliation now classifies sub-min-notional residue as benign **dust** (ledger row marked `reconciled` with a `residual_dust` note) instead of an anomaly. A dirty order book or a still-sellable remainder is recorded as an anomaly carrying an operator-readable remediation hint.

### Added (ROB-179 — /invest/api/feed/research)
- New `GET /invest/api/feed/research` endpoint on the existing `/invest/api` router. Exposes the ROB-178 `research_reports` table as a paginated, citation-shaped user feed with cursor pagination, 7 tabs (`top`, `latest`, `mine`, `watchlist`, `holdings`, `kr`, `us`), and filters (`source`, `symbol`, `analyst`, `category`, `query`, `fromDate`, `toDate`). Mirrors `/invest/api/feed/news` shape and conventions. Copyright guardrail tests (recursive scan for body fields) are the structural safety gate.

### Added (ROB-56 — KIS official mock hard-separation)
- `MCP_PROFILE` env var (`default` / `hermes-paper-kis`) gates which order tool surface is registered at startup.
- New `hermes-paper-kis` profile: only `kis_mock_*` typed order tools registered; live order surface (`kis_live_*`, legacy ambiguous tools) physically absent from the MCP tool list.
- Typed `kis_live_*` MCP order tools (`kis_live_place_order`, `kis_live_cancel_order`, `kis_live_modify_order`, `kis_live_get_order_history`) — hard-pin `is_mock=False`; additive in `default` profile.
- Typed `kis_mock_*` MCP order tools (`kis_mock_place_order`, `kis_mock_cancel_order`, `kis_mock_modify_order`, `kis_mock_get_order_history`) — hard-pin `is_mock=True`; fail closed on missing KIS mock config.
- Broker capability metadata registry (`app/services/brokers/capabilities.py`): KIS and Kiwoom declared as KR+US equity brokers; metadata only, no routing change.
- `_KISSettingsView` credential isolation regression tests (ROB-19 phase-2 carry).

### Changed (ROB-56)
- `register_all_tools` now accepts an optional `profile: McpProfile` parameter (default `McpProfile.DEFAULT`); existing deployments unaffected.

- Breaking: Require Python 3.13+ and drop support for Python 3.11 and 3.12.
