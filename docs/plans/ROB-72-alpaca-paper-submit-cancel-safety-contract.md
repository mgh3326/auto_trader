# ROB-72 — Alpaca Paper Submit/Cancel MCP Tools: Safety Contract Design

Status: design_only
Issue: ROB-72
Branch: feature/ROB-72-alpaca-paper-submit-cancel-safety-contract
Planner/Reviewer: Claude Opus
Implementer: (future issue — see §10)
Orchestrator: Hermes (no implementation work)

## 1. Goal

Define the **safety contract** for future guarded MCP tools that will allow Hermes
to submit and cancel single orders on the Alpaca **paper** endpoint only.

This document is **design-only**. It contains no implementation. The two tools
described here (`alpaca_paper_submit_order`, `alpaca_paper_cancel_order`) must
**not** be implemented until a dedicated follow-up issue picks up this plan.

## 2. Non-goals (this issue)

- ❌ Implementing `alpaca_paper_submit_order` or `alpaca_paper_cancel_order`.
- ❌ Editing Python runtime code, tests, or any file under `app/` or `tests/`.
- ❌ Calling any paper or live order endpoint; no orders are placed, submitted,
  cancelled, replaced, or modified.
- ❌ Re-introducing registry, profile, strategy, or DB-row modeling for Alpaca orders.
- ❌ Reading or printing credentials.
- ❌ Live endpoint support (`https://api.alpaca.markets`).
- ❌ Replace/modify order tools.
- ❌ Cancel-all, by-symbol, by-status, or wildcard cancel semantics.
- ❌ Generic order-routing through legacy `ORDER_TOOL_NAMES` tools.

## 3. Context: existing Alpaca paper foundation

ROB-57 (merged) delivered:
- `app/services/brokers/alpaca/` — paper-only service adapter with paper endpoint guard.
- `app/core/config.py` — `alpaca_paper_*` settings namespace.
  - `ALPACA_PAPER_BASE_URL` is **exactly** `https://paper-api.alpaca.markets` (no `/v2`).
    Any other value raises `AlpacaPaperConfigurationError` at settings-load time.
- `app/mcp_server/tooling/alpaca_paper.py` — read-only MCP tools registered under
  `ALPACA_PAPER_READONLY_TOOL_NAMES`.
- Guard tests in `tests/test_mcp_alpaca_paper_tools.py` and
  `tests/test_alpaca_paper_isolation.py` that assert mutating tools are **not** registered.

ROB-69 (merged) added the explicit read-only tool set.

### 3.1 Key invariant carried forward

`ALPACA_PAPER_BASE_URL = "https://paper-api.alpaca.markets"` — no trailing `/v2`; the
service appends `/v2` to each request path internally.  The live endpoint
`https://api.alpaca.markets` is permanently forbidden as a trading base URL.

## 4. Future tool signatures

Both tools share the same two-phase design: a **dry-run preview** phase that
returns a confirm token and a **confirmed execution** phase that requires the token.

### 4.1 `alpaca_paper_submit_order`

```python
async def alpaca_paper_submit_order(
    symbol: str,
    side: str,                         # "buy" | "sell"
    type: str,                         # "market" | "limit" | "stop" | "stop_limit"
    time_in_force: str,                # "day" | "gtc" | "ioc" | "fok"
    qty: str | None = None,            # decimal string, mutually exclusive with notional
    notional: str | None = None,       # USD decimal string, mutually exclusive with qty
    limit_price: str | None = None,    # required when type in ("limit", "stop_limit")
    stop_price: str | None = None,     # required when type in ("stop", "stop_limit")
    client_order_id: str | None = None,
    dry_run: bool = True,
    confirm_token: str | None = None,
) -> dict[str, Any]: ...
```

### 4.2 `alpaca_paper_cancel_order`

```python
async def alpaca_paper_cancel_order(
    order_id: str,          # explicit non-blank UUID; no wildcards or cancel-all
    dry_run: bool = True,
    confirm_token: str | None = None,
) -> dict[str, Any]: ...
```

Both tools should use a consistent response envelope:

- Success preview: `{"success": True, "dry_run": True, "preview": ..., "confirm_token": ..., "expires_at": ...}`.
- Success execution: `{"success": True, "dry_run": False, "order": ...}` for submit, or
  `{"success": True, "dry_run": False, "cancelled_order_id": ...}` for cancel.
- Rejection: `{"success": False, "dry_run": True, "error": <stable_key>, "message": ...}`.

## 5. Two-phase preview/confirm model

### 5.1 Rationale

Direct execution of order mutations under Hermes automation creates a risk surface
where a misunderstood instruction or prompt-injection could immediately affect
real (even paper) account state.  The two-phase model forces a second, explicit
confirmation before any broker mutation occurs.

### 5.2 Phase 1 — dry-run preview (default)

When `dry_run=True` (the default):

1. Validate all inputs (side, type, TIF, qty/notional exclusivity, price fields,
   caps, symbol format, client_order_id format).
2. Resolve asset class from Alpaca `/v2/assets/{symbol}` — do **not** trust the
   caller's classification.
3. Apply market-hours policy (§7) and cap checks (§6) — reject here if violated.
4. Build the canonical order payload dict (JSON-serialisable, deterministic key order).
5. Compute a HMAC-SHA256 confirm token over `canonical_payload + issued_at + expires_at`
   using a server-side confirmation secret that is independent of Alpaca API keys and is
   never returned, logged, or derived from caller input.  The token is opaque to the caller;
   its expiry window is **5 minutes**.
6. Return `{"dry_run": True, "preview": <canonical_payload>, "confirm_token": <token>,
   "expires_at": <ISO-8601 UTC>}`.

No broker HTTP mutation is performed in Phase 1.

### 5.3 Phase 2 — confirmed execution

When `dry_run=False`:

1. `confirm_token` must be present, non-blank, and must **not** have expired.
2. Re-derive the canonical payload from the same request arguments (excluding
   `dry_run` and `confirm_token`).
3. Re-compute the expected HMAC; compare constant-time with the provided token.
   Mismatch → reject with `{"error": "confirm_token_mismatch", "dry_run": True}`.
4. Re-validate all business-logic rules (caps, hours, asset class) — they must still
   hold at execution time.
5. Assign or preserve `client_order_id` (§8).
6. Call the Alpaca paper service method; propagate `AlpacaPaperRequestError` as an
   error response dict.
7. Return `{"dry_run": False, "order": <serialised Order>, "client_order_id": ...}`.

### 5.4 Token rejection rules

The tool must reject (return an error dict, not raise) when any of the following hold:

| Condition | Error key |
|-----------|-----------|
| `dry_run=False` and `confirm_token` is `None` or blank | `confirm_token_required` |
| Token HMAC mismatch | `confirm_token_mismatch` |
| Token timestamp window expired | `confirm_token_expired` |
| Request args differ from token payload | `confirm_token_mismatch` |

Rejection always sets `"dry_run": True` in the returned dict to signal that no
mutation occurred.

## 6. Caps and config defaults

These defaults must be overridable via `app/core/config.py` env-backed settings.
The implementation issue should add them as optional fields with the listed defaults.

| Setting field name (future)                     | Default | Description |
|-------------------------------------------------|---------|-------------|
| `alpaca_paper_max_notional_usd`                 | `2000`  | Maximum USD notional per order |
| `alpaca_paper_max_qty`                          | `25`    | Maximum share/unit quantity per order |
| `alpaca_paper_max_open_orders`                  | `5`     | Maximum concurrent open orders |
| `alpaca_paper_symbol_allowlist`                 | `[]`    | If non-empty, only these symbols are accepted |

Cap violations during Phase 1 (dry-run) must reject immediately with an error dict
describing which cap was violated, so the operator can adjust before attempting
Phase 2.

## 7. Market-hours and asset-class policy

### 7.1 Asset class resolution

The tool must call `GET /v2/assets/{symbol}` on the paper endpoint to resolve the
asset class before applying any policy.  The caller's classification is ignored.

### 7.2 US equity

- Regular session only by default (`09:30–16:00 ET`, market trading days).
- The implementation should derive session state from Alpaca paper `/v2/clock` plus
  `/v2/calendar` (or an equivalent market-calendar abstraction), not from local wall-clock
  assumptions, so early closes and holidays are respected.
- `market` orders outside regular hours → rejected during Phase 1.
- Extended-hours execution requires a separate future explicit parameter
  (e.g. `extended_hours: bool = False`) and a dedicated review/approval cycle not
  covered by this document.

### 7.3 Crypto

- Available 24/7 on the paper endpoint.
- Still subject to: paper endpoint guard, cap checks, preview-token model.

### 7.4 Unsupported asset classes

Any asset class other than `us_equity` or `crypto` → rejected during Phase 1 with
`{"error": "asset_class_not_supported", "asset_class": <resolved class>}`.

## 8. Idempotency and `client_order_id`

- If the caller provides `client_order_id`:
  - Must match `[A-Za-z0-9_-]+`, max 48 characters.
  - Violation → rejected during Phase 1.
  - The provided value is used verbatim; it is embedded in the confirm-token payload
    so a tampered client_order_id invalidates the token.
- If omitted:
  - Implementation generates a deterministic, project-prefixed ID of the form
    `rob72-<hex16>` derived from a server-side salted canonical payload hash.  The salt
    avoids leaking payload structure across accounts while keeping retry idempotency.
  - The generated ID is included in the Phase 1 preview response so the caller can
    correlate subsequent queries.
  - Retry safety: the same canonical payload always produces the same generated ID,
    so a retry after a network timeout cannot create a duplicate on the broker if the
    original order was accepted (Alpaca rejects duplicate `client_order_id`).

## 9. Forbidden paths

The following patterns are explicitly forbidden in the future implementation:

| Forbidden | Reason |
|-----------|--------|
| `https://api.alpaca.markets` as any base URL | Live endpoint; violates I2 from ROB-57 |
| Generic order-routing via `ORDER_TOOL_NAMES` | Must not conflate with legacy tools |
| `cancel_all_orders()` or cancel by symbol/status/wildcard | Cancel must be explicit by `order_id` only |
| `replace_order()` / `modify_order()` | Out of scope; separate safety review needed |
| Importing Alpaca package from `app/routers/*` | Isolation invariant from ROB-57 §6 |
| Importing from `app/mcp_server/*` outside `alpaca_paper.py` | Isolation invariant from ROB-57 §6 |
| Registry/profile/strategy DB-row modeling for Alpaca orders | Not needed; keep service thin |
| Reading or logging raw credentials | Security invariant |
| Bypassing Phase 1 with a caller-precomputed `confirm_token` on first call | Impossible when HMAC secret is server-side only; reject as `confirm_token_mismatch` |

## 10. Test matrix for future implementation

The implementation issue must include the following test groups.
All tests must be `@pytest.mark.unit` and hermetic (no real network).

### 10.1 Guard tests — must remain green throughout this issue

Until the implementation issue deliberately changes them, these existing tests
**must stay green**:

- `tests/test_mcp_alpaca_paper_tools.py::test_no_alpaca_live_or_mutating_alpaca_order_tools_registered`
  — asserts `alpaca_paper_submit_order` and `alpaca_paper_cancel_order` are NOT
  registered; must remain green until the implementation issue adds them.
- `tests/test_alpaca_paper_isolation.py::test_only_explicit_readonly_mcp_tool_imports_alpaca_paper`
  — asserts only `alpaca_paper.py` imports the Alpaca package.
- `tests/test_alpaca_paper_isolation.py::test_no_alpaca_live_settings_field`
  — asserts no `alpaca_live_*` settings fields.

When the implementation issue registers the new tools, the guard test
`test_no_alpaca_live_or_mutating_alpaca_order_tools_registered` must be updated to
remove `alpaca_paper_submit_order` and `alpaca_paper_cancel_order` from its
`forbidden_names` set.

### 10.2 Submit — Phase 1 (dry-run)

- `test_submit_order_dry_run_returns_preview_and_token`
  `dry_run=True`; assert response has `preview`, `confirm_token`, `expires_at`;
  no service `submit_order` call occurred.
- `test_submit_order_dry_run_rejects_bad_side`
  `side="hold"` → `{"error": "invalid_side", ...}`.
- `test_submit_order_dry_run_rejects_qty_and_notional_both_set`
  Both `qty` and `notional` provided → `{"error": "qty_notional_exclusive", ...}`.
- `test_submit_order_dry_run_rejects_limit_order_missing_limit_price`
  `type="limit"`, no `limit_price` → `{"error": "limit_price_required", ...}`.
- `test_submit_order_dry_run_rejects_notional_exceeds_cap`
  `notional="3000"` with default cap 2000 → `{"error": "notional_cap_exceeded", ...}`.
- `test_submit_order_dry_run_rejects_qty_exceeds_cap`
  `qty="30"` with default cap 25 → `{"error": "qty_cap_exceeded", ...}`.
- `test_submit_order_dry_run_rejects_open_orders_cap_exceeded`
  Fake service reports 5 open orders with default cap 5 →
  `{"error": "open_orders_cap_exceeded", ...}` before any submit call.
- `test_submit_order_dry_run_rejects_market_order_outside_regular_hours`
  `type="market"`, asset class `us_equity`, mocked paper clock/calendar outside regular session →
  `{"error": "market_hours_violation", ...}`.
- `test_submit_order_dry_run_rejects_symbol_not_in_allowlist`
  Allowlist configured as `["AAPL"]`; `symbol="TSLA"` → `{"error": "symbol_not_allowed", ...}`.
- `test_submit_order_dry_run_rejects_invalid_client_order_id_format`
  `client_order_id="bad id!"` → `{"error": "invalid_client_order_id", ...}`.
- `test_submit_order_dry_run_rejects_client_order_id_too_long`
  49-char string → `{"error": "invalid_client_order_id", ...}`.
- `test_submit_order_crypto_allowed_outside_regular_hours`
  Asset class `crypto`, `type="market"`, outside regular hours → dry-run succeeds.

### 10.3 Submit — Phase 2 (confirmed execution)

- `test_submit_order_confirmed_calls_service`
  Full round-trip: Phase 1 issues token; same args + token + `dry_run=False` →
  service `submit_order` called once; response has `"dry_run": False`.
- `test_submit_order_missing_token_rejected`
  `dry_run=False`, no token → `{"error": "confirm_token_required", "dry_run": True}`.
- `test_submit_order_mismatched_token_rejected`
  `dry_run=False`, token issued for different symbol → `{"error": "confirm_token_mismatch", "dry_run": True}`.
- `test_submit_order_expired_token_rejected`
  Advance clock past 5-minute window → `{"error": "confirm_token_expired", "dry_run": True}`.
- `test_submit_order_client_order_id_generated_deterministically`
  No `client_order_id` provided; two calls with identical payload produce the same
  generated ID in Phase 1 preview.
- `test_submit_order_caller_client_order_id_preserved_through_confirm`
  Phase 1 preview includes the caller's `client_order_id`; Phase 2 submission carries
  the same value to the service.

### 10.4 Cancel — Phase 1 (dry-run)

- `test_cancel_order_dry_run_returns_preview_and_token`
  `dry_run=True`; assert response has `preview`, `confirm_token`, `expires_at`; no
  service `cancel_order` call occurred.
- `test_cancel_order_dry_run_rejects_blank_order_id`
  `order_id="   "` → `{"error": "order_id_required", ...}`.

### 10.5 Cancel — Phase 2 (confirmed execution)

- `test_cancel_order_confirmed_calls_service`
  Full round-trip: Phase 1 → token; same `order_id` + token + `dry_run=False` →
  service `cancel_order` called once.
- `test_cancel_order_missing_token_rejected`
  `dry_run=False`, no token → `{"error": "confirm_token_required", "dry_run": True}`.
- `test_cancel_order_mismatched_order_id_rejected`
  Token was issued for `order-A`; execution request uses `order-B` →
  `{"error": "confirm_token_mismatch", "dry_run": True}`.

### 10.6 Isolation / registration guard

- `test_submit_and_cancel_registered_after_implementation`
  Once registered, the implementation should introduce a clear mutating-tool partition such
  as `ALPACA_PAPER_MUTATING_TOOL_NAMES` (or rename the aggregate to avoid adding mutating
  tools to the existing `ALPACA_PAPER_READONLY_TOOL_NAMES` set).
  *(This test is added by the implementation issue; it does not yet exist.)*
- `test_no_live_or_replace_or_modify_tools_ever_registered`
  `alpaca_paper_replace_order`, `alpaca_paper_modify_order`, `alpaca_live_*` → always
  absent from registered tool names.

## 11. Production deploy and smoke checklist

This checklist is for the **future implementation issue**.

### 11.1 Pre-deploy

- [ ] All tests in §10 pass (`uv run pytest tests/test_mcp_alpaca_paper_tools.py tests/test_alpaca_paper_isolation.py -q`).
- [ ] `make lint && make typecheck` pass with zero new errors.
- [ ] Confirm `ALPACA_PAPER_BASE_URL` in environment is exactly
      `https://paper-api.alpaca.markets` (no `/v2`).
- [ ] Confirm `alpaca_live_*` settings fields absent from `Settings.model_fields`.

### 11.2 Smoke — dry-run only (no mutation)

After deploy to paper environment, operator runs dry-run smoke only:

```python
# Example — dry-run only; no real order submitted
result = await alpaca_paper_submit_order(
    symbol="AAPL",
    side="buy",
    type="limit",
    time_in_force="day",
    qty="1",
    limit_price="1.00",
    dry_run=True,         # ← explicit; default is already True
)
assert result["dry_run"] is True
assert "confirm_token" in result
assert "preview" in result
```

```python
result = await alpaca_paper_cancel_order(
    order_id="<known-open-order-id>",
    dry_run=True,         # ← explicit; default is already True
)
assert result["dry_run"] is True
assert "confirm_token" in result
```

### 11.3 Confirmed execution smoke (paper only, explicit operator approval required)

Proceed to confirmed execution **only with explicit operator approval** and only
against the paper endpoint after dry-run smoke passes:

```python
# Phase 1
preview = await alpaca_paper_submit_order(
    symbol="AAPL", side="buy", type="limit", time_in_force="day",
    qty="1", limit_price="1.00", dry_run=True,
)
token = preview["confirm_token"]

# Phase 2 — execute only after operator reviews preview
result = await alpaca_paper_submit_order(
    symbol="AAPL", side="buy", type="limit", time_in_force="day",
    qty="1", limit_price="1.00", dry_run=False, confirm_token=token,
)
assert result["dry_run"] is False
assert "order" in result
```

### 11.4 Post-deploy verification

- [ ] `alpaca_paper_list_orders(status="open")` shows the smoke order.
- [ ] Cancel smoke order via `alpaca_paper_cancel_order` (Phase 1 → Phase 2).
- [ ] No `alpaca_live_*` tools appear in the MCP tool registry.
- [ ] Sentry shows no unexpected errors from the new tools.

## 12. Follow-up issue recommendation: split submit and cancel

**Recommendation: split into two separate issues.**

| Issue | Scope | Rationale |
|-------|-------|-----------|
| ROB-72.A — Submit | Implement `alpaca_paper_submit_order` with full preview/confirm, caps, market-hours, idempotency | Submit carries more risk surface; isolating it allows guards, telemetry, and cancellation fallback to be validated before exposing cancel |
| ROB-72.B — Cancel | Implement `alpaca_paper_cancel_order` after ROB-72.A telemetry and guards stabilise | Cancel depends on knowing real `order_id` values; implementing it after submit means real paper orders exist to test against |

**Why not one issue?**

- Submit and cancel have distinct risk surfaces: submit creates state, cancel destroys it.
  A defect in the token model is easier to contain if only one mutation direction is live.
- ROB-72.A ships a complete read-modify loop (submit → list → verify); ROB-72.B can
  validate cancel against real orders rather than synthetic fixtures.
- Smaller PRs are reviewed faster and are easier to revert.

**Why one issue might be acceptable:**

If operator workflows require cancel as a safety escape for submitted orders from day 1,
or if the paper-trading cadence means a two-PR sequence would span too many cycles,
a single combined implementation issue is defensible — provided the PR is reviewed with
extra care for the token model covering both operations.

The default preference is **split (ROB-72.A first, ROB-72.B second)**.
