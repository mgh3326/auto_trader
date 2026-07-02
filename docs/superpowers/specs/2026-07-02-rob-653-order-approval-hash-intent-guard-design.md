# ROB-653 P6-B — Shared `_place_order_impl` approval-hash + KIS pre-send intent guard

**Status:** Design approved 2026-07-02
**Depends on:** ROB-651 (P6-A shared canonical-hash primitives, merged `49ac9070`), ROB-645 (transport double-submit removal, merged `75ee8878`)
**Parent:** ROB-644 (P6)
**Ship as:** single PR

## 1. Problem

Toss/kiwoom_mock/alpaca_paper require `confirm=True` before a real order, but the two
highest-value live paths do not:

- `kis_live_place_order` (`orders_kis_variants.py`) and generic `place_order` (crypto)
  send a real order on `dry_run=False` **alone** — no binding between what was previewed
  and what is sent, and no idempotency protection.
- KIS API has **no client idempotency field** → broker-side dedupe is impossible.
- US/crypto live ledger records are plain inserts (not replay-idempotent).

ROB-651 solved this for Toss with a content-hash approval token + content-based
`clientOrderId`. ROB-653 ports that contract into the **shared** `_place_order_impl`
seam (`order_execution.py`) which covers kis_live KR/US + upbit crypto in one place, and
adds a KIS-specific local reserve-before-send guard because KIS has no broker idempotency key.

## 2. Grounding (verified in code)

- **Shared seam:** `app/mcp_server/tooling/order_execution.py::_place_order_impl`.
  `_build_preview` (~L1145) produces resolved `order_quantity` / tick-snapped `price` /
  `order_amount`; `_execute_and_record` (~L1190) sends and records. The hash guard sits
  between them.
- **`_execute_and_record`** dispatches ledger records by `market_type`:
  `equity_kr → _record_kis_live_order` (kis_live_order_ledger),
  `equity_us`/`crypto → _record_live_order` (live_order_ledger). Actual send is
  `_execute_order` (L696), which fans to `_execute_kr_order` / `_execute_us_order` /
  `_execute_crypto_order`.
- **No preview tool on this path:** unlike Toss (`toss_preview_order` + `toss_place_order`),
  kis_live/generic use one tool with `dry_run`. So `dry_run=True` **is** the preview and
  emits the approval token.
- **P6-A primitives** (`app/mcp_server/tooling/toss_approval.py`) are pure and mostly
  generic: `encode/decode/verify_approval_token`, `derive_approval_digest`,
  `derive_client_order_id`, `trading_day_salt`, `APPROVAL_TTL_SECONDS`. Only
  `build_canonical_payload` uses Toss wire field names. Rollout gate pattern:
  `settings.toss_approval_hash_mode` (`off|optional|warn|required`).
- **Upbit identifier** (`app/services/brokers/upbit/orders.py::_new_order_identifier`) is a
  fresh uuid4 per order; its own docstring names ROB-653 as the content-derivation follow-up.
- **Wrappers:** `kis_live_place_order` and generic `place_order` both delegate to
  `_place_order_variant` → `_place_order_impl`.

## 3. Design

### 3.1 Shared approval helper — `app/mcp_server/tooling/order_approval.py`

New module that **re-imports the pure primitives** from `toss_approval` (no duplication,
P6-A untouched) and adds one generic canonical builder:

```python
build_order_canonical_payload(
    *, market_type: str, symbol: str, side: str,
    order_type: str, quantity: str | None, price: str | None,
) -> dict[str, Any]
```

- `market_type` ∈ {`equity_kr`, `equity_us`, `crypto`}; `symbol` is the normalized symbol.
- `quantity`/`price` are stringified post-normalization wire values (or `None`) so preview
  and place derive an identical digest.
- Distinct digest/token/clientOrderId prefixes from Toss (`p6b` family) to avoid cross-broker
  token confusion.

The token TTL, digest, and idempotency-key derivation reuse the P6-A functions verbatim.

### 3.2 Hash guard in `_place_order_impl` (dry_run-as-preview)

Insert between `_build_preview` and the `dry_run` exit / `_execute_and_record`, using
post-normalization values and a single injected `now = now_kst()`:

1. Build `canonical = build_order_canonical_payload(...)`.
2. `idempotency_key = derive_client_order_id(canonical, market=<kst-market>, now=now, rung=rung)`.
3. **`dry_run=True`:** attach to the dry-run response:
   - `approval_hash = encode_approval_token(canonical, now=now)`
   - `approval_expires_at` (KST ISO, `now + APPROVAL_TTL_SECONDS`)
   - `idempotency_key`
4. **`dry_run=False`:** gate on `settings.order_approval_hash_mode`:
   - `off` → skip entirely.
   - `approval_hash` provided → `verify_approval_token(approval_hash, canonical, now=now)`;
     on failure return `{success: False, error, error_code, diff?}` (fail-closed) — covers
     `invalid_approval_hash`, `approval_expired`, `approval_hash_mismatch`.
   - missing `approval_hash` + `required` → reject (`error_code=approval_hash_required`).
   - missing `approval_hash` + `warn` → log warning, proceed.
   - missing `approval_hash` + `optional` → proceed.
   - **Valid approval_hash = confirm.** This tool has no `confirm` param; at `required` the
     hash requirement is the confirm-parity gate.
5. Thread `idempotency_key` + `approval_digest = derive_approval_digest(canonical)` into
   `_execute_and_record`.

New params on `_place_order_impl` (and forwarded from the wrappers via `_place_order_variant`):
`approval_hash: str | None = None`, `rung: str | int | None = None`.

The `equity_us`/`equity_kr` trading-day salt uses the P6-A market convention
(`us → America/New_York`, else KST); map `equity_us → "us"` and `equity_kr`/`crypto → kr`.

### 3.3 KIS pre-send intent guard — `review.order_send_intents`

New table (chosen over overloading the accepted-only ledger, to keep reconcile untouched):

| column | type | notes |
|--------|------|-------|
| `id` | PK | |
| `account_scope` | Text, not null | `kis_live` |
| `idempotency_key` | Text, not null | derived client_order_id |
| `symbol` | Text | traceability |
| `side` | Text | traceability |
| `created_at` | timestamptz, server default now | |

**UNIQUE(account_scope, idempotency_key).**

`OrderSendIntentService.reserve(*, account_scope, idempotency_key, symbol, side)`:
INSERT; `IntegrityError` → raise `DuplicateOrderIntent`. All writes via the service (no raw
SQL), consistent with existing ledger-service discipline.

Wiring: inside `_execute_and_record`, **for KIS markets only** (`equity_kr`, `equity_us`),
call `reserve(...)` immediately before `_execute_order`. On `DuplicateOrderIntent` →
return an `order_error` (fail-closed, **no send**). The reservation is **not** deleted on
send failure / `OrderSendOutcomeUnknown` → a same-key re-send the same day stays blocked
until reconcile; the next trading day gets a fresh salt → new key → allowed.

Crypto/Upbit is **excluded** from this guard — it relies on the broker-side `identifier`.

### 3.4 Upbit content-based identifier

`place_buy_order` / `place_sell_order` / `place_market_buy_order` / `place_market_sell_order`
gain an optional `identifier: str | None = None` param. When `None`, keep the uuid4 default
(backward-compat for any other caller). `_execute_crypto_order` passes the derived
`client_order_id` (content + KST trading-day salt) so a resent identical crypto order the
same day is rejected by Upbit; the next day's salt allows re-placement.

### 3.5 Ledger additive columns

Add nullable `approval_hash` (`Text`) + `idempotency_key` (`Text`) to:
- `review.kis_live_order_ledger` (model `KISLiveOrderLedger`)
- `review.live_order_ledger` (model `LiveOrderLedger`)

Thread through `_record_kis_live_order`, `_record_live_order`, and the corresponding
ledger-service record methods. Populated at accepted-record time (post-send). Purely
observability/traceability — reconcile does not depend on them.

### 3.6 Migration (single)

One alembic revision, `down_revision = "20260702_rob651"`:
- add 2 columns × 2 ledgers (4 columns)
- create `review.order_send_intents` with the UNIQUE constraint

Additive only. Operator runs `alembic upgrade head` separately (production cutover gate).

### 3.7 Config / env

- `app/core/config.py`: `order_approval_hash_mode: str = "optional"  # off|optional|warn|required`
- `env.example`: `ORDER_APPROVAL_HASH_MODE=optional`
- Ship at **`optional`** (mirror P6-A) — no behavior change for existing callers.

## 4. Acceptance criteria (from issue)

- kis_live/crypto orders cannot be sent (at `required`) or with a mismatched hash.
- Same KIS intent double-send the same day is blocked by the local guard; next-day
  re-placement is allowed.
- Existing reconcile (ROB-395/407) is unchanged — ledger columns are additive only, the
  intent table is never read by reconcile.

## 5. Tests

- **Pure helper** (`test_order_approval.py`): canonical determinism, digest/token round-trip,
  `verify_approval_token` ok/mismatch/expired/invalid, idempotency-key same-day vs next-day.
- **Guard in `_place_order_impl`**: `dry_run=True` emits approval_hash/expires/idempotency_key;
  `dry_run=False` modes (off/optional/warn/required); mismatch returns diff; expired rejected.
- **Intent reserve**: same key → `DuplicateOrderIntent`/blocked; next-day salt → allowed;
  reservation survives simulated send-timeout.
- **Upbit identifier**: content-based identifier passed through; uuid4 default preserved.
- **Ledger pass-through**: approval_hash/idempotency_key persisted on kis_live + live_order rows.
- **reconcile-unchanged**: existing reconcile tests still pass; intent table not queried.

## 6. Docs

- New runbook `docs/runbooks/order-approval-hash.md` (rollout stages, operator cutover).
- CLAUDE.md section for ROB-653 (surface, safety boundary, env gate).

## 7. Out of scope / non-goals

- No changes to reconcile logic or fill-evidence booking.
- No broker-side idempotency for KIS (impossible — that's why the local reserve exists).
- Rollout past `optional` (warn/required) is an operator decision, not this PR.
- kiwoom_mock / alpaca_paper / Toss paths (already have confirm / hash).
