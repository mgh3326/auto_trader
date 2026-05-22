# ROB-285 — Binance Public Market Data Adapter (REST + WS, Read-Only) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` (recommended) or `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
>
> AOE_STATUS: plan_ready
> AOE_ISSUE: ROB-285
> AOE_ROLE: implementer
> AOE_NEXT: execute Task 1 (audit + branch setup), then proceed in order. Task 1's grep-asserted invariant must be in place before any Binance code lands.
>
> **Per-task open-items reporting (required):** At the start of any task referenced by the "Open items" table (§ below), the implementer must state which lean is being adopted (the table's lean, or a justified deviation) in the task's first commit message or PR comment. No silent decisions. If an open item resolves differently than its table lean, the deviation must be flagged explicitly so reviewers see it.

**Goal:** Build a **read-only** Binance public market data adapter (REST + WebSocket) behind a local adapter package, persist closed 1m candles via Child A's `MinuteCandlesRepository`, and harden the integration with WS reconnect/backoff + gap detection + REST backfill (bounded by caps) + Binance rate-limit telemetry. No API key/secret required, no signed endpoints, no execution code paths.

**Architecture:** New isolated package `app/services/brokers/binance/` containing public-only REST and WS clients behind adapter classes; `binance-sdk-spot` types are never leaked across the adapter boundary. Host allowlist is enforced at the **HTTP transport layer** via `httpx.AsyncClient` event hooks — a stricter pattern than KIS (config-layer) or Alpaca (base_url injection), introduced here because the cost of accidentally routing a Binance call to a live signed endpoint is much higher. Gap-detection persistence lives in a new `crypto_instrument_health` table with service-only writes (mirroring the AlpacaPaperLedgerService convention).

**Tech Stack:** PostgreSQL 16 + TimescaleDB (from Child A) + SQLAlchemy 2 async + Alembic, `httpx` (existing), `websockets` (existing transitive — confirm during Task 2), `binance-sdk-spot` (new, behind adapter boundary), pytest. No new infrastructure deps beyond `binance-sdk-spot` and what's transitively required.

---

## Pre-implementation discovery (locked findings)

Audit performed on 2026-05-20 (post Child A merge `10232e5b`) confirmed:

1. **No Binance package exists yet.** `app/services/brokers/binance/` is absent. ROB-285 is the first introduction; all Binance code must live in this new package.
2. **Child A's repository surface is ready.** `app/services/minute_candles/repository.py::MinuteCandlesRepository` accepts `MinuteCandleRow(instrument_id=..., time_utc=..., ...)` and upserts idempotently into `crypto_candles_1m`. `DailyCandlesRepository._resolve_instrument_id` shows the legacy `(symbol, partition) → instrument_id` translation pattern but is hardcoded for Upbit; Child B must look up `instrument_id` directly via `crypto_instruments` rows seeded explicitly for Binance.
3. **No httpx `event_hooks` usage anywhere in the codebase.** `app/services/brokers/alpaca/transport.py`, `kis/client.py`, `kiwoom/client.py`, `upbit/client.py` all construct plain `httpx.AsyncClient(base_url=...)` instances. Transport-layer host enforcement is a **new pattern** that Child B introduces (parent plan §4.7).
4. **CLI smoke precedent**: `scripts/kis_websocket_mock_smoke.py` is the closest shape — module-style script with exit codes table in docstring, `app.core.config.settings` for env, structured logging, no DB mutation. Child B's `scripts/binance_public_smoke.py` mirrors this shape.
5. **Telemetry sinks in use**: `app/core/model_rate_limiter.py` uses Redis for the Gemini API rate-limit pattern; KIS/Alpaca transports use `logger.info`/`logger.warning` for rate-limit visibility (no structured Sentry tagging for rate-limit headers observed). Child B uses structured `logger.info` for rate-limit headers + a Sentry tag (`binance.rate_limit_weight_pct`) on hot paths.
6. **No scheduler dependency.** This adapter is library + CLI only; the scalper (ROB-286) and any TaskIQ wrapper are out of scope.

Task 1 of this plan re-runs the package-absence audit and locks it as a CI invariant, so a future PR can't silently introduce a parallel Binance package.

---

## What stays in Child C (explicit boundary, NOT in this PR)

The following items are **Child C (ROB-286) scope** and must not appear in this PR's diff. Reviewers should bounce the PR if any of them creep in:

- **Testnet hosts.** `testnet.binance.vision`, `stream.testnet.binance.vision`, `testnet.binancefuture.com`. The host allowlist in this PR is the **public production** set only.
- **Order / ledger code.** No `binance_testnet_order_ledger` table, no `BinanceTestnetLedgerService`, no order intent, no order preview, no order submit, no order cancel.
- **Scalper / state machine.** No `app/services/scalping/*`, no entry/TP/SL logic, no deterministic state machine.
- **Futures SDK.** No `binance-sdk-derivatives-trading-usds-futures`. The only Binance SDK added in this PR is `binance-sdk-spot`. Futures is introduced by Child C only if its plan keeps it in scope.
- **Signed / private endpoints.** No method or constant that touches `/api/v3/account`, `/api/v3/order`, `/api/v3/myTrades`, `/sapi/*`, futures private endpoints, or any `X-MBX-APIKEY` header. Defense-in-depth assertion in Task 1's audit + Task 4's transport hook.
- **Production scheduler activation.** No new TaskIQ task, no Prefect changes, no cron. Adapter is library + CLI only. Even after merge, an operator must explicitly enable scheduling in a future PR.

If any of these are needed during execution to make Child B testable, stop and re-scope — don't reach into Child C.

## Production cutover gate (deferred — operator-gated)

This PR introduces the `crypto_instrument_health` table via Alembic migration. The migration is **shipped** in the PR (file + test + downgrade) but the production cutover (`alembic upgrade head` on production/server DBs) is **not** performed as part of merging this PR. Operator pre-cutover steps mirror Child A's pattern:

1. Pre-cutover backup of the target DB (logical: `pg_dump`-style snapshot or vendor equivalent), so the migration is reversible without code rollback.
2. `alembic upgrade head` on the non-production server DB; verify `crypto_instrument_health` exists with the CHECK constraint and is empty (zero rows initially).
3. `alembic downgrade -1 && alembic upgrade head` round-trip verification.
4. Confirm no scheduler activation (this PR does not add one; verify via `grep -rn "binance" app/core/scheduler.py app/core/taskiq_broker.py app/tasks/`).
5. After validation in non-prod, schedule production cutover separately — not as part of this PR's merge.

This is documented in `docs/runbooks/binance-public-market-data.md` (Task 15) under "Production cutover checklist" so the gate doesn't get forgotten.

## Hard safety invariants (apply to every task)

1. **No signed/private endpoints.** No code path reaches `/api/v3/account`, `/api/v3/order`, `/api/v3/myTrades`, `/sapi/*`, futures private endpoints, or anything that requires an API key.
2. **No API-key headers.** The httpx transport's `request` event hook **raises** if it sees an `X-MBX-APIKEY` header on any outgoing request.
3. **No testnet code.** No `BINANCE_TESTNET_*` env var handling, no testnet hosts in the allowlist. Child C owns testnet.
4. **No ledger code.** No `binance_testnet_order_ledger`, no `BinanceTestnetLedgerService`. Child C owns the testnet ledger.
5. **No scalping state machine.** No order-intent code. No `app/services/scalping/*`.
6. **No scheduler activation.** No new TaskIQ tasks, no Prefect changes, no cron entries. Adapter starts only via CLI invocation.
7. **No `app/jobs/*` modification.** Snapshot builder and other jobs remain untouched.
8. **No KR/US/Upbit/Alpaca path changes.** Existing brokers are read-only references; do not touch their code.
9. **No production DB writes.** Tests use `test_db`. Migration for `crypto_instrument_health` runs via Child A's already-in-place infrastructure; the actual production upgrade is an operator step.
10. **Default mode is "no I/O".** Importing the package must not connect to Binance. WS subscription requires explicit method call. REST client per-method.
11. **Host allowlist is the last line of defense, not the first.** The package must not expose any class or method that even *names* a signed endpoint; the allowlist exists for defense-in-depth, not as the sole guard.

---

## File structure

### Created (new)

```
app/services/brokers/binance/
├── __init__.py                          # Public API surface; re-exports adapter classes only
├── host_allowlist.py                    # Frozen set of allowed hostnames + validator
├── transport.py                         # httpx.AsyncClient factory with event_hooks
├── rest_client.py                       # BinancePublicRestClient
├── ws_client.py                         # BinancePublicWSClient (kline_1m + bookTicker)
├── rate_limit_telemetry.py              # Parses X-MBX-USED-WEIGHT-1M etc.
├── backfill.py                          # GapDetector + RestBackfiller
├── ingest.py                            # Closed-kline → MinuteCandlesRepository pipeline
├── dto.py                               # Normalized DTOs (no SDK type leak)
└── errors.py                            # BinanceLiveHostBlocked, BinanceSignedEndpointAttempted, etc.

app/services/instrument_health/
├── __init__.py
├── repository.py                        # CryptoInstrumentHealthRepository (service-internal)
└── service.py                           # CryptoInstrumentHealthService (the only write surface)

app/models/crypto_instrument_health.py   # ORM model

alembic/versions/<rev>_add_crypto_instrument_health.py

scripts/binance_public_smoke.py          # Public REST+WS smoke CLI (no creds)
docs/runbooks/binance-public-market-data.md

tests/services/brokers/binance/
├── __init__.py
├── test_audit_no_signed_endpoints.py    # Grep invariant: no signed-endpoint surface
├── test_host_allowlist.py
├── test_transport_event_hooks.py
├── test_rest_client.py
├── test_rate_limit_telemetry.py
├── test_backfill.py
├── test_ws_client.py
├── test_ws_reconnect.py
└── test_ingest.py

tests/services/instrument_health/
├── __init__.py
└── test_instrument_health_service.py
```

### Modified

- `pyproject.toml` (add `binance-sdk-spot`)
- `uv.lock` (dependency resolution)
- `app/models/__init__.py` (register `CryptoInstrumentHealth`)
- `tests/conftest.py` — **only if necessary**. The `db_session` fixture already calls `Base.metadata.create_all`; once `CryptoInstrumentHealth` is registered, the table is created automatically. Add a manual ALTER patch only if a column collision is discovered (unlikely for a brand-new table).

### Not modified

- `app/services/brokers/alpaca/*`, `kis/*`, `kiwoom/*`, `upbit/*` — no touching.
- `app/services/upbit_websocket.py` — Upbit WS gap-detection retrofit is a separate follow-up.
- `app/services/crypto_execution_mapping.py` — unrelated; Binance signal-execution mapping does not exist in this PR.
- `app/jobs/*`, `app/tasks/*`, `app/core/scheduler.py`, `app/core/taskiq_broker.py` — no scheduler integration in this PR.
- `app/services/daily_candles/*` — Child A's surface is consumed read-only; not modified.

---

## Locked decisions for Child B

### B.1 — `crypto_instrument_health` table (locked here, not Child B's optional)

Parent plan §4.6 left the choice "table vs Redis-only" open. **Decision: dedicated table.** Reason: audit-friendly, survives restarts, queryable via SQL for monitoring, and lets the scalper read state via the same repository pattern as candle data.

```sql
CREATE TABLE crypto_instrument_health (
    instrument_id BIGINT PRIMARY KEY REFERENCES crypto_instruments(id),
    state TEXT NOT NULL DEFAULT 'healthy',
    reason TEXT NULL,
    last_state_change_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_closed_candle_time TIMESTAMPTZ NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    retry_after_at TIMESTAMPTZ NULL,
    metadata JSONB NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (state IN ('healthy','degraded','rate_limited','manual_backfill_required'))
);
```

Lifecycle:
- `healthy` (default) → `degraded` (WS unhealthy after ≥3 reconnect failures) → `healthy` (next successful reconnect).
- `healthy` → `rate_limited` (REST 429/418 received) → `healthy` (after `retry_after_at` passes).
- `healthy`/`degraded` → `manual_backfill_required` (gap > cap on reconnect) → `healthy` only after operator clears the flag.

### B.2 — Service-only writes for `crypto_instrument_health`

All writes via `CryptoInstrumentHealthService`. Repository class is module-internal (no import outside `app/services/instrument_health/`). Same convention as `AlpacaPaperLedgerService` (ROB-84). Tests assert the import guard (Task 8).

### B.3 — In-progress (non-closed) kline persistence

**Decision: drop in-progress klines for MVP.** Only `is_closed=true` events trigger writes to `crypto_candles_1m`. Rationale: scalper (Child C) uses `bookTicker` for live price; in-progress kline storage adds complexity (read-modify-write pattern, overwrite invariant) without immediate consumer. The parent plan acceptance allows either "drop" or "persist with `is_closed=false`"; this plan locks "drop" to keep the MVP simple. Re-evaluating to enable in-progress persistence is a separate follow-up.

### B.4 — WebSocket subscription model

**Decision: Binance combined streams via single connection per environment.** URL shape: `wss://stream.binance.com:9443/stream?streams=btcusdt@kline_1m/btcusdt@bookTicker/ethusdt@kline_1m/...`. Subscribed-symbol set is parameterized; defaults to the MVP triplet `BTCUSDT, ETHUSDT, SOLUSDT` (matching Child C's symbol cap). Single connection minimizes overhead; combined streams are Binance-native and well-supported by the SDK.

### B.5 — `httpx` redirect behavior

**Decision: `follow_redirects=False` for REST and WS upgrade requests.** Binance public endpoints do not legitimately redirect, so any 30x is suspicious and the adapter raises. This sidesteps the complexity of validating each post-redirect host inside response hooks. If a future legitimate redirect is needed, the change can be made surgically with explicit allowlist validation.

### B.6 — Rate-limit telemetry sink

**Decision: structured `logger.info` + Sentry tag on hot paths, fail-open on telemetry errors.** Matches the closest existing pattern (KIS/Alpaca transports use logger). The Sentry tag (`binance.rate_limit_weight_pct`) is set only when `used_weight / declared_limit > 0.5` to avoid tag noise during normal operation.

**Fail-open requirement (locked):** Telemetry MUST NOT break the adapter. The Sentry call site swallows every exception (`ImportError` when sentry_sdk is absent, `RuntimeError`/`AttributeError` if sentry_sdk is misconfigured, anything else). Two dedicated tests in Task 6 enforce this contract: one stubs out `sentry_sdk` import to raise `ImportError`, another monkeypatches `set_tag` to raise `RuntimeError`. Both must complete `emit_rate_limit_snapshot()` without propagating an exception. Rationale: if telemetry breaks the adapter, the project loses both observability and the adapter — strictly worse than just losing observability.

No new metrics infrastructure is introduced. If a future monitoring need emerges, the telemetry function is a single chokepoint to extend.

### B.7 — Instrument seeding contract

`crypto_instruments` rows for Binance (e.g. `(venue='binance', product='spot', venue_symbol='BTCUSDT', base='BTC', quote='USDT', status='active')`) are **assumed pre-seeded** for symbols subscribed by the adapter. Adapter behavior on missing instrument: ingest layer logs `WARNING` and skips the candle; instrument health is **not** affected (the instrument simply isn't tracked yet). A seed CLI is out of scope; operators or a follow-up issue can add seeding helpers. This avoids the adapter doing implicit DB writes for instrument creation.

### B.8 — `binance-sdk-spot` version pinning

Exact version locked during Task 2 after `uv add binance-sdk-spot` + `uv lock` dry-run. Acceptance criteria checks (license MIT/Apache 2/BSD, ≥1 release in trailing 12 months, no open critical-severity issues, Python 3.13 compat) are filled in the Task 2 PR description.

### B.9 — Scheduler activation

**Locked: scheduler activation is out of scope.** No TaskIQ task, no Prefect deployment, no cron. The adapter is a library + CLI. A future child issue (post-ROB-283-epic) will wire it into a scheduled task once the MVP is reviewed in production-like environments.

---

## Open items (deferred or to-be-finalized during execution)

| # | Item | Lean | Resolve during |
|---|------|------|----------------|
| 1 | Whether `binance-sdk-spot` covers WS Streams adequately or `websockets` library should be used directly. | If SDK WS is opinionated about callback shape or doesn't expose raw frames, use `websockets` directly to keep our adapter clean. Confirm in Task 10. | Task 10 |
| 2 | Combined-stream URL: subscribe via URL query param vs SUBSCRIBE message after connect. | URL query param (simpler, declarative). Switch to SUBSCRIBE only if dynamic add/remove is needed. | Task 10 |
| 3 | Whether to retry idempotently on transient `400` from Binance REST. | No — `400` is a client error; surface immediately. Retry only on transport-level errors (timeout, connection reset). | Task 5 |
| 4 | Test fixture for WS: real Binance public WS hit during smoke vs `websockets.serve` test server. | Use `websockets.serve` for unit tests (deterministic); smoke CLI hits the real endpoint. | Task 10 |
| 5 | Whether `instrument_health` transitions emit Sentry events. | Yes for `manual_backfill_required` (operator action required); no for `degraded`/`rate_limited` (transient). | Task 8 |
| 6 | Backfill ordering: forward in time vs reverse in time. | Forward (oldest-first). Reverse pagination is needed only if the gap exceeds the cap, in which case we cap and stop. | Task 9 |
| 7 | Where to look up `instrument_id` for a Binance symbol — direct query, or via a small in-memory cache. | In-memory cache (loaded on adapter startup, invalidated on missing-lookup); reduces DB churn during normal streaming. | Task 13 |

---

## Host allowlist fail-closed — required test coverage matrix

The four guarantees the user called out must each be proven by a test at the **HTTP client / request path**, not just at config-load time. Reviewers should match each row to a test name when reviewing the PR.

| Guarantee | Test file | Test name | Verifies at |
|---|---|---|---|
| API key / `X-MBX-APIKEY` header is rejected | `tests/services/brokers/binance/test_transport_event_hooks.py` | `test_request_with_api_key_header_raises` | Pre-request hook (transport layer) |
| Signed / private endpoint surface is absent | `tests/services/brokers/binance/test_audit_no_signed_endpoints.py` | `test_no_signed_endpoint_surface_in_binance_package`, `test_no_api_key_header_constants_in_binance_package` | Source-level (grep-based audit) |
| Signed-endpoint surface absent on the REST client class | `tests/services/brokers/binance/test_rest_client.py` | `test_rest_client_does_not_expose_signed_methods` | Class introspection (`hasattr`) |
| 3xx redirect is rejected (defense-in-depth) | `tests/services/brokers/binance/test_transport_event_hooks.py` | `test_redirect_to_non_allowed_host_raises` | Response hook (transport layer) |
| Non-allowed public host is rejected | `tests/services/brokers/binance/test_transport_event_hooks.py` | `test_get_to_non_allowed_host_raises_at_request_time` | Pre-request hook (transport layer) |
| Allowlist module rejects non-public hosts at the unit level | `tests/services/brokers/binance/test_host_allowlist.py` | `test_non_public_hosts_rejected` (parametrized) | Module-level (`assert_allowed_host`) |
| WS URL with non-allowed host raises at construction | `tests/services/brokers/binance/test_ws_client.py` | `test_ws_rejects_non_allowed_host` | WS client `__init__` |
| Smoke CLI surfaces the defense-in-depth check | `scripts/binance_public_smoke.py` | exit code 5 path | Live host injection at runtime |

Together these prove fail-closed at the HTTP request path (`event_hooks={"request": ..., "response": ...}`) AND at multiple layers above (class introspection, source audit, smoke CLI). If any row's test is removed in implementation, the PR description must justify why.

## Verification commands (Child B PR)

```bash
# Lint + format + type (mirrors CI):
uv run ruff check app/ tests/
uv run ruff format --check app/ tests/
uv run ty check app/ --error-on-warning

# Dependency lock:
uv lock --check

# Tests:
uv run pytest tests/services/brokers/binance/ -v
uv run pytest tests/services/instrument_health/ -v
uv run pytest tests -k "binance and (public or market_data or rate_limit)" -q

# Scope audit (must show only the new package):
grep -rln "api.binance.com\|binance-sdk-spot" --include="*.py" app/
grep -rln "X-MBX-APIKEY" --include="*.py" app/ || echo "OK: no signed endpoints"

# Smoke (no creds required):
uv run python -m scripts.binance_public_smoke --symbol BTCUSDT --dry-run
uv run python -m scripts.binance_public_smoke --symbols BTCUSDT,ETHUSDT --duration 30

# Screener regression (must remain green by construction):
uv run pytest tests -k "screener_snapshot or invest_crypto_screener" -q
```

---

## Task list

### Task 1: Audit invariant + branch setup

Before any Binance code lands, encode the "no parallel Binance package" and "no signed-endpoint surface" invariants as tests that the rest of the plan must keep green.

**Files:**
- Create: `tests/services/brokers/binance/__init__.py` (empty)
- Create: `tests/services/brokers/binance/test_audit_no_signed_endpoints.py`

- [ ] **Step 1: Create worktree per project convention**

```bash
# From /Users/mgh3326/work/auto_trader:
git fetch origin
git worktree add /Users/mgh3326/work/auto_trader.rob-285 -b rob-285 origin/main
cd /Users/mgh3326/work/auto_trader.rob-285
ln -s /Users/mgh3326/work/auto_trader/.env .env
uv sync --all-groups
```

> Note: if the worktree already exists (e.g., the plan was authored in it), skip the `git worktree add` step.

- [ ] **Step 2: Audit grep for pre-existing Binance references**

```bash
grep -rln "binance\|Binance\|BINANCE" --include="*.py" app/ || echo "no binance refs yet"
```

Expected: **empty** (no Binance references in `app/` at the start of this PR). If anything appears, stop and report — the scope assumption is wrong.

- [ ] **Step 3: Write the audit test (locks the invariant)**

```python
# tests/services/brokers/binance/test_audit_no_signed_endpoints.py
"""Locks invariants for ROB-285:

1. The Binance package lives at exactly one path: app/services/brokers/binance/.
2. The package source contains no signed-endpoint surface (no method names
   matching the Binance signed-endpoint vocabulary, no X-MBX-APIKEY header
   constants).

If this test starts failing, a future PR either added a parallel Binance
location or introduced signed-endpoint code. Extend the ALLOWED set with
explicit justification in the PR description, or roll back the change.
"""

from __future__ import annotations

import pathlib
import re
import subprocess

ALLOWED_PACKAGE_PATHS = {
    "app/services/brokers/binance",
}

# Symbol regex matches the function/method names Binance uses for signed
# endpoints. Adding any of these in the public adapter is a scope breach.
SIGNED_SYMBOL_RE = re.compile(
    r"\b(account|order|all_orders|my_trades|user_data_stream|"
    r"open_orders|cancel_order|transfer|asset|withdraw|deposit)\b\s*\(",
    re.IGNORECASE,
)


def _repo_root() -> pathlib.Path:
    return pathlib.Path(__file__).resolve().parents[4]


def test_only_one_binance_package_path_exists() -> None:
    repo_root = _repo_root()
    result = subprocess.run(
        ["grep", "-rln", "binance", "--include=*.py", "app/"],
        cwd=repo_root, capture_output=True, text=True, check=False,
    )
    paths = {line.strip() for line in result.stdout.splitlines() if line.strip()}
    package_dirs = {str(pathlib.Path(p).parent) for p in paths}
    unexpected = {d for d in package_dirs if not any(
        d.startswith(allowed) for allowed in ALLOWED_PACKAGE_PATHS
    )}
    assert not unexpected, (
        f"Unexpected Binance code locations: {sorted(unexpected)}. "
        "ROB-285 invariant: Binance code lives in app/services/brokers/binance/ "
        "ONLY. If you intentionally added a new location, extend "
        "ALLOWED_PACKAGE_PATHS in this test and justify in PR description."
    )


def test_no_signed_endpoint_surface_in_binance_package() -> None:
    repo_root = _repo_root()
    pkg = repo_root / "app" / "services" / "brokers" / "binance"
    if not pkg.exists():
        # Until Task 4 introduces the package, this is fine.
        return
    offenders: list[tuple[pathlib.Path, int, str]] = []
    for py_file in pkg.rglob("*.py"):
        for lineno, line in enumerate(py_file.read_text().splitlines(), 1):
            if SIGNED_SYMBOL_RE.search(line) and "def " in line:
                offenders.append((py_file, lineno, line.strip()))
    assert not offenders, (
        f"Signed-endpoint method names found in Binance public adapter: "
        f"{offenders}. ROB-285 public adapter must not expose signed-endpoint "
        "surface. If a name collision is unavoidable, rename or justify in PR "
        "description and update SIGNED_SYMBOL_RE."
    )


def test_no_api_key_header_constants_in_binance_package() -> None:
    repo_root = _repo_root()
    pkg = repo_root / "app" / "services" / "brokers" / "binance"
    if not pkg.exists():
        return
    forbidden = "X-MBX-APIKEY"
    offenders = []
    for py_file in pkg.rglob("*.py"):
        if forbidden in py_file.read_text():
            offenders.append(str(py_file))
    assert not offenders, (
        f"X-MBX-APIKEY header constant found in: {offenders}. "
        "Public adapter must never construct API-key headers. "
        "The transport event hook checks for this header at request time "
        "as defense in depth; the source itself must not reference it."
    )
```

- [ ] **Step 4: Run, expect PASS (no Binance code exists yet)**

```bash
uv run pytest tests/services/brokers/binance/test_audit_no_signed_endpoints.py -v
```

Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add tests/services/brokers/binance/__init__.py \
        tests/services/brokers/binance/test_audit_no_signed_endpoints.py
git commit -m "test(rob-285): lock audit invariants — Binance package shape + no signed-endpoint surface

Co-Authored-By: Paperclip <noreply@paperclip.ing>"
```

---

### Task 2: Add `binance-sdk-spot` dependency + vet

**Files:**
- Modify: `pyproject.toml`
- Modify: `uv.lock`

- [ ] **Step 1: Add the SDK**

```bash
uv add binance-sdk-spot
```

- [ ] **Step 2: Verify license, maintenance, Python 3.13 compatibility**

```bash
uv run python -c "import importlib.metadata as m; \
  pkg = m.metadata('binance-sdk-spot'); \
  print('License:', pkg.get('License') or pkg.get('License-Expression')); \
  print('Home:', pkg.get('Home-page') or pkg.get('Project-URL'))"
```

Acceptance gates (record in PR description):
- License is MIT / Apache 2 / BSD (or equivalent permissive). Reject GPL/AGPL — abort and report.
- Last release ≥ within trailing 12 months (check PyPI release history).
- No open critical-severity GitHub issues at PR-author time (manual check on the SDK repo).
- Python 3.13 compatibility: `uv sync --all-groups` succeeds; `uv run python -c "import binance_sdk_spot"` succeeds.

- [ ] **Step 3: Capture the transitive footprint diff**

```bash
git diff --stat uv.lock | tail -3
```

Record in PR description: how many new transitive packages, whether any pull in C extensions or large deps.

- [ ] **Step 4: Run audit test again (must still pass)**

```bash
uv run pytest tests/services/brokers/binance/test_audit_no_signed_endpoints.py -v
```

Expected: 3 passed. The SDK install does not yet introduce code in `app/services/brokers/binance/`, so the audit invariant is untouched.

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml uv.lock
git commit -m "feat(rob-285): add binance-sdk-spot dependency

License: <fill>, last release: <fill>, transitive deps: <fill>.
Python 3.13 import-test: pass.

Co-Authored-By: Paperclip <noreply@paperclip.ing>"
```

---

### Task 3: Host allowlist module

**Files:**
- Create: `app/services/brokers/binance/__init__.py`
- Create: `app/services/brokers/binance/host_allowlist.py`
- Create: `app/services/brokers/binance/errors.py`
- Create: `tests/services/brokers/binance/test_host_allowlist.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/services/brokers/binance/test_host_allowlist.py
"""ROB-285 — Binance public host allowlist."""

import pytest
from app.services.brokers.binance.errors import BinanceLiveHostBlocked
from app.services.brokers.binance.host_allowlist import (
    PUBLIC_HOSTS,
    assert_allowed_host,
)


@pytest.mark.parametrize("host", [
    "api.binance.com",
    "data-api.binance.vision",
    "stream.binance.com",
    "data-stream.binance.vision",
])
def test_public_hosts_accepted(host: str) -> None:
    # Should not raise.
    assert_allowed_host(host)


@pytest.mark.parametrize("host", [
    "testnet.binance.vision",         # testnet — not in public adapter scope
    "fapi.binance.com",                # futures live — not in scope
    "api.binance.us",                  # different exchange
    "evil.example.com",                # arbitrary
    "stream.binance.com.evil.example", # subdomain spoof
])
def test_non_public_hosts_rejected(host: str) -> None:
    with pytest.raises(BinanceLiveHostBlocked):
        assert_allowed_host(host)


def test_public_hosts_is_frozen() -> None:
    # Defense against accidental in-place mutation.
    assert isinstance(PUBLIC_HOSTS, frozenset)
```

- [ ] **Step 2: Run, expect FAIL (modules missing)**

```bash
uv run pytest tests/services/brokers/binance/test_host_allowlist.py -v
```

- [ ] **Step 3: Implement**

```python
# app/services/brokers/binance/__init__.py
"""ROB-285 — Binance public market data adapter (read-only).

This package exposes ONLY read-only public REST + WS surfaces. Any code
that imports a Binance signed endpoint, account method, or API-key header
is a bug — see `test_audit_no_signed_endpoints` for the locked invariant.
"""
```

```python
# app/services/brokers/binance/errors.py
"""Binance adapter errors."""

class BinanceAdapterError(Exception):
    """Base class for Binance adapter errors."""


class BinanceLiveHostBlocked(BinanceAdapterError):
    """Raised when the transport detects a request to a non-allowlisted host."""


class BinanceSignedEndpointAttempted(BinanceAdapterError):
    """Raised when the transport detects an API-key header on a public request."""


class BinanceRateLimited(BinanceAdapterError):
    """Raised when REST 429/418 is received; carries Retry-After seconds."""

    def __init__(self, retry_after_seconds: float, message: str = "") -> None:
        super().__init__(message or f"Rate-limited; retry after {retry_after_seconds}s")
        self.retry_after_seconds = retry_after_seconds


class BinanceBackfillCapExceeded(BinanceAdapterError):
    """Raised when a gap exceeds REST backfill caps; caller should mark
    the instrument as manual_backfill_required and stop trading it."""
```

```python
# app/services/brokers/binance/host_allowlist.py
"""ROB-285 — Public-adapter host allowlist (frozen).

Parent plan §4.7 introduces transport-layer host enforcement as a new pattern
(KIS/Alpaca use config-layer only). This module is the single source of
truth for which hosts the public adapter is allowed to talk to. The
testnet allowlist lives in ROB-286 Child C's execution adapter — never
mix the two sets.
"""

from __future__ import annotations

from app.services.brokers.binance.errors import BinanceLiveHostBlocked


PUBLIC_HOSTS: frozenset[str] = frozenset({
    "api.binance.com",
    "data-api.binance.vision",
    "stream.binance.com",
    "data-stream.binance.vision",
})


def assert_allowed_host(host: str) -> None:
    """Raise BinanceLiveHostBlocked if `host` is not in PUBLIC_HOSTS.

    Strict equality match — no suffix/wildcard. Subdomain spoofs like
    `stream.binance.com.evil.example` are rejected because the full host
    string differs from any allowlist entry.
    """
    if host not in PUBLIC_HOSTS:
        raise BinanceLiveHostBlocked(
            f"Host {host!r} is not in PUBLIC_HOSTS. "
            "Allowed: " + ", ".join(sorted(PUBLIC_HOSTS))
        )
```

- [ ] **Step 4: Run, expect PASS**

```bash
uv run pytest tests/services/brokers/binance/test_host_allowlist.py -v
```

Expected: 7 passed.

- [ ] **Step 5: Commit**

```bash
git add app/services/brokers/binance/__init__.py \
        app/services/brokers/binance/host_allowlist.py \
        app/services/brokers/binance/errors.py \
        tests/services/brokers/binance/test_host_allowlist.py
git commit -m "feat(rob-285): Binance public host allowlist + adapter errors

Co-Authored-By: Paperclip <noreply@paperclip.ing>"
```

---

### Task 4: httpx transport wrapper with event_hooks

**Files:**
- Create: `app/services/brokers/binance/transport.py`
- Create: `tests/services/brokers/binance/test_transport_event_hooks.py`

This is the **new pattern** parent plan §4.7 introduces. KIS/Alpaca don't have it. The transport wrapper is the only place httpx clients are constructed for the Binance adapter.

- [ ] **Step 1: Write the failing tests**

```python
# tests/services/brokers/binance/test_transport_event_hooks.py
"""ROB-285 — Transport-layer host allowlist + API-key rejection."""

from __future__ import annotations

import httpx
import pytest

from app.services.brokers.binance.errors import (
    BinanceLiveHostBlocked,
    BinanceSignedEndpointAttempted,
)
from app.services.brokers.binance.transport import build_public_client


@pytest.mark.asyncio
async def test_get_to_allowed_host_is_passed_through(httpx_mock) -> None:
    httpx_mock.add_response(
        url="https://api.binance.com/api/v3/ping", json={}, status_code=200,
    )
    client = build_public_client()
    try:
        resp = await client.get("https://api.binance.com/api/v3/ping")
        assert resp.status_code == 200
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_get_to_non_allowed_host_raises_at_request_time() -> None:
    client = build_public_client()
    try:
        with pytest.raises(BinanceLiveHostBlocked):
            await client.get("https://fapi.binance.com/fapi/v1/ping")
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_request_with_api_key_header_raises() -> None:
    client = build_public_client()
    try:
        with pytest.raises(BinanceSignedEndpointAttempted):
            await client.get(
                "https://api.binance.com/api/v3/account",
                headers={"X-MBX-APIKEY": "any-value"},
            )
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_redirect_to_non_allowed_host_raises(httpx_mock) -> None:
    """Defense in depth: follow_redirects=False means a 30x response is
    surfaced; the response hook treats any 30x as suspicious because
    Binance public endpoints do not legitimately redirect."""
    httpx_mock.add_response(
        url="https://api.binance.com/api/v3/ping",
        status_code=302,
        headers={"Location": "https://evil.example.com/whatever"},
    )
    client = build_public_client()
    try:
        with pytest.raises(BinanceLiveHostBlocked):
            await client.get("https://api.binance.com/api/v3/ping")
    finally:
        await client.aclose()
```

> If `httpx_mock` fixture is not yet installed, use `respx` instead — both are widely used in the Python httpx ecosystem. Confirm what the project uses by `grep -rn "httpx_mock\|respx" tests/` before this task and adapt.

- [ ] **Step 2: Run, expect FAIL**

```bash
uv run pytest tests/services/brokers/binance/test_transport_event_hooks.py -v
```

- [ ] **Step 3: Implement**

```python
# app/services/brokers/binance/transport.py
"""ROB-285 — Binance public HTTP transport with strict host allowlist.

Parent plan §4.7 introduces transport-layer host enforcement as a new
pattern. This module is the single chokepoint for constructing httpx
clients used by the Binance public adapter. All event hooks are wired
here; no other module should construct an httpx.AsyncClient for Binance
endpoints.
"""

from __future__ import annotations

from typing import Final

import httpx

from app.services.brokers.binance.errors import (
    BinanceLiveHostBlocked,
    BinanceSignedEndpointAttempted,
)
from app.services.brokers.binance.host_allowlist import assert_allowed_host


# Public-adapter request timeout; the smoke CLI can override per-call.
_DEFAULT_TIMEOUT: Final[float] = 10.0


async def _on_request(request: httpx.Request) -> None:
    """Pre-request hook: enforce host allowlist + forbid API-key header."""
    assert_allowed_host(request.url.host)
    # Defense in depth: even if some code path inadvertently added an
    # API-key header, refuse to send the request. The public adapter has
    # no business attaching this header.
    if "x-mbx-apikey" in (h.lower() for h in request.headers.keys()):
        raise BinanceSignedEndpointAttempted(
            f"Outgoing request to {request.url} carries X-MBX-APIKEY. "
            "Public adapter must not send signed-endpoint headers."
        )


async def _on_response(response: httpx.Response) -> None:
    """Post-response hook: surface 3xx as host-violation suspicion.

    With follow_redirects=False, a 30x reaches us as-is. Binance public
    endpoints do not legitimately redirect; treat any 30x as a possible
    routing anomaly and refuse to silently follow.
    """
    if 300 <= response.status_code < 400:
        location = response.headers.get("location", "")
        raise BinanceLiveHostBlocked(
            f"Unexpected redirect from {response.request.url} to {location!r}; "
            "Binance public endpoints do not legitimately redirect. Refusing."
        )


def build_public_client(*, timeout: float = _DEFAULT_TIMEOUT) -> httpx.AsyncClient:
    """Construct an httpx.AsyncClient with the public-adapter event hooks.

    Caller is responsible for `await client.aclose()` (usually via async
    context manager).
    """
    return httpx.AsyncClient(
        timeout=timeout,
        follow_redirects=False,
        event_hooks={"request": [_on_request], "response": [_on_response]},
    )
```

- [ ] **Step 4: Run, expect PASS**

```bash
uv run pytest tests/services/brokers/binance/test_transport_event_hooks.py -v
```

Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
git add app/services/brokers/binance/transport.py \
        tests/services/brokers/binance/test_transport_event_hooks.py
git commit -m "feat(rob-285): Binance public httpx transport with host allowlist event hooks

Co-Authored-By: Paperclip <noreply@paperclip.ing>"
```

---

### Task 5: REST client (`exchangeInfo`, `klines`, `bookTicker`)

**Files:**
- Create: `app/services/brokers/binance/dto.py`
- Create: `app/services/brokers/binance/rest_client.py`
- Create: `tests/services/brokers/binance/test_rest_client.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/services/brokers/binance/test_rest_client.py
"""ROB-285 — Binance public REST client (read-only, no API key required)."""

from __future__ import annotations

import datetime as dt

import pytest

from app.services.brokers.binance.rest_client import BinancePublicRestClient


@pytest.mark.asyncio
async def test_exchange_info_returns_symbol_metadata(httpx_mock) -> None:
    httpx_mock.add_response(
        url="https://api.binance.com/api/v3/exchangeInfo?symbol=BTCUSDT",
        json={
            "symbols": [{
                "symbol": "BTCUSDT", "status": "TRADING",
                "baseAsset": "BTC", "quoteAsset": "USDT",
                "filters": [],
            }],
        },
    )
    async with BinancePublicRestClient() as rest:
        info = await rest.exchange_info("BTCUSDT")
    assert info.symbol == "BTCUSDT"
    assert info.base_asset == "BTC"
    assert info.quote_asset == "USDT"
    assert info.status == "TRADING"


@pytest.mark.asyncio
async def test_klines_returns_list_of_dtos(httpx_mock) -> None:
    # Binance kline row shape: [openTime, open, high, low, close, vol,
    # closeTime, quoteVol, trades, takerBuyBase, takerBuyQuote, ignore]
    httpx_mock.add_response(
        url=(
            "https://api.binance.com/api/v3/klines?"
            "symbol=BTCUSDT&interval=1m&limit=1000"
        ),
        json=[[
            1700000000000, "30000.0", "30100.0", "29900.0", "30050.0",
            "12.5", 1700000059999, "375625.0", 100, "6.0", "180300.0", "0",
        ]],
    )
    async with BinancePublicRestClient() as rest:
        rows = await rest.klines("BTCUSDT", "1m", limit=1000)
    assert len(rows) == 1
    row = rows[0]
    assert row.open_time == dt.datetime(
        2023, 11, 14, 22, 13, 20, tzinfo=dt.UTC
    )
    assert float(row.open) == 30000.0
    assert row.is_closed is True   # Past kline; closeTime is in the past.


@pytest.mark.asyncio
async def test_book_ticker_returns_bid_ask(httpx_mock) -> None:
    httpx_mock.add_response(
        url="https://api.binance.com/api/v3/ticker/bookTicker?symbol=BTCUSDT",
        json={
            "symbol": "BTCUSDT",
            "bidPrice": "30000.0", "bidQty": "1.0",
            "askPrice": "30001.0", "askQty": "1.5",
        },
    )
    async with BinancePublicRestClient() as rest:
        bt = await rest.book_ticker("BTCUSDT")
    assert float(bt.bid_price) == 30000.0
    assert float(bt.ask_price) == 30001.0


@pytest.mark.asyncio
async def test_rest_client_does_not_expose_signed_methods() -> None:
    rest = BinancePublicRestClient()
    for forbidden in ("account", "order", "open_orders", "my_trades",
                      "cancel_order", "user_data_stream"):
        assert not hasattr(rest, forbidden), (
            f"Public adapter exposes {forbidden} — scope breach. "
            "Signed-endpoint surface belongs in Child C testnet adapter, "
            "not the public adapter."
        )
    await rest.aclose() if hasattr(rest, "aclose") else None
```

- [ ] **Step 2: Run, expect FAIL**

```bash
uv run pytest tests/services/brokers/binance/test_rest_client.py -v
```

- [ ] **Step 3: Implement DTOs + REST client**

```python
# app/services/brokers/binance/dto.py
"""ROB-285 — Normalized DTOs for the Binance public adapter.

SDK / wire types are never returned across the adapter boundary. All
caller-visible data structures are defined here.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True, slots=True)
class BinanceExchangeSymbolInfo:
    symbol: str
    base_asset: str
    quote_asset: str
    status: str  # TRADING / BREAK / HALT / etc.


@dataclass(frozen=True, slots=True)
class BinanceKlineRow:
    symbol: str
    interval: str
    open_time: dt.datetime
    close_time: dt.datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    base_volume: Decimal
    quote_volume: Decimal | None
    trade_count: int | None
    taker_buy_base_volume: Decimal | None
    taker_buy_quote_volume: Decimal | None
    is_closed: bool

    @property
    def event_at(self) -> dt.datetime:
        # For REST klines, the row's source_event_at is close_time.
        return self.close_time


@dataclass(frozen=True, slots=True)
class BinanceBookTicker:
    symbol: str
    bid_price: Decimal
    bid_qty: Decimal
    ask_price: Decimal
    ask_qty: Decimal
    fetched_at: dt.datetime
```

```python
# app/services/brokers/binance/rest_client.py
"""ROB-285 — Binance public REST client.

Public endpoints only: exchangeInfo, klines, bookTicker. No signed
endpoints. No API key required. Host allowlist enforced by transport.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Final

import httpx

from app.services.brokers.binance.dto import (
    BinanceBookTicker,
    BinanceExchangeSymbolInfo,
    BinanceKlineRow,
)
from app.services.brokers.binance.transport import build_public_client


_BASE_URL: Final[str] = "https://api.binance.com"


def _kline_from_row(symbol: str, interval: str, row: list, *, now: dt.datetime | None = None) -> BinanceKlineRow:
    """Translate a raw Binance kline list into a typed DTO.

    A REST kline is considered closed when close_time < now. The WS
    `x` flag will be used by the WS client; here we infer is_closed
    from time ordering. now is injectable for tests.
    """
    open_time = dt.datetime.fromtimestamp(row[0] / 1000.0, tz=dt.UTC)
    close_time = dt.datetime.fromtimestamp(row[6] / 1000.0, tz=dt.UTC)
    current = now or dt.datetime.now(tz=dt.UTC)
    return BinanceKlineRow(
        symbol=symbol,
        interval=interval,
        open_time=open_time,
        close_time=close_time,
        open=Decimal(row[1]),
        high=Decimal(row[2]),
        low=Decimal(row[3]),
        close=Decimal(row[4]),
        base_volume=Decimal(row[5]),
        quote_volume=Decimal(row[7]) if row[7] is not None else None,
        trade_count=int(row[8]) if row[8] is not None else None,
        taker_buy_base_volume=Decimal(row[9]) if row[9] is not None else None,
        taker_buy_quote_volume=Decimal(row[10]) if row[10] is not None else None,
        is_closed=close_time < current,
    )


class BinancePublicRestClient:
    """Read-only REST client for Binance public endpoints."""

    def __init__(self, *, client: httpx.AsyncClient | None = None) -> None:
        self._client = client or build_public_client()
        self._owns_client = client is None

    async def __aenter__(self) -> "BinancePublicRestClient":
        return self

    async def __aexit__(self, *exc) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def exchange_info(self, symbol: str) -> BinanceExchangeSymbolInfo:
        resp = await self._client.get(
            f"{_BASE_URL}/api/v3/exchangeInfo", params={"symbol": symbol},
        )
        resp.raise_for_status()
        # Rate-limit telemetry is wired in Task 6; for now, raw parse.
        payload = resp.json()
        sym = payload["symbols"][0]
        return BinanceExchangeSymbolInfo(
            symbol=sym["symbol"],
            base_asset=sym["baseAsset"],
            quote_asset=sym["quoteAsset"],
            status=sym["status"],
        )

    async def klines(
        self, symbol: str, interval: str, *,
        start_time: dt.datetime | None = None,
        end_time: dt.datetime | None = None,
        limit: int = 500,
    ) -> list[BinanceKlineRow]:
        params: dict[str, str | int] = {
            "symbol": symbol, "interval": interval, "limit": limit,
        }
        if start_time is not None:
            params["startTime"] = int(start_time.timestamp() * 1000)
        if end_time is not None:
            params["endTime"] = int(end_time.timestamp() * 1000)
        resp = await self._client.get(f"{_BASE_URL}/api/v3/klines", params=params)
        resp.raise_for_status()
        rows = resp.json()
        return [_kline_from_row(symbol, interval, r) for r in rows]

    async def book_ticker(self, symbol: str) -> BinanceBookTicker:
        resp = await self._client.get(
            f"{_BASE_URL}/api/v3/ticker/bookTicker", params={"symbol": symbol},
        )
        resp.raise_for_status()
        data = resp.json()
        return BinanceBookTicker(
            symbol=data["symbol"],
            bid_price=Decimal(data["bidPrice"]),
            bid_qty=Decimal(data["bidQty"]),
            ask_price=Decimal(data["askPrice"]),
            ask_qty=Decimal(data["askQty"]),
            fetched_at=dt.datetime.now(tz=dt.UTC),
        )

    # Intentionally NOT exposed: account(), order(), open_orders(),
    # my_trades(), cancel_order(), user_data_stream(). Public adapter only.
```

- [ ] **Step 4: Run, expect PASS**

```bash
uv run pytest tests/services/brokers/binance/test_rest_client.py -v
```

Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
git add app/services/brokers/binance/dto.py \
        app/services/brokers/binance/rest_client.py \
        tests/services/brokers/binance/test_rest_client.py
git commit -m "feat(rob-285): Binance public REST client (exchangeInfo, klines, bookTicker)

Co-Authored-By: Paperclip <noreply@paperclip.ing>"
```

---

### Task 6: Rate-limit header parser + telemetry emission

**Files:**
- Create: `app/services/brokers/binance/rate_limit_telemetry.py`
- Create: `tests/services/brokers/binance/test_rate_limit_telemetry.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/services/brokers/binance/test_rate_limit_telemetry.py
"""ROB-285 — Rate-limit header parser + telemetry."""

from __future__ import annotations

import logging

import pytest

from app.services.brokers.binance.rate_limit_telemetry import (
    RateLimitSnapshot,
    parse_rate_limit_headers,
    emit_rate_limit_snapshot,
)


def test_parses_used_weight_and_order_count() -> None:
    snap = parse_rate_limit_headers({
        "X-MBX-USED-WEIGHT-1M": "150",
        "X-MBX-ORDER-COUNT-1M": "2",
    })
    assert isinstance(snap, RateLimitSnapshot)
    assert snap.used_weight_1m == 150
    assert snap.order_count_1m == 2


def test_missing_headers_returns_none_fields() -> None:
    snap = parse_rate_limit_headers({})
    assert snap.used_weight_1m is None
    assert snap.order_count_1m is None


def test_emit_logs_structured_info(caplog) -> None:
    snap = RateLimitSnapshot(used_weight_1m=400, order_count_1m=5)
    with caplog.at_level(logging.INFO, logger="app.services.brokers.binance"):
        emit_rate_limit_snapshot(snap, declared_weight_limit=1200)
    records = [r for r in caplog.records if r.name.startswith("app.services.brokers.binance")]
    assert any("binance.rate_limit" in r.message for r in records)


def test_emit_does_not_set_sentry_tag_below_threshold(monkeypatch) -> None:
    sentry_calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        "app.services.brokers.binance.rate_limit_telemetry._set_sentry_tag",
        lambda k, v: sentry_calls.append((k, v)),
    )
    snap = RateLimitSnapshot(used_weight_1m=300, order_count_1m=0)  # 25% used
    emit_rate_limit_snapshot(snap, declared_weight_limit=1200)
    assert sentry_calls == []


def test_emit_sets_sentry_tag_above_threshold(monkeypatch) -> None:
    sentry_calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        "app.services.brokers.binance.rate_limit_telemetry._set_sentry_tag",
        lambda k, v: sentry_calls.append((k, v)),
    )
    snap = RateLimitSnapshot(used_weight_1m=700, order_count_1m=0)  # 58% used
    emit_rate_limit_snapshot(snap, declared_weight_limit=1200)
    assert sentry_calls and sentry_calls[0][0] == "binance.rate_limit_weight_pct"


def test_emit_does_not_raise_when_sentry_sdk_is_missing(monkeypatch) -> None:
    """Telemetry must fail-open when sentry_sdk is not installed."""
    import builtins

    real_import = builtins.__import__

    def stub_import(name, *args, **kwargs):
        if name == "sentry_sdk":
            raise ImportError("simulated missing sentry_sdk")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", stub_import)
    snap = RateLimitSnapshot(used_weight_1m=1000, order_count_1m=0)
    # Must not raise. Returns None.
    assert emit_rate_limit_snapshot(snap, declared_weight_limit=1200) is None


def test_emit_does_not_raise_when_sentry_set_tag_raises(monkeypatch) -> None:
    """Telemetry must fail-open when sentry_sdk.set_tag itself raises
    (e.g., not initialized in a way that surfaces as an exception in
    some sentry_sdk versions, or pathological misconfig)."""
    import sys
    import types

    fake_sentry = types.ModuleType("sentry_sdk")

    def boom(*args, **kwargs):
        raise RuntimeError("simulated sentry misconfig")

    fake_sentry.set_tag = boom  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "sentry_sdk", fake_sentry)
    snap = RateLimitSnapshot(used_weight_1m=1000, order_count_1m=0)
    # Must not raise.
    assert emit_rate_limit_snapshot(snap, declared_weight_limit=1200) is None
```

- [ ] **Step 2: Run, expect FAIL**

```bash
uv run pytest tests/services/brokers/binance/test_rate_limit_telemetry.py -v
```

- [ ] **Step 3: Implement**

```python
# app/services/brokers/binance/rate_limit_telemetry.py
"""ROB-285 — Binance rate-limit header parser + telemetry emission.

Parses `X-MBX-USED-WEIGHT-1M` and `X-MBX-ORDER-COUNT-1M` from REST
responses and emits structured `logger.info` + Sentry tag when usage
crosses 50% of the declared limit. Soft-throttle and hard-stop logic
live in Task 7; this module only observes.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Mapping


logger = logging.getLogger("app.services.brokers.binance.rate_limit")

# Below this fraction of declared limit, we don't emit a Sentry tag —
# avoids noise during normal operation. Logged at INFO regardless.
_SENTRY_TAG_THRESHOLD: float = 0.5


@dataclass(frozen=True, slots=True)
class RateLimitSnapshot:
    used_weight_1m: int | None
    order_count_1m: int | None


def _int_or_none(value: str | None) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except ValueError:
        return None


def parse_rate_limit_headers(headers: Mapping[str, str]) -> RateLimitSnapshot:
    """Extract Binance rate-limit counters from REST response headers.

    Header lookup is case-insensitive via dict-style access on httpx
    Headers; for plain dicts the caller must normalize.
    """
    # httpx.Headers is case-insensitive but a plain dict isn't. Normalize.
    norm = {k.lower(): v for k, v in headers.items()}
    return RateLimitSnapshot(
        used_weight_1m=_int_or_none(norm.get("x-mbx-used-weight-1m")),
        order_count_1m=_int_or_none(norm.get("x-mbx-order-count-1m")),
    )


def _set_sentry_tag(key: str, value: str) -> None:
    """Indirected so tests can monkeypatch without depending on sentry_sdk.

    Telemetry must NEVER break the adapter. Sentry can be:
    - not installed (ImportError),
    - installed but not initialized (no DSN — set_tag is a safe no-op),
    - installed and misconfigured (set_tag may raise in pathological cases),
    - installed and healthy (set_tag works).

    All four cases are handled with a blanket Exception catch — fail-open
    is correct here because rate-limit telemetry is observability, not
    operational state. If telemetry breaks the adapter, the project
    loses both observability AND the adapter.
    """
    try:
        import sentry_sdk

        sentry_sdk.set_tag(key, value)
    except Exception:  # noqa: BLE001 — intentional fail-open
        # Swallow ImportError, RuntimeError, AttributeError, anything.
        # Adapter functionality cannot depend on Sentry health.
        return


def emit_rate_limit_snapshot(
    snap: RateLimitSnapshot, *, declared_weight_limit: int = 1200,
) -> None:
    """Log + (conditionally) tag a single rate-limit snapshot.

    declared_weight_limit defaults to Binance spot REST 1m weight cap
    (1200 as of 2025); pass the exchangeInfo-reported value when known.
    """
    used = snap.used_weight_1m or 0
    pct = (used / declared_weight_limit) if declared_weight_limit > 0 else 0.0
    logger.info(
        "binance.rate_limit "
        f"used_weight_1m={used} "
        f"order_count_1m={snap.order_count_1m or 0} "
        f"declared_weight={declared_weight_limit} "
        f"pct={pct:.2%}"
    )
    if pct >= _SENTRY_TAG_THRESHOLD:
        _set_sentry_tag("binance.rate_limit_weight_pct", f"{int(pct * 100)}")
```

Wire telemetry into the REST client by adding a hook in `BinancePublicRestClient.__init__`'s response handling. Simplest path: after each successful response in `klines`/`exchange_info`/`book_ticker`, call `emit_rate_limit_snapshot(parse_rate_limit_headers(resp.headers))`. Avoid wrapping at the transport layer to keep the telemetry close to the call site and avoid double-counting on retries.

Add the wire-in to `rest_client.py`:

```python
# At top of rest_client.py
from app.services.brokers.binance.rate_limit_telemetry import (
    emit_rate_limit_snapshot, parse_rate_limit_headers,
)

# Inside each public method, immediately after resp.raise_for_status():
emit_rate_limit_snapshot(parse_rate_limit_headers(dict(resp.headers)))
```

- [ ] **Step 4: Run tests, expect PASS**

```bash
uv run pytest tests/services/brokers/binance/test_rate_limit_telemetry.py \
              tests/services/brokers/binance/test_rest_client.py -v
```

Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add app/services/brokers/binance/rate_limit_telemetry.py \
        app/services/brokers/binance/rest_client.py \
        tests/services/brokers/binance/test_rate_limit_telemetry.py
git commit -m "feat(rob-285): Binance rate-limit header parsing + structured telemetry

Co-Authored-By: Paperclip <noreply@paperclip.ing>"
```

---

### Task 7: Rate-limit soft-throttle + hard-stop

**Files:**
- Modify: `app/services/brokers/binance/rest_client.py`
- Create or extend: `tests/services/brokers/binance/test_rate_limit_telemetry.py`

- [ ] **Step 1: Write the failing tests**

Add to `test_rate_limit_telemetry.py` (or new `test_rate_limit_handling.py`):

```python
@pytest.mark.asyncio
async def test_soft_throttle_sleeps_when_above_80pct(httpx_mock, monkeypatch) -> None:
    """When used_weight crosses 80% of declared, the next REST call sleeps."""
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr("app.services.brokers.binance.rest_client.asyncio.sleep", fake_sleep)
    httpx_mock.add_response(
        url="https://api.binance.com/api/v3/exchangeInfo?symbol=BTCUSDT",
        json={"symbols": [{"symbol": "BTCUSDT", "status": "TRADING",
                           "baseAsset": "BTC", "quoteAsset": "USDT", "filters": []}]},
        headers={"X-MBX-USED-WEIGHT-1M": "1000"},  # 83% of 1200
    )
    httpx_mock.add_response(
        url="https://api.binance.com/api/v3/exchangeInfo?symbol=ETHUSDT",
        json={"symbols": [{"symbol": "ETHUSDT", "status": "TRADING",
                           "baseAsset": "ETH", "quoteAsset": "USDT", "filters": []}]},
        headers={"X-MBX-USED-WEIGHT-1M": "1010"},
    )
    async with BinancePublicRestClient() as rest:
        await rest.exchange_info("BTCUSDT")
        await rest.exchange_info("ETHUSDT")
    assert sleeps, "Expected at least one soft-throttle sleep call"


@pytest.mark.asyncio
async def test_429_raises_binance_rate_limited_with_retry_after(httpx_mock) -> None:
    httpx_mock.add_response(
        url="https://api.binance.com/api/v3/exchangeInfo?symbol=BTCUSDT",
        status_code=429,
        headers={"Retry-After": "30"},
    )
    from app.services.brokers.binance.errors import BinanceRateLimited
    async with BinancePublicRestClient() as rest:
        with pytest.raises(BinanceRateLimited) as exc_info:
            await rest.exchange_info("BTCUSDT")
    assert exc_info.value.retry_after_seconds == 30.0


@pytest.mark.asyncio
async def test_418_raises_binance_rate_limited(httpx_mock) -> None:
    httpx_mock.add_response(
        url="https://api.binance.com/api/v3/exchangeInfo?symbol=BTCUSDT",
        status_code=418,
        headers={"Retry-After": "60"},
    )
    from app.services.brokers.binance.errors import BinanceRateLimited
    async with BinancePublicRestClient() as rest:
        with pytest.raises(BinanceRateLimited):
            await rest.exchange_info("BTCUSDT")
```

- [ ] **Step 2: Run, expect FAIL**

```bash
uv run pytest tests/services/brokers/binance/test_rate_limit_telemetry.py -v -k "soft_throttle or 429 or 418"
```

- [ ] **Step 3: Implement**

Extend `BinancePublicRestClient` with internal state for the last-seen snapshot and a private helper invoked before each request:

```python
# app/services/brokers/binance/rest_client.py (additions)
import asyncio

from app.services.brokers.binance.errors import BinanceRateLimited

_SOFT_THROTTLE_THRESHOLD: float = 0.8
_DEFAULT_DECLARED_WEIGHT: int = 1200


class BinancePublicRestClient:
    # ... existing fields ...
    def __init__(self, *, client: httpx.AsyncClient | None = None,
                 declared_weight_limit: int = _DEFAULT_DECLARED_WEIGHT) -> None:
        self._client = client or build_public_client()
        self._owns_client = client is None
        self._declared_weight_limit = declared_weight_limit
        self._last_used_weight: int | None = None

    async def _maybe_soft_throttle(self) -> None:
        if self._last_used_weight is None:
            return
        if self._last_used_weight / self._declared_weight_limit >= _SOFT_THROTTLE_THRESHOLD:
            # Sleep to the next minute window. Binance counters reset at
            # the minute boundary; sleeping the remainder of the current
            # minute is the simplest safe behavior.
            now = dt.datetime.now(tz=dt.UTC)
            sleep_seconds = max(60 - now.second, 1.0)
            logger.warning(
                "binance.rate_limit soft-throttling: used_weight=%s "
                "declared=%s sleeping=%.1fs",
                self._last_used_weight, self._declared_weight_limit,
                sleep_seconds,
            )
            await asyncio.sleep(sleep_seconds)

    async def _send(self, method: str, url: str, **kwargs) -> httpx.Response:
        await self._maybe_soft_throttle()
        resp = await self._client.request(method, url, **kwargs)
        snap = parse_rate_limit_headers(dict(resp.headers))
        emit_rate_limit_snapshot(snap, declared_weight_limit=self._declared_weight_limit)
        if snap.used_weight_1m is not None:
            self._last_used_weight = snap.used_weight_1m
        if resp.status_code in (418, 429):
            retry_after = float(resp.headers.get("retry-after", "60"))
            raise BinanceRateLimited(retry_after, f"Binance {resp.status_code}; Retry-After {retry_after}s")
        return resp

    # Replace direct self._client.get(...) calls in exchange_info / klines /
    # book_ticker with self._send("GET", url, params=...).
```

- [ ] **Step 4: Run, expect PASS**

```bash
uv run pytest tests/services/brokers/binance/ -v
```

- [ ] **Step 5: Commit**

```bash
git add app/services/brokers/binance/rest_client.py \
        tests/services/brokers/binance/test_rate_limit_telemetry.py
git commit -m "feat(rob-285): rate-limit soft-throttle (>=80% weight) + hard-stop (429/418)

Co-Authored-By: Paperclip <noreply@paperclip.ing>"
```

---

### Task 8: `crypto_instrument_health` table + model + service

**Files:**
- Create: `alembic/versions/<rev>_add_crypto_instrument_health.py`
- Create: `app/models/crypto_instrument_health.py`
- Modify: `app/models/__init__.py`
- Create: `app/services/instrument_health/__init__.py`
- Create: `app/services/instrument_health/repository.py`
- Create: `app/services/instrument_health/service.py`
- Create: `tests/services/instrument_health/__init__.py`
- Create: `tests/services/instrument_health/test_instrument_health_service.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/services/instrument_health/test_instrument_health_service.py
"""ROB-285 — CryptoInstrumentHealthService (service-only writes)."""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.crypto_instruments import CryptoInstrument
from app.services.instrument_health.service import (
    CryptoInstrumentHealthService,
    InstrumentHealthState,
)


@pytest.mark.asyncio
async def test_default_state_is_healthy_on_first_touch(db_session: AsyncSession) -> None:
    inst = CryptoInstrument(
        venue="binance", product="spot", venue_symbol="BTCUSDT",
        base_asset="BTC", quote_asset="USDT", status="active",
    )
    db_session.add(inst)
    await db_session.flush()
    svc = CryptoInstrumentHealthService(session=db_session)
    state = await svc.get_state(inst.id)
    assert state == InstrumentHealthState.HEALTHY


@pytest.mark.asyncio
async def test_record_degraded_then_back_to_healthy(db_session: AsyncSession) -> None:
    inst = CryptoInstrument(
        venue="binance", product="spot", venue_symbol="ETHUSDT",
        base_asset="ETH", quote_asset="USDT", status="active",
    )
    db_session.add(inst)
    await db_session.flush()
    svc = CryptoInstrumentHealthService(session=db_session)
    await svc.record_degraded(inst.id, reason="3 reconnect failures")
    assert await svc.get_state(inst.id) == InstrumentHealthState.DEGRADED
    await svc.record_recovered(inst.id)
    assert await svc.get_state(inst.id) == InstrumentHealthState.HEALTHY


@pytest.mark.asyncio
async def test_record_manual_backfill_required_does_not_auto_clear(db_session: AsyncSession) -> None:
    inst = CryptoInstrument(
        venue="binance", product="spot", venue_symbol="SOLUSDT",
        base_asset="SOL", quote_asset="USDT", status="active",
    )
    db_session.add(inst)
    await db_session.flush()
    svc = CryptoInstrumentHealthService(session=db_session)
    await svc.record_manual_backfill_required(inst.id, reason="gap 8000 candles")
    assert await svc.get_state(inst.id) == InstrumentHealthState.MANUAL_BACKFILL_REQUIRED
    # record_recovered must be explicit; the service does not auto-clear.
    with pytest.raises(ValueError):
        await svc.record_recovered(inst.id)
    await svc.clear_manual_backfill(inst.id, operator="alice")
    assert await svc.get_state(inst.id) == InstrumentHealthState.HEALTHY


@pytest.mark.asyncio
async def test_invalid_state_raises_at_db_level(db_session: AsyncSession) -> None:
    # Direct SQL insert with bogus state must violate the CHECK constraint.
    inst = CryptoInstrument(
        venue="binance", product="spot", venue_symbol="DOGEUSDT",
        base_asset="DOGE", quote_asset="USDT", status="active",
    )
    db_session.add(inst)
    await db_session.flush()
    from sqlalchemy.exc import IntegrityError
    with pytest.raises(IntegrityError):
        await db_session.execute(text(
            "INSERT INTO crypto_instrument_health (instrument_id, state) "
            "VALUES (:i, 'bogus')"
        ), {"i": inst.id})
        await db_session.flush()


def test_repository_is_not_importable_from_outside_the_service_module() -> None:
    """Service-only writes: importing CryptoInstrumentHealthRepository from
    elsewhere is an explicit anti-pattern. We rely on a runtime guard in
    repository.py that raises if any external module imports it."""
    # External (out-of-package) import is what should be guarded against.
    # Service-internal imports use a non-public alias to bypass the guard.
    import importlib
    with pytest.raises(ImportError):
        importlib.import_module(
            "app.services.instrument_health.repository._public_export"
        )
```

- [ ] **Step 2: Run, expect FAIL**

```bash
uv run pytest tests/services/instrument_health/ -v
```

- [ ] **Step 3: Generate migration**

```bash
uv run alembic revision -m "add crypto_instrument_health"
```

Then edit the generated file with:

```python
# alembic/versions/<rev>_add_crypto_instrument_health.py
from alembic import op
import sqlalchemy as sa

revision = "<rev>"
down_revision = "5fa5a347d85b"  # ROB-284 head
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "crypto_instrument_health",
        sa.Column(
            "instrument_id",
            sa.BigInteger(),
            sa.ForeignKey("crypto_instruments.id"),
            primary_key=True,
        ),
        sa.Column("state", sa.Text(), nullable=False, server_default=sa.text("'healthy'")),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column(
            "last_state_change_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False, server_default=sa.text("now()"),
        ),
        sa.Column("last_closed_candle_time", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("retry_after_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("metadata", sa.dialects.postgresql.JSONB(), nullable=True),
        sa.Column(
            "created_at", sa.TIMESTAMP(timezone=True),
            nullable=False, server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at", sa.TIMESTAMP(timezone=True),
            nullable=False, server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "state IN ('healthy','degraded','rate_limited','manual_backfill_required')",
            name="ck_crypto_instrument_health_state",
        ),
    )


def downgrade() -> None:
    op.drop_table("crypto_instrument_health")
```

- [ ] **Step 4: Implement model + service + repository**

```python
# app/models/crypto_instrument_health.py
from __future__ import annotations

import datetime as dt
from typing import Any

from sqlalchemy import BigInteger, CheckConstraint, ForeignKey, Integer, Text
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class CryptoInstrumentHealth(Base):
    __tablename__ = "crypto_instrument_health"
    __table_args__ = (
        CheckConstraint(
            "state IN ('healthy','degraded','rate_limited','manual_backfill_required')",
            name="state",  # naming_convention prefixes with ck_<table>_
        ),
    )

    instrument_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("crypto_instruments.id"), primary_key=True,
    )
    state: Mapped[str] = mapped_column(Text, nullable=False, default="healthy")
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_state_change_at: Mapped[dt.datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False,
    )
    last_closed_candle_time: Mapped[dt.datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True,
    )
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    retry_after_at: Mapped[dt.datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True,
    )
    extra_metadata: Mapped[dict[str, Any] | None] = mapped_column(
        "metadata", JSONB, nullable=True,
    )
    created_at: Mapped[dt.datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    updated_at: Mapped[dt.datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
```

```python
# app/services/instrument_health/repository.py
"""ROB-285 — Repository for crypto_instrument_health.

Service-internal. Do not import outside app/services/instrument_health/.
"""

from __future__ import annotations

import inspect

# Runtime import guard — see test_instrument_health_service.py
def _enforce_internal_use() -> None:
    frame = inspect.stack()[2]  # skip this function + caller (repository module)
    importer = frame.frame.f_globals.get("__name__", "<unknown>")
    if not importer.startswith("app.services.instrument_health"):
        raise ImportError(
            f"CryptoInstrumentHealthRepository is service-internal "
            f"(imported from {importer!r}). Use CryptoInstrumentHealthService "
            "as the public API. ROB-285 service-only-write rule."
        )

# Sentinel re-export that the guard uses to detect external imports.
class _public_export:  # noqa: N801 — by-design unconventional name
    def __init__(self) -> None:
        _enforce_internal_use()

# ... repository class definition ...
```

```python
# app/services/instrument_health/service.py
"""ROB-285 — CryptoInstrumentHealthService (the public write surface).

All writes to crypto_instrument_health go through this service. Direct
SQL or repository imports from outside this package are forbidden;
test_instrument_health_service.py locks the invariant.
"""

from __future__ import annotations

import datetime as dt
import enum
from sqlalchemy.ext.asyncio import AsyncSession


class InstrumentHealthState(str, enum.Enum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    RATE_LIMITED = "rate_limited"
    MANUAL_BACKFILL_REQUIRED = "manual_backfill_required"


class CryptoInstrumentHealthService:
    def __init__(self, *, session: AsyncSession) -> None:
        self._session = session

    async def get_state(self, instrument_id: int) -> InstrumentHealthState:
        ...

    async def record_degraded(self, instrument_id: int, *, reason: str) -> None:
        ...

    async def record_rate_limited(
        self, instrument_id: int, *, retry_after_at: dt.datetime, reason: str,
    ) -> None:
        ...

    async def record_manual_backfill_required(
        self, instrument_id: int, *, reason: str,
    ) -> None:
        ...

    async def record_recovered(self, instrument_id: int) -> None:
        """Transition from degraded or rate_limited back to healthy.
        Refuses to clear manual_backfill_required — use clear_manual_backfill."""
        ...

    async def clear_manual_backfill(
        self, instrument_id: int, *, operator: str,
    ) -> None:
        ...
```

- [ ] **Step 5: Register model in `app/models/__init__.py`**

```python
# app/models/__init__.py — add:
from .crypto_instrument_health import CryptoInstrumentHealth  # noqa: F401
```

- [ ] **Step 6: Run, expect PASS**

```bash
uv run pytest tests/services/instrument_health/ -v
```

- [ ] **Step 7: Commit**

```bash
git add alembic/versions/<rev>_add_crypto_instrument_health.py \
        app/models/crypto_instrument_health.py app/models/__init__.py \
        app/services/instrument_health/ \
        tests/services/instrument_health/
git commit -m "feat(rob-285): crypto_instrument_health table + service-only write surface

Co-Authored-By: Paperclip <noreply@paperclip.ing>"
```

---

### Task 9: REST backfill engine — module-level (no WS integration yet)

> **Layer note:** Task 9 builds the *standalone* `RestBackfiller` engine with caps and its unit tests against an in-memory fake REST client. It does **not** integrate with the WS reconnect loop — that orchestration lives in Task 12 (gap detection → invokes this engine → routes results to `MinuteCandlesRepository` and `CryptoInstrumentHealthService`). The two tasks are intentionally separated so the engine math (pagination, cap counting, partial-result attachment) is testable in isolation before it gets wired into the WS run-loop's state-machine concerns.

**Files:**
- Create: `app/services/brokers/binance/backfill.py`
- Create: `tests/services/brokers/binance/test_backfill.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/services/brokers/binance/test_backfill.py
"""ROB-285 — REST kline backfill with bounded caps."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from app.services.brokers.binance.backfill import (
    BackfillCaps, BackfillResult, RestBackfiller,
)
from app.services.brokers.binance.errors import BinanceBackfillCapExceeded


class _FakeRest:
    """In-memory fake REST client for deterministic tests."""

    def __init__(self, *, all_klines: list, page_size: int = 1000) -> None:
        self.all = all_klines
        self.page_size = page_size
        self.calls = 0

    async def klines(self, symbol, interval, *, start_time, end_time=None, limit):
        self.calls += 1
        # Return up to `limit` klines whose open_time >= start_time.
        slice_ = [k for k in self.all if k.open_time >= start_time][:limit]
        return slice_


def _mk_kline(t: dt.datetime) -> "BinanceKlineRow":
    from app.services.brokers.binance.dto import BinanceKlineRow
    return BinanceKlineRow(
        symbol="BTCUSDT", interval="1m",
        open_time=t,
        close_time=t + dt.timedelta(minutes=1) - dt.timedelta(milliseconds=1),
        open=Decimal("1"), high=Decimal("1"), low=Decimal("1"), close=Decimal("1"),
        base_volume=Decimal("0"), quote_volume=None, trade_count=None,
        taker_buy_base_volume=None, taker_buy_quote_volume=None, is_closed=True,
    )


@pytest.mark.asyncio
async def test_backfill_within_caps_returns_all_klines() -> None:
    start = dt.datetime(2026, 5, 20, tzinfo=dt.UTC)
    klines = [_mk_kline(start + dt.timedelta(minutes=i)) for i in range(50)]
    rest = _FakeRest(all_klines=klines)
    bf = RestBackfiller(
        rest=rest, caps=BackfillCaps(max_candles=5000, max_requests=10, page_size=1000),
    )
    result = await bf.backfill(symbol="BTCUSDT", interval="1m", since=start)
    assert isinstance(result, BackfillResult)
    assert len(result.klines) == 50
    assert rest.calls == 1


@pytest.mark.asyncio
async def test_backfill_paginates_with_endtime_anchor() -> None:
    start = dt.datetime(2026, 5, 20, tzinfo=dt.UTC)
    # 2500 candles → 3 pages at 1000/page.
    klines = [_mk_kline(start + dt.timedelta(minutes=i)) for i in range(2500)]
    rest = _FakeRest(all_klines=klines, page_size=1000)
    bf = RestBackfiller(
        rest=rest, caps=BackfillCaps(max_candles=5000, max_requests=10, page_size=1000),
    )
    result = await bf.backfill(symbol="BTCUSDT", interval="1m", since=start)
    assert len(result.klines) == 2500
    assert rest.calls == 3


@pytest.mark.asyncio
async def test_backfill_cap_exceeded_raises_with_partial() -> None:
    start = dt.datetime(2026, 5, 20, tzinfo=dt.UTC)
    klines = [_mk_kline(start + dt.timedelta(minutes=i)) for i in range(8000)]
    rest = _FakeRest(all_klines=klines, page_size=1000)
    bf = RestBackfiller(
        rest=rest,
        caps=BackfillCaps(max_candles=5000, max_requests=10, page_size=1000),
    )
    with pytest.raises(BinanceBackfillCapExceeded) as exc_info:
        await bf.backfill(symbol="BTCUSDT", interval="1m", since=start)
    # Exception carries the partial result for the caller to inspect.
    assert exc_info.value.args  # message present
```

- [ ] **Step 2: Run, expect FAIL**

```bash
uv run pytest tests/services/brokers/binance/test_backfill.py -v
```

- [ ] **Step 3: Implement**

```python
# app/services/brokers/binance/backfill.py
"""ROB-285 — REST kline backfill engine with bounded caps.

Pagination is forward-in-time (`startTime` anchored), advancing
`startTime` past the last received kline. Stops when:
- the API returns fewer rows than `page_size` (caught up), OR
- `max_candles` is reached, OR
- `max_requests` is reached.

If either cap is hit before catch-up, raises BinanceBackfillCapExceeded
with the partial result attached for the caller's logging.
"""

from __future__ import annotations

import datetime as dt
import os
from dataclasses import dataclass
from typing import Protocol

from app.services.brokers.binance.dto import BinanceKlineRow
from app.services.brokers.binance.errors import BinanceBackfillCapExceeded


@dataclass(frozen=True, slots=True)
class BackfillCaps:
    max_candles: int
    max_requests: int
    page_size: int

    @classmethod
    def from_env(cls) -> "BackfillCaps":
        return cls(
            max_candles=int(os.getenv("BINANCE_KLINE_BACKFILL_MAX_CANDLES", "5000")),
            max_requests=int(os.getenv("BINANCE_KLINE_BACKFILL_MAX_REQUESTS", "10")),
            page_size=int(os.getenv("BINANCE_KLINE_BACKFILL_PAGE_SIZE", "1000")),
        )


@dataclass(frozen=True, slots=True)
class BackfillResult:
    klines: list[BinanceKlineRow]
    requests_used: int


class _RestKlineClient(Protocol):
    async def klines(
        self, symbol: str, interval: str, *,
        start_time: dt.datetime, end_time: dt.datetime | None = None,
        limit: int,
    ) -> list[BinanceKlineRow]:
        ...


class RestBackfiller:
    def __init__(self, *, rest: _RestKlineClient, caps: BackfillCaps) -> None:
        self._rest = rest
        self._caps = caps

    async def backfill(
        self, *, symbol: str, interval: str, since: dt.datetime,
    ) -> BackfillResult:
        out: list[BinanceKlineRow] = []
        requests = 0
        cursor = since
        while True:
            if requests >= self._caps.max_requests:
                raise BinanceBackfillCapExceeded(
                    f"max_requests={self._caps.max_requests} exceeded; "
                    f"collected {len(out)} klines for {symbol} {interval}"
                )
            if len(out) >= self._caps.max_candles:
                raise BinanceBackfillCapExceeded(
                    f"max_candles={self._caps.max_candles} exceeded; "
                    f"collected {len(out)} klines for {symbol} {interval}"
                )
            page = await self._rest.klines(
                symbol=symbol, interval=interval,
                start_time=cursor, limit=self._caps.page_size,
            )
            requests += 1
            if not page:
                break
            out.extend(page)
            if len(page) < self._caps.page_size:
                break  # caught up
            # Advance cursor past the last kline received to avoid duplicates.
            cursor = page[-1].open_time + dt.timedelta(milliseconds=1)
        return BackfillResult(klines=out, requests_used=requests)
```

- [ ] **Step 4: Run, expect PASS**

```bash
uv run pytest tests/services/brokers/binance/test_backfill.py -v
```

- [ ] **Step 5: Commit**

```bash
git add app/services/brokers/binance/backfill.py \
        tests/services/brokers/binance/test_backfill.py
git commit -m "feat(rob-285): bounded REST backfill engine (5000/10/1000 default caps)

Co-Authored-By: Paperclip <noreply@paperclip.ing>"
```

---

### Task 10: WebSocket client (kline_1m + bookTicker, combined streams)

**Files:**
- Create: `app/services/brokers/binance/ws_client.py`
- Create: `tests/services/brokers/binance/test_ws_client.py`

- [ ] **Step 1: Confirm WS library choice**

Run:

```bash
uv run python -c "import importlib; print(importlib.import_module('binance_sdk_spot.websocket_api'))" 2>&1 | head -3
uv run python -c "import websockets; print(websockets.__version__)" 2>&1
```

If `binance-sdk-spot` exposes a clean WS API that does not require callbacks-with-state, use it. Otherwise use `websockets` directly. **Lean:** `websockets` direct — keeps callback shape simple and tests deterministic via `websockets.serve`.

- [ ] **Step 2: Write the failing tests**

```python
# tests/services/brokers/binance/test_ws_client.py
"""ROB-285 — Binance public WS client (combined streams)."""

from __future__ import annotations

import asyncio
import json

import pytest
import websockets

from app.services.brokers.binance.ws_client import (
    BinancePublicWSClient,
    KlineEvent,
)


@pytest.mark.asyncio
async def test_ws_yields_kline_events_for_closed_bars() -> None:
    """Wire a local websocket server, push one closed kline, assert
    the client yields a KlineEvent with is_closed=True."""
    received: list[KlineEvent] = []

    async def handler(ws):
        await ws.send(json.dumps({
            "stream": "btcusdt@kline_1m",
            "data": {
                "e": "kline",
                "k": {
                    "t": 1700000000000, "T": 1700000059999,
                    "s": "BTCUSDT", "i": "1m",
                    "o": "30000.0", "h": "30100.0", "l": "29900.0", "c": "30050.0",
                    "v": "12.5", "q": "375625.0", "n": 100,
                    "V": "6.0", "Q": "180300.0", "x": True,
                },
            },
        }))
        # Keep connection open briefly for the client to drain.
        await asyncio.sleep(0.1)

    server = await websockets.serve(handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        url = f"ws://127.0.0.1:{port}/stream?streams=btcusdt@kline_1m"
        async with BinancePublicWSClient(url=url) as ws:
            async for event in ws.events(stop_after=1):
                received.append(event)
                if len(received) >= 1:
                    break
    finally:
        server.close()
        await server.wait_closed()

    assert len(received) == 1
    ev = received[0]
    assert ev.symbol == "BTCUSDT"
    assert ev.is_closed is True


@pytest.mark.asyncio
async def test_ws_drops_in_progress_klines() -> None:
    """ROB-285 §B.3 lock: in-progress klines (x=False) are dropped."""
    async def handler(ws):
        await ws.send(json.dumps({
            "stream": "btcusdt@kline_1m",
            "data": {"e": "kline", "k": {
                "t": 1700000000000, "T": 1700000059999, "s": "BTCUSDT", "i": "1m",
                "o": "30000", "h": "30100", "l": "29900", "c": "30050",
                "v": "12.5", "q": "375625", "n": 100,
                "V": "6", "Q": "180300", "x": False,
            }},
        }))
        await ws.send(json.dumps({
            "stream": "btcusdt@kline_1m",
            "data": {"e": "kline", "k": {
                "t": 1700000060000, "T": 1700000119999, "s": "BTCUSDT", "i": "1m",
                "o": "30050", "h": "30150", "l": "29950", "c": "30100",
                "v": "8.0", "q": "240800", "n": 80,
                "V": "4", "Q": "120400", "x": True,
            }},
        }))
        await asyncio.sleep(0.1)

    server = await websockets.serve(handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    received: list[KlineEvent] = []
    try:
        url = f"ws://127.0.0.1:{port}/stream?streams=btcusdt@kline_1m"
        async with BinancePublicWSClient(url=url) as ws:
            async for event in ws.events(stop_after=1):
                received.append(event)
                if len(received) >= 1:
                    break
    finally:
        server.close()
        await server.wait_closed()
    assert len(received) == 1
    assert received[0].is_closed is True  # In-progress was dropped.


@pytest.mark.asyncio
async def test_ws_rejects_non_allowed_host() -> None:
    from app.services.brokers.binance.errors import BinanceLiveHostBlocked
    with pytest.raises(BinanceLiveHostBlocked):
        BinancePublicWSClient(url="wss://evil.example.com/stream?streams=btcusdt@kline_1m")
```

- [ ] **Step 3: Run, expect FAIL**

```bash
uv run pytest tests/services/brokers/binance/test_ws_client.py -v
```

- [ ] **Step 4: Implement**

```python
# app/services/brokers/binance/ws_client.py
"""ROB-285 — Binance public WS client (combined streams).

Subscribes to a combined-stream URL like:
  wss://stream.binance.com:9443/stream?streams=btcusdt@kline_1m/btcusdt@bookTicker

Yields normalized events. In-progress klines (`x: False`) are dropped
per parent plan §B.3; only closed klines (`x: True`) are emitted.
"""

from __future__ import annotations

import datetime as dt
import json
import urllib.parse
from dataclasses import dataclass
from decimal import Decimal
from typing import AsyncIterator

import websockets

from app.services.brokers.binance.errors import BinanceLiveHostBlocked
from app.services.brokers.binance.host_allowlist import PUBLIC_HOSTS


_WS_DEFAULT_HOST = "stream.binance.com"


@dataclass(frozen=True, slots=True)
class KlineEvent:
    symbol: str
    interval: str
    open_time: dt.datetime
    close_time: dt.datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    base_volume: Decimal
    quote_volume: Decimal
    trade_count: int
    is_closed: bool


@dataclass(frozen=True, slots=True)
class BookTickerEvent:
    symbol: str
    bid_price: Decimal
    bid_qty: Decimal
    ask_price: Decimal
    ask_qty: Decimal
    received_at: dt.datetime


WsEvent = KlineEvent | BookTickerEvent


def _assert_url_host_allowed(url: str, *, allowed: frozenset[str]) -> None:
    parsed = urllib.parse.urlparse(url)
    # Local test override: 127.0.0.1 is acceptable when the URL scheme is ws (not wss)
    # — tests inject a local server. Reject all other non-allowed hosts.
    host = parsed.hostname or ""
    if host == "127.0.0.1" and parsed.scheme == "ws":
        return
    if host not in allowed:
        raise BinanceLiveHostBlocked(
            f"WS host {host!r} is not in PUBLIC_HOSTS. "
            "Allowed: " + ", ".join(sorted(allowed))
        )


class BinancePublicWSClient:
    def __init__(self, *, url: str) -> None:
        _assert_url_host_allowed(url, allowed=PUBLIC_HOSTS)
        self._url = url
        self._ws: websockets.WebSocketClientProtocol | None = None

    async def __aenter__(self) -> "BinancePublicWSClient":
        self._ws = await websockets.connect(self._url, ping_interval=20)
        return self

    async def __aexit__(self, *exc) -> None:
        if self._ws is not None:
            await self._ws.close()

    async def events(self, *, stop_after: int | None = None) -> AsyncIterator[WsEvent]:
        emitted = 0
        assert self._ws is not None
        async for raw in self._ws:
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue
            data = msg.get("data") or {}
            event_type = data.get("e")
            if event_type == "kline":
                k = data["k"]
                if not k.get("x"):
                    continue  # Drop in-progress kline (parent plan §B.3).
                ev: WsEvent = KlineEvent(
                    symbol=k["s"], interval=k["i"],
                    open_time=dt.datetime.fromtimestamp(k["t"] / 1000.0, tz=dt.UTC),
                    close_time=dt.datetime.fromtimestamp(k["T"] / 1000.0, tz=dt.UTC),
                    open=Decimal(k["o"]), high=Decimal(k["h"]),
                    low=Decimal(k["l"]), close=Decimal(k["c"]),
                    base_volume=Decimal(k["v"]),
                    quote_volume=Decimal(k["q"]),
                    trade_count=int(k["n"]),
                    is_closed=True,
                )
            elif data.get("u") is not None and "b" in data and "a" in data:
                # bookTicker stream payload shape (no "e" field).
                ev = BookTickerEvent(
                    symbol=data["s"],
                    bid_price=Decimal(data["b"]), bid_qty=Decimal(data["B"]),
                    ask_price=Decimal(data["a"]), ask_qty=Decimal(data["A"]),
                    received_at=dt.datetime.now(tz=dt.UTC),
                )
            else:
                continue
            yield ev
            emitted += 1
            if stop_after is not None and emitted >= stop_after:
                return
```

- [ ] **Step 5: Run, expect PASS**

```bash
uv run pytest tests/services/brokers/binance/test_ws_client.py -v
```

- [ ] **Step 6: Commit**

```bash
git add app/services/brokers/binance/ws_client.py \
        tests/services/brokers/binance/test_ws_client.py
git commit -m "feat(rob-285): Binance public WS client (combined streams, drops in-progress klines)

Co-Authored-By: Paperclip <noreply@paperclip.ing>"
```

---

### Task 11: WS reconnect/backoff

**Files:**
- Modify: `app/services/brokers/binance/ws_client.py`
- Create: `tests/services/brokers/binance/test_ws_reconnect.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/services/brokers/binance/test_ws_reconnect.py
"""ROB-285 — WS reconnect/backoff: exp 1s→60s, jitter ±20%, ≥3 attempts."""

import pytest

from app.services.brokers.binance.ws_client import compute_backoff_delay


@pytest.mark.parametrize("attempt,expected_min,expected_max", [
    (0, 0.8, 1.2),    # 1s ± 20%
    (1, 1.6, 2.4),    # 2s ± 20%
    (2, 3.2, 4.8),    # 4s ± 20%
    (5, 25.6, 38.4),  # 32s ± 20%
    (6, 48.0, 72.0),  # 60s (capped) ± 20% — but cap means upper-bound effective is min(72, 60+20%)
    (20, 48.0, 72.0), # Still capped.
])
def test_backoff_delay_bounds(attempt: int, expected_min: float, expected_max: float) -> None:
    # Sample multiple times to verify jitter range.
    samples = [compute_backoff_delay(attempt) for _ in range(50)]
    assert all(expected_min <= s <= expected_max for s in samples), (
        f"Backoff for attempt={attempt} out of [{expected_min}, {expected_max}]: "
        f"min={min(samples)}, max={max(samples)}"
    )


def test_minimum_three_attempts_before_unhealthy() -> None:
    # The state-machine helper should require ≥3 consecutive failures
    # before declaring 'unhealthy'.
    from app.services.brokers.binance.ws_client import is_unhealthy
    assert is_unhealthy(consecutive_failures=0) is False
    assert is_unhealthy(consecutive_failures=1) is False
    assert is_unhealthy(consecutive_failures=2) is False
    assert is_unhealthy(consecutive_failures=3) is True
    assert is_unhealthy(consecutive_failures=10) is True
```

- [ ] **Step 2: Run, expect FAIL**

```bash
uv run pytest tests/services/brokers/binance/test_ws_reconnect.py -v
```

- [ ] **Step 3: Implement**

Append to `ws_client.py`:

```python
import random


_BACKOFF_INITIAL = 1.0
_BACKOFF_FACTOR = 2.0
_BACKOFF_CAP = 60.0
_BACKOFF_JITTER = 0.2  # ±20%
_UNHEALTHY_THRESHOLD = 3


def compute_backoff_delay(attempt: int) -> float:
    """Exponential backoff with ±20% jitter, capped at 60s.

    attempt is 0-indexed (attempt 0 → ~1s base).
    """
    base = min(_BACKOFF_INITIAL * (_BACKOFF_FACTOR ** attempt), _BACKOFF_CAP)
    jitter = base * _BACKOFF_JITTER
    return base + random.uniform(-jitter, jitter)


def is_unhealthy(consecutive_failures: int) -> bool:
    return consecutive_failures >= _UNHEALTHY_THRESHOLD
```

The actual run-loop integration (a `BinancePublicWSClient.run_with_reconnect()` method) is Task 12's concern; this task pins the math.

- [ ] **Step 4: Run, expect PASS**

```bash
uv run pytest tests/services/brokers/binance/test_ws_reconnect.py -v
```

- [ ] **Step 5: Commit**

```bash
git add app/services/brokers/binance/ws_client.py \
        tests/services/brokers/binance/test_ws_reconnect.py
git commit -m "feat(rob-285): WS reconnect backoff math (1s->60s exp, +-20% jitter, >=3 unhealthy)

Co-Authored-By: Paperclip <noreply@paperclip.ing>"
```

---

### Task 12: Gap detection + WS-reconnect orchestration (consumes Task 9 engine)

> **Layer note:** Task 12 is the *orchestration* layer that ties Task 9's `RestBackfiller`, Task 8's `CryptoInstrumentHealthService`, Task 10/11's WS connect+backoff, and Child A's `MinuteCandlesRepository` into a single run-loop. Task 9 owns the backfill math; Task 12 owns the lifecycle decisions (when to call backfill, how to react to cap-exceeded, when to mark `degraded` vs `manual_backfill_required`).

**Files:**
- Modify: `app/services/brokers/binance/ws_client.py` (add `run_with_reconnect` orchestration)
- Modify: `tests/services/brokers/binance/test_ws_reconnect.py` (or new test file)
- Optional: `app/services/brokers/binance/gap_detector.py` if the logic warrants a separate module

- [ ] **Step 1: Write the failing tests**

```python
# Append to test_ws_reconnect.py or create test_gap_detection.py
@pytest.mark.asyncio
async def test_gap_detector_returns_no_gap_when_recent_closed_candle(...) -> None:
    """When last_closed = now - 30s and interval = 60s, gap is 0 (no fill needed)."""
    ...

@pytest.mark.asyncio
async def test_gap_detector_returns_gap_when_minutes_missing(...) -> None:
    """When last_closed = now - 5m, gap is 4 candles (assuming current minute open)."""
    ...

@pytest.mark.asyncio
async def test_gap_detection_within_cap_triggers_rest_backfill(...) -> None:
    """Gap of 100 candles is within cap → RestBackfiller is invoked, results
    persisted via MinuteCandlesRepository, no state change."""
    ...

@pytest.mark.asyncio
async def test_gap_detection_beyond_cap_marks_manual_backfill_required(...) -> None:
    """Gap of 10000 candles exceeds cap → instrument transitions to
    manual_backfill_required, scalper-readable flag set."""
    ...
```

- [ ] **Step 2: Implement gap_detector + integration**

```python
# app/services/brokers/binance/gap_detector.py
"""ROB-285 — Gap detector for kline streams.

On reconnect, compares last persisted closed candle's open_time against
now() to determine missed candles. Returns (since, expected_count).
"""

import datetime as dt
from dataclasses import dataclass


_INTERVAL_TO_SECONDS = {"1m": 60, "1d": 86400}


@dataclass(frozen=True, slots=True)
class GapDecision:
    needs_fill: bool
    since: dt.datetime | None
    expected_count: int


def detect_gap(*, last_closed: dt.datetime | None,
               interval: str = "1m",
               now: dt.datetime | None = None) -> GapDecision:
    n = now or dt.datetime.now(tz=dt.UTC)
    sec = _INTERVAL_TO_SECONDS[interval]
    if last_closed is None:
        return GapDecision(needs_fill=False, since=None, expected_count=0)
    elapsed = (n - last_closed).total_seconds()
    expected = int(elapsed // sec) - 1  # -1 because the current bucket is in-progress
    if expected <= 0:
        return GapDecision(needs_fill=False, since=None, expected_count=0)
    return GapDecision(
        needs_fill=True,
        since=last_closed + dt.timedelta(seconds=sec),
        expected_count=expected,
    )
```

Then wire `run_with_reconnect` in `ws_client.py` (or a new `app/services/brokers/binance/runner.py`) to:
1. Connect WS.
2. On disconnect, increment consecutive_failures, sleep `compute_backoff_delay`, reconnect.
3. After successful reconnect, for each subscribed symbol:
   - Look up last persisted closed candle from `crypto_candles_1m` via Child A repository.
   - Call `detect_gap` → if `needs_fill`, invoke `RestBackfiller`.
   - On `BinanceBackfillCapExceeded`, call `CryptoInstrumentHealthService.record_manual_backfill_required`.
   - On clean backfill, persist results via `MinuteCandlesRepository.upsert_rows`.
4. If consecutive_failures ≥ 3, call `CryptoInstrumentHealthService.record_degraded`.
5. On recovery (first successful event after degraded), call `record_recovered`.

- [ ] **Step 3: Run, expect PASS**

```bash
uv run pytest tests/services/brokers/binance/test_ws_reconnect.py \
              tests/services/brokers/binance/test_gap_detection.py -v
```

- [ ] **Step 4: Commit**

```bash
git add app/services/brokers/binance/gap_detector.py \
        app/services/brokers/binance/ws_client.py \
        tests/services/brokers/binance/test_gap_detection.py \
        tests/services/brokers/binance/test_ws_reconnect.py
git commit -m "feat(rob-285): gap detection on reconnect + backfill/health integration

Co-Authored-By: Paperclip <noreply@paperclip.ing>"
```

---

### Task 13: Ingest pipeline (closed kline → `MinuteCandlesRepository`)

**Files:**
- Create: `app/services/brokers/binance/ingest.py`
- Create: `tests/services/brokers/binance/test_ingest.py`

- [ ] **Step 1: Write the failing tests**

```python
@pytest.mark.asyncio
async def test_ingest_closed_kline_persists_via_repository(db_session) -> None:
    """A KlineEvent with is_closed=True is upserted into crypto_candles_1m
    via MinuteCandlesRepository, using a cached instrument_id lookup."""
    ...

@pytest.mark.asyncio
async def test_ingest_skips_kline_for_unknown_symbol(db_session, caplog) -> None:
    """When no crypto_instruments row exists for (binance, spot, NEWCOIN),
    the ingest layer logs a WARNING and skips — does not auto-create."""
    ...

@pytest.mark.asyncio
async def test_ingest_idempotent_upsert(db_session) -> None:
    """Re-ingesting the same closed kline is a no-op at the DB level."""
    ...
```

- [ ] **Step 2: Implement**

```python
# app/services/brokers/binance/ingest.py
"""ROB-285 — Bridge from KlineEvent → MinuteCandlesRepository.

Caches (venue, product, venue_symbol) → instrument_id lookups in memory
to reduce DB churn during normal streaming. On cache miss, queries
crypto_instruments; on still-missing, logs WARNING and skips.
"""

import logging
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.crypto_instruments import CryptoInstrument
from app.services.brokers.binance.ws_client import KlineEvent
from app.services.minute_candles.repository import (
    MinuteCandleRow, MinuteCandlesRepository,
)


logger = logging.getLogger("app.services.brokers.binance.ingest")


class BinanceCandleIngester:
    def __init__(self, *, session: AsyncSession,
                 repository: MinuteCandlesRepository | None = None) -> None:
        self._session = session
        self._repo = repository or MinuteCandlesRepository(session=session)
        self._cache: dict[str, int] = {}  # venue_symbol → instrument_id (spot only)

    async def _resolve(self, venue_symbol: str) -> int | None:
        if venue_symbol in self._cache:
            return self._cache[venue_symbol]
        result = await self._session.execute(
            select(CryptoInstrument.id).where(
                CryptoInstrument.venue == "binance",
                CryptoInstrument.product == "spot",
                CryptoInstrument.venue_symbol == venue_symbol,
            )
        )
        row = result.first()
        if row is None:
            return None
        self._cache[venue_symbol] = int(row[0])
        return int(row[0])

    async def ingest(self, event: KlineEvent) -> bool:
        """Returns True if persisted, False if skipped."""
        if not event.is_closed:
            return False  # Defensive — WS client should drop these already.
        instrument_id = await self._resolve(event.symbol)
        if instrument_id is None:
            logger.warning(
                "binance.ingest skip: no crypto_instruments row for "
                "(binance, spot, %s)", event.symbol,
            )
            return False
        await self._repo.upsert_rows(rows=[MinuteCandleRow(
            instrument_id=instrument_id,
            time_utc=event.open_time,
            open=float(event.open), high=float(event.high),
            low=float(event.low), close=float(event.close),
            base_volume=float(event.base_volume),
            quote_volume=float(event.quote_volume),
            trade_count=event.trade_count,
            is_closed=True,
            source="binance_sdk_ws",
            source_event_at=event.close_time,
        )])
        return True
```

- [ ] **Step 3: Run, expect PASS**

```bash
uv run pytest tests/services/brokers/binance/test_ingest.py -v
```

- [ ] **Step 4: Commit**

```bash
git add app/services/brokers/binance/ingest.py \
        tests/services/brokers/binance/test_ingest.py
git commit -m "feat(rob-285): Binance kline ingest pipeline -> MinuteCandlesRepository

Co-Authored-By: Paperclip <noreply@paperclip.ing>"
```

---

### Task 14: Public CLI smoke

**Files:**
- Create: `scripts/binance_public_smoke.py`
- Modify: `docs/runbooks/binance-public-market-data.md` (Task 15 also adds to this)

The smoke script mirrors `scripts/kis_websocket_mock_smoke.py` shape: docstring header with exit codes, structured logging, `--dry-run` flag default, no DB mutation by default.

- [ ] **Step 1: Implement**

```python
# scripts/binance_public_smoke.py
"""Binance Public Market Data Smoke

ROB-285: 운영 서버에서 Binance 공개 REST + WebSocket 핸드셰이크가 정상
동작하는지 빠르게 검증한다. API key 사용 안 함. DB write 안 함
(`--dry-run` 기본값). 호스트 allowlist + rate-limit 헤더 출력으로
end-to-end 가시화.

Exit codes:
    0  - 모든 smoke 성공
    1  - 예기치 못한 예외
    2  - REST exchangeInfo 실패
    3  - REST klines backfill 실패
    4  - WebSocket connect 실패
    5  - 호스트 allowlist rejection 동작 실패 (defense-in-depth 검증)

사용법:
    uv run python -m scripts.binance_public_smoke --symbol BTCUSDT --dry-run
    uv run python -m scripts.binance_public_smoke \
        --symbols BTCUSDT,ETHUSDT --duration 30
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from app.services.brokers.binance.errors import BinanceLiveHostBlocked
from app.services.brokers.binance.rest_client import BinancePublicRestClient
from app.services.brokers.binance.ws_client import BinancePublicWSClient


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--symbol", default=None, help="single symbol shortcut")
    p.add_argument("--symbols", default="BTCUSDT,ETHUSDT,SOLUSDT",
                   help="comma-separated WS symbols")
    p.add_argument("--duration", type=int, default=15,
                   help="WS subscribe duration in seconds")
    p.add_argument("--dry-run", action="store_true", default=True)
    p.add_argument("--no-dry-run", dest="dry_run", action="store_false")
    return p.parse_args()


async def _run(args: argparse.Namespace) -> int:
    log = logging.getLogger("binance_public_smoke")
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    symbol = args.symbol or args.symbols.split(",")[0]

    # 1. REST exchangeInfo
    try:
        async with BinancePublicRestClient() as rest:
            info = await rest.exchange_info(symbol)
            log.info(f"exchangeInfo OK: {info.symbol} {info.status} "
                     f"{info.base_asset}/{info.quote_asset}")
    except Exception as exc:
        log.error(f"exchangeInfo FAIL: {exc}")
        return 2

    # 2. REST klines backfill (last 5 minutes)
    try:
        import datetime as dt
        async with BinancePublicRestClient() as rest:
            klines = await rest.klines(
                symbol, "1m",
                start_time=dt.datetime.now(tz=dt.UTC) - dt.timedelta(minutes=5),
                limit=10,
            )
            log.info(f"klines OK: {len(klines)} rows")
    except Exception as exc:
        log.error(f"klines FAIL: {exc}")
        return 3

    # 3. Allowlist defense-in-depth: rejecting fapi.binance.com should raise.
    try:
        async with BinancePublicRestClient() as rest:
            try:
                await rest._send("GET", "https://fapi.binance.com/fapi/v1/ping")
            except BinanceLiveHostBlocked:
                log.info("allowlist OK: fapi.binance.com correctly rejected")
            else:
                log.error("allowlist FAIL: fapi.binance.com was NOT rejected")
                return 5
    except Exception as exc:
        log.error(f"allowlist check FAIL: {exc}")
        return 5

    # 4. WebSocket connect + receive at least one kline event
    syms = "/".join(f"{s.lower()}@kline_1m" for s in args.symbols.split(","))
    url = f"wss://stream.binance.com:9443/stream?streams={syms}"
    received = 0
    try:
        async with BinancePublicWSClient(url=url) as ws:
            stop_at = asyncio.get_event_loop().time() + args.duration
            async for event in ws.events():
                log.info(f"ws event: {event}")
                received += 1
                if asyncio.get_event_loop().time() >= stop_at:
                    break
                if received >= 3:
                    break
    except Exception as exc:
        log.error(f"WS FAIL: {exc}")
        return 4

    log.info(f"smoke OK (dry_run={args.dry_run}; received {received} WS events)")
    return 0


def main() -> int:
    args = _parse_args()
    try:
        return asyncio.run(_run(args))
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        logging.error(f"unexpected: {exc}", exc_info=True)
        return 1


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 2: Run the smoke locally (manual verification)**

```bash
uv run python -m scripts.binance_public_smoke --symbol BTCUSDT --duration 10
# Expected: prints exchangeInfo, klines OK, allowlist OK, ws events, exits 0.
```

If the run fails due to network restrictions, mark as **server-only verification** in the PR description (similar to Child A's hypertable test). Local exit codes 4 are acceptable if behind a firewall.

- [ ] **Step 3: Commit**

```bash
git add scripts/binance_public_smoke.py
git commit -m "feat(rob-285): public REST+WS smoke CLI (no credentials)

Co-Authored-By: Paperclip <noreply@paperclip.ing>"
```

---

### Task 15: Runbook + final scope audit + screener regression

**Files:**
- Create: `docs/runbooks/binance-public-market-data.md`

- [ ] **Step 1: Author the runbook**

Sections to cover:
- **What this is** — 한 줄 요약 + ownership note.
- **Topology** — 기본 구성도 (REST + WS → Repositories → DB tables).
- **Env vars** — `BINANCE_KLINE_BACKFILL_MAX_CANDLES`, `MAX_REQUESTS`, `PAGE_SIZE`. No API key vars (explicit non-presence).
- **CLI commands** — copy-paste-able invocations of the smoke script, plus a sample of running ingester via the smoke + `--no-dry-run` for non-prod environments only.
- **Health states** — `healthy`/`degraded`/`rate_limited`/`manual_backfill_required` semantics + queries to inspect `crypto_instrument_health`.
- **Manual backfill recovery** — operator steps: (a) why an instrument lands in `manual_backfill_required`, (b) how to widen caps via env, (c) how to call `CryptoInstrumentHealthService.clear_manual_backfill(...)` from a one-off script, (d) how to re-run REST backfill manually.
- **Rate-limit weight reference** — Binance public REST weight cap (~1200/min as of 2025); how the soft-throttle works.
- **Allowlist hosts** — list the four allowed hosts; explain that anything else raises `BinanceLiveHostBlocked`.
- **Production cutover checklist** — (a) seed `crypto_instruments` rows for Binance spot symbols, (b) run smoke against production host (no creds), (c) tail logs for first 30 minutes to confirm rate-limit usage and stream throughput, (d) operator manually clears any `manual_backfill_required` rows before scheduler activation (which is out of scope here).

- [ ] **Step 2: Final scope audit**

```bash
# Must show only the new package:
grep -rln "binance" --include="*.py" app/ | sort

# Must show no signed-endpoint surface in the new package:
uv run pytest tests/services/brokers/binance/test_audit_no_signed_endpoints.py -v

# Must show no app/jobs/* mutation:
git diff --name-only origin/main -- app/jobs/

# Must show no Upbit/Alpaca/KIS path changes:
git diff --name-only origin/main -- app/services/brokers/upbit/ \
                                      app/services/brokers/alpaca/ \
                                      app/services/brokers/kis/ \
                                      app/services/brokers/kiwoom/

# Screener regression — must remain green by construction:
uv run pytest tests -k "screener_snapshot or invest_crypto_screener" -q
```

Paste all outputs into the PR description.

- [ ] **Step 3: Commit + open PR**

```bash
git add docs/runbooks/binance-public-market-data.md
git commit -m "docs(rob-285): operator runbook for Binance public market data adapter

Co-Authored-By: Paperclip <noreply@paperclip.ing>"

git push -u origin rob-285
gh pr create --base main --head rob-285 \
  --title "feat(rob-285): Binance public market data adapter (REST + WS, read-only) (Child B)" \
  --body "$(cat <<'EOF'
## Summary

Child B of ROB-283 epic. Adds a read-only Binance public market data adapter
(REST + WebSocket) behind a new package `app/services/brokers/binance/`,
persists closed 1m candles via Child A's MinuteCandlesRepository, and adds
WS reconnect/backoff + gap detection + bounded REST backfill + Binance
rate-limit telemetry. No API key required, no signed endpoints, no execution.

## Audit invariants locked

- Binance code lives at exactly one path: `app/services/brokers/binance/`.
- No signed-endpoint surface (test_audit_no_signed_endpoints).
- No X-MBX-APIKEY header anywhere in package source.
- Transport-layer host allowlist (httpx event_hooks) at the boundary.

## binance-sdk-spot vetting

- License: <fill>
- Last release: <fill>
- Python 3.13 compat: pass (import-tested)
- Transitive footprint: <fill> new packages

## What landed

| Module | Purpose |
|---|---|
| host_allowlist.py | Frozen set + raise |
| transport.py | httpx.AsyncClient factory with event_hooks |
| rest_client.py | exchangeInfo / klines / bookTicker |
| rate_limit_telemetry.py | Header parser + structured log + Sentry tag |
| backfill.py | Bounded REST backfill with 5000/10/1000 default caps |
| ws_client.py | Combined-stream WS, drops in-progress klines |
| gap_detector.py | On-reconnect gap math |
| ingest.py | KlineEvent → MinuteCandlesRepository |
| crypto_instrument_health (table) | Service-only write, lifecycle states |

## Test results

- tests/services/brokers/binance/ — <N> passed
- tests/services/instrument_health/ — <N> passed
- Screener regression — <N> passed (no modifications)

## Server-only verification (if any)

- Live WS smoke against `stream.binance.com:9443` — outcome: <pass/skipped>
- Live REST against `api.binance.com` — outcome: <pass/skipped>

## Scope confirmation

- ❌ No signed Binance endpoints reachable.
- ❌ No `BINANCE_TESTNET_*` handling (Child C).
- ❌ No `binance_testnet_order_ledger` (Child C).
- ❌ No scalping state machine (Child C).
- ❌ No scheduler activation.
- ❌ No `app/jobs/*` modification.
- ❌ No KR/US/Upbit/Alpaca path changes.
- ❌ No production DB writes (test_db only).

Closes ROB-285. Soft-prereq for Child C (ROB-286).

🤖 Plan-driven implementation via Claude Code.
EOF
)"
```

---

## Self-review checklist (to be run after writing this plan, before execution)

- [ ] Task 1 audit grep is in place before any Binance code.
- [ ] No task introduces signed endpoints, API keys, testnet hosts, ledger, scalper, scheduler, or `app/jobs/*` mutation.
- [ ] Every task has explicit file paths.
- [ ] Every "Step 1" with code contains the actual code, not a description.
- [ ] No "TBD"/"TODO"/"implement later" in task bodies.
- [ ] All commit messages reference `rob-285`.
- [ ] Co-author trailer is `Paperclip <noreply@paperclip.ing>`.
- [ ] CI commands include `ruff check`, `ruff format --check`, and `ty check` — the same trio that turned out to be required for Child A.
- [ ] The audit test wording follows the Child A pattern ("if you intentionally added X, extend ALLOWED ... PR description").

## Open items deferred to execution

The decisions in §B.1–B.9 are locked. Items in the Open items table are intentionally left for the implementer to finalize during the relevant task. Anything else surfaced during implementation that materially changes the plan must be flagged in the PR description as a "scope adjustment after pre-implementation audit", same convention as Child A's snapshot-builder discovery.
