# ROB-283 — Binance Crypto Candles & Testnet Scalping MVP — Decisions Lock & Issue Split

> **For agentic workers:** This is a parent/epic **decisions-lock** plan, not a TDD execution plan. No code, no migrations, no scheduler activation, no broker/order/watch/order-intent mutation in this PR. Implementation lives in three child issues, each of which will get its own implementation plan after this document is approved.
>
> AOE_STATUS: plan_ready
> AOE_ISSUE: ROB-283
> AOE_ROLE: parent/epic decision lock
> AOE_NEXT: open child issues 283-a/b/c, draft per-child implementation plans, route back through review

---

## 1. What this plan is (and isn't)

**Is:** A single source of truth for design decisions that must be fixed before any implementation begins on ROB-283. Specifically:
- Instrument master + candle schema shape.
- Legacy `crypto_candles_1d` handling and rollback story.
- Binance public market data adapter boundaries.
- WebSocket reliability + REST backfill contract.
- Binance host allowlist / fail-closed enforcement at transport layer.
- Testnet ledger pattern and service-only write rule.
- Interface contract with ROB-282 crypto screener.
- Child issue split with scope and acceptance criteria.

**Is not:**
- A TDD task list. Per-child implementation plans cover that.
- An implementation of any of the above. ROB-283 (the parent) keeps **status = Backlog** and ships **no migrations, no adapter code, no ledger tables, no scheduler entries**.
- A decision about live Binance trading. Live Binance is an explicit non-goal of this epic.

---

## 2. Hard safety invariants (apply to all child issues)

1. **No live Binance trading.** Live Binance endpoints must be unreachable from any execution code path created under this epic.
2. **No live Binance custody.** No API keys with withdraw permission. No fund transfers.
3. **Testnet credentials only.** Testnet execution requires `BINANCE_TESTNET_ENABLED=true` **and** testnet API key/secret pair **and** transport-layer host allowlist passing **and** explicit per-call dry-run override absent.
4. **Public market data is read-only.** No signed endpoints from the market-data adapter. No order/account methods exposed in the market-data adapter class.
5. **No production scheduler activation in any child PR.** Scheduled refreshes ship paused/disabled and are unpaused by a separate operator action.
6. **No broker/order/watch/order-intent mutation outside the testnet ledger.** All ledger writes go through the dedicated testnet ledger service. Direct SQL `INSERT/UPDATE/DELETE` against the testnet ledger is forbidden and documented in CLAUDE.md.
7. **No Upbit/Alpaca/KIS live execution path changes.** Crypto execution mapping (`app/services/crypto_execution_mapping.py`) is read-only for this epic.
8. **No silent fallback.** Any disagreement between config flag, env var, host allowlist, or credential pair must fail closed at initialization, not log-and-continue.
9. **Default mode is `dry_run`.** Confirmed testnet submit is opt-in per call, never per session, never per service-init.

---

## 3. Issue split

ROB-283 stays as parent/epic. Three child implementation issues are proposed, each producing a working, testable PR on its own. Numbering is provisional — real Linear IDs will be assigned at issue creation.

### Child A — `crypto_instruments` master table + venue-aware candle schema
**Scope:**
- New `crypto_instruments` table (see §4.1).
- Add `crypto_candles_1m` table (new) with the same instrument-FK schema.
- Migrate `crypto_candles_1d` to instrument-FK shape (see §4.4).
- New SQLAlchemy models for both candle tables and the instruments table.
- Update `DailyCandlesRepository` and add `MinuteCandlesRepository` to write against the new shape.
- Provide a `crypto_market_view` (DB view or read-model) that surfaces `market` / `venue_symbol` strings for backwards-compatible read paths (ROB-282 snapshot builder, dashboards).
- Seed `crypto_instruments` with known Upbit KRW pairs; do **not** seed Binance rows in this child.

**Acceptance criteria (Child A):**
- `crypto_instruments` exists with unique `(venue, product, venue_symbol)`.
- Both `crypto_candles_1d` and `crypto_candles_1m` reference `instrument_id` and partition by `(instrument_id, time)`.
- `crypto_candles_1d` legacy rows have `instrument_id` populated 100% (or are isolated in a renamed `crypto_candles_1d_legacy` table — see §4.4 decision).
- Upbit `KRW-BTC`, Binance spot `BTCUSDT`, Binance USDT-M futures `BTCUSDT`, Alpaca crypto paper `BTC/USD` can each be inserted as distinct rows for the same `time` bucket without collision (test).
- Upsert idempotency test: re-inserting the same closed candle is a no-op (or version-bump only).
- Downgrade migration exists, OR a documented rollback runbook covers `crypto_candles_1d_legacy` table swap-back.
- Repository tests cover symbol normalization, source metadata, `is_closed` finality flag.
- No code outside this PR depends on `instrument_id` yet (i.e., Child A ships behind a screener read-model view so Child B/C can land independently).

### Child B — Binance public market data adapter
**Scope:**
- New `app/services/brokers/binance/` package.
- Public REST adapter: exchangeInfo, klines backfill, bookTicker.
- Public WebSocket Streams adapter: `kline_1m`, `bookTicker`, optionally `aggTrade`.
- Persistence via `DailyCandlesRepository`/`MinuteCandlesRepository` (built in Child A).
- Reconnect/backoff (see §4.6).
- Gap detection + REST backfill on closed-kline gaps with explicit caps (see §4.6).
- Rate-limit/weight telemetry (see §4.6).
- `binance-sdk-spot` dependency vetting (see §4.5).
- CLI smoke: `python -m scripts.binance_public_smoke --symbol BTCUSDT` (no credentials).

**Acceptance criteria (Child B):**
- Public REST kline backfill works with no API key set.
- Public WS smoke (kline + bookTicker) runs without API key.
- Closed 1m candles upserted idempotently into `crypto_candles_1m`.
- In-progress (non-closed) candles are either dropped or persisted with `is_closed=false`; never overwrite a closed candle.
- Reconnect/backoff: minimum 3 retries with exponential backoff before declaring the stream unhealthy.
- Gap detection: on reconnect, query last persisted closed candle per instrument and REST-backfill missing 1m candles up to cap.
- Cap exceeded → instrument enters `manual_backfill_required` state; downstream scalping cannot trade that instrument.
- Tests cover: reconnect, gap fill, cap exceeded, rate-limit header parsing, host allowlist rejection (live host injected via env override).
- Public smoke documented in `docs/runbooks/binance-public-market-data.md`.

### Child C — Binance testnet scalping MVP
**Scope:**
- New `app/services/brokers/binance/testnet_execution.py` (signed adapter, testnet-only).
- New `binance_testnet_order_ledger` table + `BinanceTestnetLedgerService` mirroring AlpacaPaperLedgerService shape.
- Deterministic scalping state machine (entry/TP/SL) in `app/services/scalping/binance_testnet_scalper.py`.
- Host allowlist enforcement at transport layer (see §4.7).
- Symbols MVP scope: `BTCUSDT`, `ETHUSDT`, `SOLUSDT`.
- Max one open testnet position per symbol.
- Max notional default: `10 USDT` per candidate (configurable via env, but per-call override required to exceed).
- No scheduler activation (manual CLI invocation only for MVP).
- Optional Discord/Hermes notification on state transitions — notification only, never approval.

**Acceptance criteria (Child C):**
- Deterministic state machine runs in shadow mode against Binance testnet feed.
- Testnet order preview is a no-op by default (`dry_run=true`).
- Confirmed testnet submit fails closed unless all of:
  - `BINANCE_TESTNET_ENABLED=true`
  - testnet credentials present
  - host allowlist passes (live host injected → init raises)
  - per-call `confirm=True` flag
- TP/SL state recorded in `binance_testnet_order_ledger` via service-only writes.
- Reduce-only semantics enforced for any futures exit order (if futures path is included; spot MVP may skip).
- Test asserts no code path can route to `api.binance.com` or other live hosts.
- Test asserts direct SQL `INSERT/UPDATE/DELETE` against `binance_testnet_order_ledger` is not possible from service code (mirror Alpaca ledger convention).
- Runbook `docs/runbooks/binance-testnet-scalping.md` documents enable steps, env vars, testnet quirks (reset cadence, OCO availability), and how to drain/close open positions manually.

### Child dependency graph
- Child A is a hard prerequisite for Child B (B writes via repositories that depend on Child A schema).
- Child A is a hard prerequisite for Child C (C ledger references instruments).
- Child B is a soft prerequisite for Child C (C can run on Child B's WS feed in shadow mode; without Child B, only REST polling).

---

## 4. Locked decisions

### 4.1 Instrument master + candle schema

**Locked: introduce `crypto_instruments` master table; candles reference it by FK.**

```sql
CREATE TABLE crypto_instruments (
    id BIGSERIAL PRIMARY KEY,
    venue TEXT NOT NULL,             -- 'upbit', 'binance', 'alpaca'
    product TEXT NOT NULL,           -- 'spot', 'usdm_futures', 'paper'
    venue_symbol TEXT NOT NULL,      -- venue-native symbol: 'KRW-BTC', 'BTCUSDT', 'BTC/USD'
    base_asset TEXT NOT NULL,        -- 'BTC', 'ETH', 'SOL'
    quote_asset TEXT NOT NULL,       -- 'KRW', 'USDT', 'USD'
    status TEXT NOT NULL DEFAULT 'active',  -- 'active', 'delisted', 'halted'
    precision_price INTEGER NULL,
    precision_amount INTEGER NULL,
    tick_size NUMERIC NULL,
    lot_size NUMERIC NULL,
    min_notional NUMERIC NULL,
    listed_at TIMESTAMPTZ NULL,
    delisted_at TIMESTAMPTZ NULL,
    metadata JSONB NULL,             -- venue-specific fields (margin tiers, contract size, etc.)
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (venue, product, venue_symbol),
    CHECK (status IN ('active','delisted','halted'))
);

CREATE INDEX idx_crypto_instruments_venue_product_base ON crypto_instruments (venue, product, base_asset);
CREATE INDEX idx_crypto_instruments_base_quote ON crypto_instruments (base_asset, quote_asset);
```

**Candle tables (both 1d and 1m share shape):**

```sql
CREATE TABLE crypto_candles_1m (
    instrument_id BIGINT NOT NULL REFERENCES crypto_instruments(id),
    time TIMESTAMPTZ NOT NULL,
    open NUMERIC NOT NULL,
    high NUMERIC NOT NULL,
    low NUMERIC NOT NULL,
    close NUMERIC NOT NULL,
    base_volume NUMERIC NOT NULL,          -- base-asset volume; required for any closed candle
    quote_volume NUMERIC NULL,             -- quote-asset / value; nullable (not all sources expose)
    trade_count INTEGER NULL,
    vwap NUMERIC NULL,
    taker_buy_base_volume NUMERIC NULL,
    taker_buy_quote_volume NUMERIC NULL,
    is_closed BOOLEAN NOT NULL DEFAULT TRUE,
    source TEXT NOT NULL,                  -- 'binance_sdk_ws', 'binance_sdk_rest', 'upbit_ws', etc.
    source_event_at TIMESTAMPTZ NULL,
    ingested_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (instrument_id, time),
    CHECK (base_volume >= 0),
    CHECK (quote_volume IS NULL OR quote_volume >= 0),
    CHECK (trade_count IS NULL OR trade_count >= 0),
    CHECK (vwap IS NULL OR vwap >= 0),
    CHECK (high >= low),
    CHECK (high >= open AND high >= close),
    CHECK (low <= open AND low <= close)
);

-- Timescale hypertable: chunk by time, 1-day interval for 1m, 90-day for 1d (matches existing kr/us tables).
SELECT create_hypertable('crypto_candles_1m', 'time', chunk_time_interval => INTERVAL '1 day');

CREATE INDEX idx_crypto_candles_1m_source ON crypto_candles_1m (source, time DESC);
```

`crypto_candles_1d` follows the same shape with `chunk_time_interval => INTERVAL '90 days'` (matches existing convention).

**Read-model view for backwards-compatible string identifiers:**

```sql
CREATE VIEW crypto_candles_1d_view AS
SELECT
    c.time,
    c.instrument_id,
    i.venue,
    i.product,
    i.venue_symbol AS symbol,
    i.base_asset,
    i.quote_asset,
    -- derived market key, computed not stored:
    (i.venue || '_' || i.product || '_' || lower(i.quote_asset)) AS market,
    c.open, c.high, c.low, c.close,
    c.base_volume AS volume,
    c.quote_volume AS value,
    c.is_closed, c.source, c.source_event_at, c.ingested_at
FROM crypto_candles_1d c
JOIN crypto_instruments i ON i.id = c.instrument_id;
```

**Rationale recap:**
- `(venue, product, venue_symbol)` is the natural instrument identity. Within a venue+product, `venue_symbol` determines `base_asset`/`quote_asset`, so duplicating those into the candle PK would be redundant and slow upserts (resolves over-specified-unique concern from review).
- `market` is **derived**, never stored on the candle row. Drift impossible by construction.
- `base_volume` is `NOT NULL` (every closed candle has it); `quote_volume`/derived metrics nullable to handle source variability (resolves NOT NULL conflict from review).
- `source` is metadata, not part of identity. Re-ingesting via a different source for the same closed bucket is an idempotent upsert, not a duplicate row.

### 4.2 Candle fields and constraints

**Locked:**
- `open/high/low/close/base_volume`: `NOT NULL`.
- `quote_volume, trade_count, vwap, taker_buy_*, source_event_at`: nullable.
- Non-negative `CHECK` constraints on all numeric columns where applicable.
- OHLC sanity `CHECK`s: `high >= low`, `high >= max(open, close)`, `low <= min(open, close)`.
- `is_closed BOOLEAN NOT NULL DEFAULT TRUE`. In-progress candles must explicitly set `is_closed=false`.
- Upsert behavior: `ON CONFLICT (instrument_id, time) DO UPDATE` — only overwrite when incoming `is_closed=true` and stored `is_closed=false`, or when incoming `source_event_at` is newer than stored. Never silently overwrite a closed candle with a less-trustworthy source.

### 4.3 Timescale

**Locked:**
- Timescale **is** required (validated by existing migrations; `crypto_candles_1d` is already a hypertable in production).
- Both new/altered tables remain hypertables. Chunk intervals: 1d = 90 days, 1m = 1 day (consistent with `kr_candles_1d` / `us_candles_1d` precedent for 1d, and a reasonable starting point for 1m).
- **Continuous aggregates are an explicit non-goal for ROB-283.** Any need for materialized rollups (1m → 5m → 1h) is deferred to a follow-up issue.

### 4.4 Legacy `crypto_candles_1d` handling

**Locked: in-place migration with instrument backfill, not table rename.**

Rationale: Production `crypto_candles_1d` has Upbit-only data with known `(symbol, market)` pairs. A rename-and-rebuild would discard valid historical bars. Backfilling `instrument_id` is bounded work and reversible.

Migration steps (for Child A implementation plan):
1. Create `crypto_instruments` table.
2. Seed Upbit KRW instruments (`KRW-BTC`, `KRW-ETH`, etc.) — one row per distinct `(symbol, market)` already present in `crypto_candles_1d`.
3. Add `instrument_id BIGINT NULL REFERENCES crypto_instruments(id)` column to `crypto_candles_1d`.
4. UPDATE `crypto_candles_1d` rows joining on `(symbol, market)` → `instrument_id`.
5. Add `NOT NULL` constraint after backfill completes and row count matches.
6. Drop `symbol` and `market` columns. Drop old indexes/uniques.
7. Add new PK `(instrument_id, time)` and indexes.
8. Add `is_closed`, `quote_volume` (renamed from `value`), `source_event_at`, and CHECK constraints.

**Rollback:**
- Downgrade migration restores `symbol`/`market` columns from `crypto_instruments` JOIN.
- Documented rollback runbook in `docs/runbooks/daily-candles-store.md` covers manual revert if downgrade fails mid-flight.
- Pre-migration backup of `crypto_candles_1d` to `crypto_candles_1d_pre_rob283` table is required as a step in the implementation plan (operator command, not in the migration itself).

### 4.5 Binance SDK dependencies

**Locked principles, exact version locked per child PR:**
- Use `binance-sdk-spot` (official) behind a local adapter class in Child B. Do not let SDK types leak across the adapter boundary.
- `binance-sdk-derivatives-trading-usds-futures` is added **only** in Child C, and only if the futures path is implemented in that PR. Spot-first MVP may defer futures entirely.
- Acceptance gate for each SDK package (must appear in Child B/C acceptance criteria, not handoff-only):
  - License: MIT / Apache 2 / equivalent permissive. Reject GPL/AGPL.
  - Maintenance: at least one release in trailing 12 months, no open critical-security issues at PR time.
  - Python version: confirm compatibility with project's pinned Python 3.13.
  - `uv lock` diff reviewed; transitive dependency footprint reported in PR description.

### 4.6 WebSocket reliability + REST backfill

**Locked:**

**Reconnect/backoff:**
- Exponential backoff: initial 1s, factor 2, jitter ±20%, cap 60s.
- Minimum 3 reconnect attempts before declaring stream unhealthy.
- After unhealthy declaration, instrument enters `degraded` state — known to monitors, no trading.

**Gap detection:**
- On each reconnect, query last persisted **closed** candle per subscribed instrument.
- Compute gap = `now - last_closed_time`.
- If gap > 1 candle interval, trigger REST backfill.

**REST backfill caps (locked defaults, env-overridable):**
- `BINANCE_KLINE_BACKFILL_MAX_CANDLES = 5000` (≈3.5 days of 1m candles; 5 REST requests at 1000/req).
- `BINANCE_KLINE_BACKFILL_MAX_REQUESTS = 10` (hard cap on REST calls per gap-fill attempt).
- `BINANCE_KLINE_BACKFILL_PAGE_SIZE = 1000` (Binance REST max).

**Beyond cap:**
- Instrument transitions to `manual_backfill_required`.
- Persisted state in a new `crypto_instrument_health` table (or equivalent — to be locked in Child B plan).
- Scalping state machine reads instrument health and refuses to trade `manual_backfill_required` or `degraded` instruments until cleared.

**Rate-limit telemetry:**
- Parse Binance REST response headers `X-MBX-USED-WEIGHT-1M` and `X-MBX-ORDER-COUNT-1M`.
- Emit to existing logging/telemetry pipeline (whatever the project uses for KIS/Alpaca rate-limit visibility — to be confirmed in Child B plan).
- Soft-throttle: if used weight > 80% of declared limit, sleep until next minute window.
- Hard-stop: if 429/418 received, instrument enters `rate_limited` state and backs off for `Retry-After` seconds.

**Note on Upbit precedent:** Upbit WS adapter (`app/services/upbit_websocket.py`) has reconnect (10× 5s linear) but **no gap detection and no REST backfill**. Binance adapter is defining a new pattern, not mirroring Upbit. A follow-up to retrofit Upbit with gap detection is reasonable but **out of scope for this epic**.

### 4.7 Binance host allowlist / fail-closed

**Locked: enforce at HTTP transport layer in addition to config flag.**

**Public market data adapter:**
- Allowlist (read): `api.binance.com`, `data-api.binance.vision`, `stream.binance.com`, `data-stream.binance.vision`.
- All signed endpoints (anything requiring API key in headers) are programmatically blocked: the adapter class does not expose `account()`, `order()`, etc., and the underlying `httpx.Client` is constructed with an event hook that rejects requests carrying an API-key header.

**Testnet execution adapter:**
- Allowlist (testnet only): `testnet.binance.vision`, `stream.testnet.binance.vision`, `testnet.binancefuture.com` (if futures).
- `__init__` validates the configured `base_url` and any SDK-default URL **before** the first request; injection of a live host (`api.binance.com`, etc.) raises `BinanceLiveHostBlocked` at construction.
- `httpx.Client` event hook re-validates per-request host post-redirect-resolution; redirects to non-allowlisted hosts raise the same error.
- Unit tests cover:
  - Construct adapter with `base_url='https://api.binance.com'` → raises.
  - Construct adapter with env `BINANCE_TESTNET_BASE_URL=https://api.binance.com` → raises.
  - Mock a 302 redirect from testnet to live host → request raises.

**Why not mirror existing patterns:** KIS uses an `_KISSettingsView` config-layer flag and Alpaca uses explicit `base_url` injection. Neither has a transport-layer guard. We are introducing a stricter pattern here because the cost of accidentally routing a Binance order to live is much higher than the equivalent for KIS sandbox (which is feature-limited) or Alpaca paper (which is not real money). This pattern can be retrofitted to KIS/Alpaca in a follow-up if desired.

### 4.8 Testnet ledger

**Locked: new dedicated `binance_testnet_order_ledger` table + `BinanceTestnetLedgerService`, mirroring AlpacaPaperLedgerService shape.**

**Why separate from `alpaca_paper_order_ledger`:** Different venue, different lifecycle vocabulary (futures reduce-only, OCO availability differs), different telemetry/runbook ownership. Sharing would create a multi-venue ledger that's harder to reason about per-venue and makes per-venue rollouts riskier (resolves "separate vs shared ledger" decision from review).

**Service shape (mirrors AlpacaPaperLedgerService 11-method pattern, names provisional):**
- `record_plan` / `record_preview` / `record_validation_attempt` / `record_submit` / `record_status` / `record_cancel` / `record_position_snapshot` / `record_tp_sl_armed` / `record_tp_sl_triggered` / `record_close` / `record_reconcile`
- State vocab: `planned → previewed → validated → submitted → filled → tp_sl_armed → tp_sl_triggered → closed → reconciled` + `anomaly`.

**Service-only write rule:**
- All writes via `BinanceTestnetLedgerService`. Repository class is service-internal (no import outside the service module).
- Documented in CLAUDE.md (new section, parallel to existing "Alpaca Paper 실행 레저" entry).
- Test in Child C asserts: importing `BinanceTestnetLedgerRepository` from outside `app/services/brokers/binance/` raises `ImportError` (via a lint check or a runtime guard — exact mechanism locked in Child C plan).
- No DB-level triggers added; convention + tests follow AlpacaPaperLedgerService precedent. (If a future audit shows convention is insufficient, a DB-level write guard can be added in a follow-up.)

**Reduce-only enforcement (if futures path is implemented):**
- Exit orders for futures positions must set `reduceOnly=True`. Validated at submit time by the testnet execution adapter, not by the ledger.
- Ledger records the `reduceOnly` flag for audit.

### 4.9 ROB-282 screener interface

**Locked: screener boundary is the snapshot builder job, not direct candle queries.**

**Current reality:**
- The crypto screener UI consumes `invest_crypto_screener_snapshots`, a pre-computed snapshot table.
- The snapshot builder job (`app/jobs/invest_crypto_screener_snapshots.py`) is what reads candle data, and that's where the new instrument-FK schema lands.

**Therefore:**
- ROB-282's UI/API contract does not need changes from ROB-283.
- The snapshot builder job needs to migrate to query `crypto_candles_1d_view` (or the underlying `crypto_candles_1d` + `crypto_instruments` JOIN) instead of `crypto_candles_1d`'s old `(symbol, market)` columns.
- This migration is **in scope for Child A** (as a read-side update inside Child A's PR), because Child A is the one breaking the column contract.
- The snapshot table's own columns (`symbol`, `source='tvscreener_upbit'`, etc.) remain unchanged.

**For future venue expansion (Binance USDT crypto in the screener):** a follow-up issue will add a Binance-aware snapshot builder. That is **out of scope for this epic.**

---

## 5. Open items (still requires decision)

These items are intentionally not locked in this plan and must be resolved before or during the relevant child plan.

| # | Item | Owner | Resolve by |
|---|------|-------|------------|
| 1 | Exact pinned versions of `binance-sdk-spot` (and futures SDK if applicable) — locked in Child B/C plan PR after `uv lock` dry-run. | Child B plan author | Child B plan PR |
| 2 | Whether `crypto_candles_1m` and `crypto_candles_1d` share a single SQLAlchemy mixin/base or stay independent classes. Lean: shared mixin, finalize in Child A plan. | Child A plan author | Child A plan PR |
| 3 | Whether `crypto_instrument_health` is a real table or in-memory + Redis. Lean: table, for audit and durability across restarts. Finalize in Child B plan. | Child B plan author | Child B plan PR |
| 4 | Exact telemetry sink for Binance rate-limit headers (Sentry tags? structured log? new metric?). Confirm what KIS/Alpaca currently use, mirror that. | Child B plan author | Child B plan PR |
| 5 | Whether Child C ships spot-only or spot+futures testnet. Lean: spot-only for first PR, futures as a follow-up child. | Child C plan author | Child C plan PR |
| 6 | Discord vs Hermes notification surface for testnet scalper state transitions. Default: log + Sentry only, no chat notification in MVP. Revisit after Child C lands. | Child C plan author | Child C plan PR |
| 7 | Whether the import-guard test for `BinanceTestnetLedgerRepository` is enforced via Ruff custom rule, a `__init__` runtime check, or a pytest module-load test. | Child C plan author | Child C plan PR |
| 8 | Pre-migration backup mechanism for `crypto_candles_1d` (CREATE TABLE AS vs pg_dump). Lean: CREATE TABLE AS for atomicity. | Child A plan author | Child A plan PR |
| 9 | Whether Upbit WS gap-detection retrofit is opened as a separate issue now or after Binance pattern lands. Lean: file the issue now, prioritize after. | This plan reviewer | Before Child B starts |
| 10 | Whether the `crypto_candles_1d_view` is materialized or a plain view. Lean: plain view (cheap JOIN, instruments is small). | Child A plan author | Child A plan PR |

---

## 6. Verification / handoff for this plan PR

This is a plan-only PR. The only artifact is this document. Verification scope:

- [ ] This document is at `docs/plans/ROB-283-binance-candles-testnet-scalping-plan.md`.
- [ ] No code, no migrations, no scheduler entries, no broker/order/watch/order-intent mutation in the diff.
- [ ] Linear comment posted on ROB-283 linking to this plan and listing the proposed child issues.
- [ ] Reviewer sign-off on §4 decisions before any child issue is opened.

After approval:
1. Open three Linear child issues (Schema, Market Data, Testnet Scalping) under ROB-283 parent.
2. Draft per-child implementation plans (TDD task lists) — each child gets its own `docs/plans/ROB-N-*-plan.md`.
3. Route each child plan through review separately.
4. Implement children one at a time, in dependency order (A → B → C).

---

## Appendix A — Ground-truth facts confirmed during plan research

| Fact | Source |
|---|---|
| `crypto_candles_1d` schema is `(time, symbol, market, OHLCV, value, source, ingested_at)` with unique `(time, symbol, market)`, no SQLAlchemy model class. | `alembic/versions/f974ac12e573_add_crypto_candles_1d.py` |
| Repository: `DailyCandlesRepository.upsert_rows(market, rows)` writes via partition `market` column. | `app/services/daily_candles/repository.py:20-32` |
| Timescale is **required** (migration validates ≥2.15.0). `crypto_candles_1d` is a hypertable with 90-day chunks. Same for `kr_candles_1d`, `us_candles_1d`. | `alembic/versions/f974ac12e573_*.py:23-84` |
| No continuous aggregates exist yet for crypto candles. | (search) |
| AlpacaPaperLedgerService has 11 `record_*` methods. State vocab: `planned → previewed → validated → submitted → filled → position_reconciled → sell_validated → closed → final_reconciled`, plus `anomaly`. Service-only-write enforced by module docstring + ORM-only writes + tests; no DB triggers. | `app/services/alpaca_paper_ledger_service.py`, `docs/runbooks/alpaca-paper-ledger.md` |
| Upbit WS has reconnect (10× 5s) but **no gap detection, no REST backfill, no `is_closed` distinction**. Binance is not mirroring this — it must add what Upbit lacks. | `app/services/upbit_websocket.py:48-105, 204-226` |
| Crypto screener queries `invest_crypto_screener_snapshots` (pre-computed), not `crypto_candles_1d` directly. Schema change affects the **snapshot builder job**, not the screener UI/API. | `app/services/invest_crypto_screener_snapshots/repository.py:72-100` |
| KIS uses config-layer host switching (`_KISSettingsView.is_mock`). Alpaca uses explicit `base_url` injection at init. **Neither has transport-layer (httpx event hook) enforcement.** Binance is introducing a new pattern. | `app/services/brokers/kis/client.py:43-80`, `app/services/brokers/alpaca/transport.py:16`, `app/services/brokers/alpaca/config.py:11-24` |
| Crypto execution mapping (`Upbit → Alpaca paper`) exists but is unrelated to Binance routing. | `app/services/crypto_execution_mapping.py:69-80` |
| Plans convention: `docs/plans/ROB-N-<topic>-plan.md` (e.g., `ROB-28-kis-mock-routing-plan.md`) coexists with dated `YYYY-MM-DD-*.md` files. AOE metadata block at top is conventional. | `docs/plans/` listing |

---

## Appendix B — Child issue acceptance criteria templates

Use these as starting drafts when filing the three child issues. Refine in each child's implementation plan.

### Child A: Schema & instrument master

**Title:** `auto_trader: crypto_instruments master + venue-aware candle schema (ROB-283-a)`

**Acceptance:**
- [ ] `crypto_instruments` table created with unique `(venue, product, venue_symbol)` and all metadata columns.
- [ ] `crypto_candles_1d` migrated to instrument-FK shape; legacy Upbit rows backfilled 100%.
- [ ] `crypto_candles_1m` created with same shape, Timescale hypertable, 1-day chunks.
- [ ] CHECK constraints enforce OHLC sanity, non-negative volumes, status enum.
- [ ] `crypto_candles_1d_view` exposes legacy `symbol`/`market` shape for backwards compatibility.
- [ ] `DailyCandlesRepository` and new `MinuteCandlesRepository` write via `instrument_id`.
- [ ] Snapshot builder job (`app/jobs/invest_crypto_screener_snapshots.py`) reads via new view; ROB-282 screener UI/API unchanged.
- [ ] Idempotent upsert tests; cross-venue same-bucket coexistence test.
- [ ] Pre-migration backup runbook step documented.
- [ ] Downgrade migration or documented rollback procedure.
- [ ] No broker/order/watch/order-intent mutation in PR.

### Child B: Binance public market data adapter

**Title:** `auto_trader: Binance public market data adapter (REST + WS, read-only) (ROB-283-b)`

**Acceptance:**
- [ ] `app/services/brokers/binance/` package with public REST + WS classes.
- [ ] `binance-sdk-spot` added to `uv lock`; license/maintenance/Python-version vetted in PR description.
- [ ] Signed endpoints unreachable from public adapter (host allowlist + API-key header rejection).
- [ ] WS reconnect (exponential 1s→60s, jitter, ≥3 attempts) + gap detection + REST backfill within caps.
- [ ] `manual_backfill_required` state persisted for gaps beyond cap.
- [ ] Rate-limit headers parsed and telemetry-emitted; soft-throttle at 80% weight, hard-stop on 429/418.
- [ ] Public CLI smoke `python -m scripts.binance_public_smoke` runs with no API key.
- [ ] Closed candles idempotently upserted to `crypto_candles_1m`.
- [ ] Test: live host injected via env override → init raises.
- [ ] Runbook `docs/runbooks/binance-public-market-data.md`.
- [ ] No scheduler activation. No broker/order/watch/order-intent mutation.

### Child C: Binance testnet scalping MVP

**Title:** `auto_trader: Binance testnet scalping MVP (deterministic state machine + ledger) (ROB-283-c)`

**Acceptance:**
- [ ] `binance_testnet_order_ledger` table + `BinanceTestnetLedgerService` with 11+ `record_*` methods.
- [ ] Direct SQL writes against ledger blocked (import-guard test or runtime check).
- [ ] Transport-layer host allowlist enforces testnet-only hosts; live host injection raises at init.
- [ ] `BINANCE_TESTNET_ENABLED=true` + testnet credentials + per-call `confirm=True` all required for live submit.
- [ ] Default mode `dry_run=true`; preview is a no-op.
- [ ] Deterministic state machine (entry/TP/SL) runs in shadow mode against Binance feed from Child B.
- [ ] Reduce-only enforced on futures exit (if futures included).
- [ ] Max symbols `BTCUSDT, ETHUSDT, SOLUSDT`. Max one open testnet position per symbol. Default max notional 10 USDT.
- [ ] CLAUDE.md updated with new ledger section (parallel to Alpaca Paper ledger section).
- [ ] Runbook `docs/runbooks/binance-testnet-scalping.md` covers env vars, testnet quirks (reset, OCO availability), manual close procedure.
- [ ] Tests: no live-endpoint routing, ledger state transitions, fail-closed on missing credentials/flags.
- [ ] No scheduler activation. No KR/US/Upbit/Alpaca live execution changes.
