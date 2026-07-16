# Alpaca Paper Order Ledger Runbook (ROB-84/ROB-90)

## Purpose

`review.alpaca_paper_order_ledger` is an internal operator/audit lifecycle ledger for Alpaca Paper broker execution attempts.

It records the full lifecycle of a paper roundtrip: plan → preview → validation → submit → fill → position → sell → close → final reconcile.

It was introduced in ROB-84 as a prerequisite for automating the preopen→paper buy/sell roundtrip (ROB-85). ROB-90 normalized the taxonomy to canonical states.

Related: ROB-83 introduced the Alpaca Paper smoke workflow. ROB-84 adds persistent records. ROB-90 normalizes lifecycle states and adds roundtrip correlation. ROB-92 adds a read-only structured roundtrip audit report; see `docs/runbooks/alpaca-paper-roundtrip-report.md`.

---

## Lifecycle States (ROB-90 Canonical)

| State | Meaning |
|-------|---------|
| `planned` | Order intent recorded; no preview or submit yet |
| `previewed` | Approval preview was built; no order submitted yet |
| `validated` | Confirm-false check passed; order not yet submitted |
| `submitted` | Order was sent to Alpaca Paper broker; awaiting fill (includes partially_filled broker status) |
| `filled` | Order fully filled |
| `position_reconciled` | Post-fill position snapshot recorded |
| `sell_validated` | Sell-leg confirm-false check passed |
| `closed` | Sell order executed; position closed |
| `final_reconciled` | Roundtrip fully reconciled |
| `canceled` | Order successfully canceled at the broker with cancel request evidence (ROB-920) |
| `anomaly` | Broker returned a non-recoverable status (rejected, expired, suspended, canceled, unknown) or a state mismatch |

### Lifecycle Transitions

```
record_plan()
  └─ lifecycle_state = planned, record_kind = plan

record_preview()
  └─ lifecycle_state = previewed, record_kind = preview

record_validation_attempt(validation_outcome='passed')
  └─ lifecycle_state = validated, record_kind = validation_attempt, confirm_flag = false

record_validation_attempt(validation_outcome='failed')
  └─ lifecycle_state = anomaly, record_kind = validation_attempt, confirm_flag = false

record_submit()
  └─ lifecycle_state = _derive_lifecycle_state(order.status, order.filled_qty)
       submitted / filled / anomaly / canceled
       record_kind = execution, confirm_flag = true

record_status()
  └─ lifecycle_state = _derive_lifecycle_state(order.status, order.filled_qty)
       (updated on each status poll)

record_cancel()
  └─ writes cancel_status + canceled_at; updates lifecycle_state to canceled if order_status is already canceled (ROB-920)

record_position_snapshot()
  └─ writes position_snapshot JSONB + lifecycle_state = position_reconciled

record_sell_validation()
  └─ lifecycle_state = sell_validated, record_kind = validation_attempt, confirm_flag = false

record_close()
  └─ lifecycle_state = closed

record_reconcile()
  └─ writes reconcile_status + reconciled_at (no lifecycle_state advance)

record_final_reconcile()
  └─ lifecycle_state = final_reconciled, record_kind = reconcile, settlement_status = n_a
```

---

## Legacy State Mapping (ROB-90 Migration)

The migration `d4e5f6a7b8c9` (down_revision: `c1d2e3f4a5b6`) mapped old states as follows:

| Old state | Canonical state | record_kind | Notes |
|-----------|----------------|-------------|-------|
| `previewed` (no broker order) | `previewed` | `preview` | Preview-only row |
| `previewed` (with broker order) | `previewed` | `execution` | Execution row |
| `validation_failed` | `anomaly` | `validation_attempt` | `validation_outcome='failed'`, `confirm_flag=false` |
| `submitted` | `submitted` | `execution` | `confirm_flag=true` |
| `open` | `submitted` | `execution` | `confirm_flag=true` |
| `partially_filled` | `submitted` | `execution` | Broker status preserved in `order_status` |
| `filled` | `filled` | `execution` | `confirm_flag=true` |
| `canceled` | `anomaly` | `execution` | `confirm_flag=true` |
| `unexpected` | `anomaly` | `execution` | — |

> [!NOTE]
> **ROB-920 Evidence-Based Cancellation (Asymmetry Rule):**
> From 2026-07-17 (ROB-920), order_status `'canceled'` is mapped to the terminal `canceled` lifecycle state **only** if there is cancel request evidence (e.g. `cancel_status` and/or `canceled_at` is set on the row). Unexpected broker-side cancellations without evidence remain mapped to `anomaly`.
> *2026-07-17 이전 행은 구 매핑* (Past anomaly-canceled rows before 2026-07-17 remain mapped to `anomaly` - no backfill).

---

## record_kind Values

| record_kind | Meaning |
|------------|---------|
| `plan` | Intent-only row, no preview/validation done |
| `preview` | Approval preview built (confirm=false dry run) |
| `validation_attempt` | Confirm-false API validation attempt |
| `execution` | Actual broker order (confirm=true) |
| `reconcile` | Post-execution reconciliation record |
| `anomaly` | Unexpected/error row |

---

## Example: Buy/Sell Roundtrip Records

Rows for a complete paper roundtrip sharing `lifecycle_correlation_id = "corr-abc"`:

| client_order_id | side | record_kind | lifecycle_state | confirm_flag | validation_attempt_no |
|----------------|------|------------|----------------|-------------|----------------------|
| buy-001 | buy | plan | planned | null | null |
| buy-001 | buy | preview | previewed | null | null |
| buy-001 | buy | validation_attempt | validated | false | 1 |
| buy-001 | buy | execution | submitted | true | null |
| buy-001 | buy | execution | filled | true | null |
| buy-001 | buy | execution | position_reconciled | true | null |
| sell-001 | sell | validation_attempt | sell_validated | false | 1 |
| sell-001 | sell | execution | closed | true | null |
| sell-001 | sell | reconcile | final_reconciled | true | null |

All rows: `lifecycle_correlation_id = "corr-abc"`, `broker = "alpaca"`, `account_mode = "alpaca_paper"`.

---

## Provenance Fields

The `ApprovalProvenance` dataclass maps approval pipeline data into the ledger row:

| Ledger column | Source |
|---------------|--------|
| `candidate_uuid` | `PreopenPaperApprovalCandidate.candidate_uuid` |
| `signal_symbol` | `PreopenPaperApprovalCandidate.signal_symbol` (Upbit symbol e.g. `KRW-BTC`) |
| `signal_venue` | `PreopenPaperApprovalCandidate.signal_venue` (e.g. `upbit`) |
| `execution_symbol` | `execution_symbol` in the candidate (e.g. `BTCUSD`) |
| `execution_venue` | `execution_venue` (e.g. `alpaca_paper`) |
| `execution_asset_class` | `crypto` or `us_equity` |
| `workflow_stage` | `preopen`, `crypto_weekend`, etc. |
| `purpose` | `paper_plumbing_smoke`, etc. |
| `briefing_artifact_run_uuid` | `PreopenBriefingArtifact.run_uuid` (nullable) |
| `briefing_artifact_status` | `PreopenBriefingArtifact.status` |
| `qa_evaluator_status` | QA evaluator status string |
| `approval_bridge_generated_at` | `PreopenPaperApprovalBridge.generated_at` |
| `approval_bridge_status` | `PreopenPaperApprovalBridge.status` |

Use `from_approval_bridge(bridge, candidate, briefing_artifact=..., qa_evaluator_status=...)` to build provenance.

---

## Read Paths

### FastAPI endpoints (authenticated, GET only)

```
GET /trading/api/alpaca-paper/ledger/recent?limit=50&lifecycle_state=anomaly
GET /trading/api/alpaca-paper/ledger/{ledger_id}
GET /trading/api/alpaca-paper/ledger/by-client-order-id/{client_order_id}
GET /trading/api/alpaca-paper/ledger/by-correlation-id/{lifecycle_correlation_id}
GET /trading/api/alpaca-paper/roundtrip-report/by-correlation-id/{lifecycle_correlation_id}
GET /trading/api/alpaca-paper/roundtrip-report/by-client-order-id/{client_order_id}
GET /trading/api/alpaca-paper/roundtrip-report/by-candidate-uuid/{candidate_uuid}
GET /trading/api/alpaca-paper/roundtrip-report/by-briefing-artifact-run-uuid/{briefing_artifact_run_uuid}
```

### MCP tools (read-only, in `ALPACA_PAPER_READONLY_TOOL_NAMES`)

```
alpaca_paper_ledger_list_recent(limit=50, lifecycle_state=None)
alpaca_paper_ledger_get(client_order_id)
alpaca_paper_ledger_get_by_correlation(lifecycle_correlation_id)
alpaca_paper_roundtrip_report(lifecycle_correlation_id=None, client_order_id=None, candidate_uuid=None, briefing_artifact_run_uuid=None, open_orders=None, positions=None)
```

---

## Profile exposure (ROB-908 — DEFAULT-profile flag)

The Alpaca paper surface historically registered only under the `us-paper`
(`McpProfile.US_PAPER`) server. The mock_alpaca operator session runs on the
single DEFAULT profile (8765 haproxy → 8766), so those tools were invisible to
it. ROB-908 adds a flag-gated DEFAULT exposure, mirroring the ROB-601/ROB-867
kiwoom-mock pattern:

- Set `ALPACA_PAPER_DEFAULT_TOOLS_ENABLED=true`
  (`settings.alpaca_paper_default_tools_enabled`, default `False`) to surface the
  Alpaca paper surface in the DEFAULT profile: the read tools
  (`alpaca_paper_get_account` / `_get_cash` / `_list_positions` / `_list_orders`
  / `_get_order` / `_list_assets` / `_list_fills`), the pure-validator
  `alpaca_paper_preview_order`, the read-only `us_dual_paper_*` trio, the
  confirm-gated `alpaca_paper_submit_order` / `alpaca_paper_cancel_order`, and
  the ledger reads (`alpaca_paper_ledger_list_recent` / `_ledger_get` /
  `_ledger_get_by_correlation` / `_roundtrip_report` /
  `_execution_preflight_check`).
- With the flag off (default) these tools are **physically absent** from the
  DEFAULT profile — a defense-in-depth complement to the per-call `confirm=True`
  + server-issued `quote_snapshot_id` order gates, which are unchanged.
- `alpaca_paper_automated_submit_order` (and its automated preview) are
  **never** exposed in DEFAULT even with the flag on — they stay `us-paper`-only
  (ROB-842 governance deny-list).

---

## Operator FAQ

**Q: How do I find all anomaly (formerly canceled/unexpected) orders?**

```
GET /trading/api/alpaca-paper/ledger/recent?lifecycle_state=anomaly&limit=100
# or MCP:
alpaca_paper_ledger_list_recent(lifecycle_state="anomaly", limit=100)
```

**Q: How do I look up a specific order?**

```
GET /trading/api/alpaca-paper/ledger/by-client-order-id/{client_order_id}
# or MCP:
alpaca_paper_ledger_get(client_order_id="...")
```

**Q: How do I view a complete buy/sell roundtrip?**

```
GET /trading/api/alpaca-paper/ledger/by-correlation-id/{lifecycle_correlation_id}
# or MCP:
alpaca_paper_ledger_get_by_correlation(lifecycle_correlation_id="...")
```

**Q: The lifecycle_state is `anomaly` — what happened?**

Check the `order_status`, `error_summary`, `record_kind`, and `validation_outcome` fields.
Anomaly maps to: `canceled`, `rejected`, `expired`, `suspended`, any unrecognized status, or a
validation_attempt with `validation_outcome='failed'`.

**Q: What does `submitted` lifecycle_state mean if order_status is `partially_filled`?**

ROB-90 maps `partially_filled` broker status to canonical `submitted` lifecycle_state.
The raw broker status is preserved in `order_status`. When fully filled, `record_status()`
will advance the lifecycle_state to `filled`.

**Q: Can I write directly to the table with SQL?**

No. All writes must go through `AlpacaPaperLedgerService`. Direct SQL inserts/updates/deletes are not permitted.

---

## Safety Non-Actions

The following are explicitly out of scope and must not be added to this ledger or its service:

- **No broker mutation**: The ledger service does not submit, cancel, or modify orders.
- **No live trading routes**: This ledger is strictly `alpaca_paper`.
- **No KIS/Upbit mutation**: This ledger has no relation to KIS or Upbit order paths.
- **No direct SQL backfill**: Use `AlpacaPaperLedgerService` methods only; migration mechanics are the only approved bulk-write path.
- **No scheduler changes**: Ledger writes are triggered by execution flow, not scheduler.
- **No bulk close/cancel/liquidate**: Out of scope.
- **No secrets persisted**: All JSONB payloads are run through `_redact_sensitive_keys()` before persistence. Keys matching `api_key`, `secret`, `authorization`, `token`, `account_no`, `account_number`, `account_id`, `email` are replaced with `[REDACTED]`.
- **No generic broker routes**: Do not widen `place_order` / `cancel_order` / `modify_order` paths.
- **Signal/execution separation**: `signal_symbol`/`signal_venue` (e.g. Upbit) are stored separately from `execution_symbol`/`execution_venue` (e.g. Alpaca Paper).

---

---

## ROB-91 Paper Approval Packet Contract

`app/services/paper_approval_packet.py` introduces a bounded, frozen approval packet and three deterministic verifiers that run immediately before a confirmed broker submit.

### Purpose

Before calling `alpaca_paper_submit_order(confirm=True)`, producers build a `PaperApprovalPacket` and callers run:

1. `verify_packet_freshness(packet, now=datetime.now(UTC))` — rejects expired packets.
2. `await verify_packet_idempotency(packet, ledger=svc)` — rejects duplicate `client_order_id` that already executed.
3. `await verify_sell_packet_source(packet, ledger=svc)` — for sell packets, validates exactly one reconciled buy source.

### PaperApprovalPacket Fields

| Field | Type | Description |
|-------|------|-------------|
| `signal_source` | `str` | Source artifact identifier |
| `artifact_id` | `UUID` | Unique artifact ID |
| `signal_symbol` | `str` | Signal symbol (e.g. `KRW-BTC`) |
| `signal_venue` | `"upbit"` | Signal origin venue |
| `execution_symbol` | `str` | Alpaca Paper symbol (e.g. `BTC/USD`) |
| `execution_venue` | `"alpaca_paper"` | Always `alpaca_paper` |
| `execution_asset_class` | `"crypto" \| "us_equity"` | Asset class |
| `side` | `"buy" \| "sell"` | Order side |
| `max_notional` | `Decimal \| None` | Exclusive with `max_qty`; must be > 0 |
| `max_qty` | `Decimal \| None` | Exclusive with `max_notional`; must be > 0 |
| `qty_source` | `str` | How qty was derived; sell packets restricted to ledger/reconcile values |
| `expected_lifecycle_step` | `str` | Pre-submit step: `planned/previewed/validated/submitted` |
| `lifecycle_correlation_id` | `str` | Roundtrip correlation key |
| `client_order_id` | `str` | Per-leg caller-supplied key |
| `expires_at` | `datetime` | Timezone-aware expiry; freshness check rejects if `now >= expires_at` |

**Schema constraints:** `extra="forbid"`, `frozen=True`, exactly one of `max_notional`/`max_qty` positive, timezone-aware `expires_at`, `expected_lifecycle_step` in `{planned, previewed, validated, submitted}`. For Upbit crypto packets, `signal_symbol → execution_symbol` is validated via `map_upbit_to_alpaca_paper`.

### Verifier Table

| Verifier | Trigger condition | Error code |
|----------|------------------|-----------|
| `verify_packet_freshness` | `now >= expires_at` | `stale_packet` |
| `verify_packet_freshness` | `now` has no tzinfo | `naive_now` |
| `verify_packet_idempotency` | `client_order_id` already in executed state | `duplicate_client_order_id` |
| `verify_sell_packet_source` | sell `qty_source` not in ledger/reconcile values | `invalid_qty_source` |
| `verify_sell_packet_source` | no buy execution row in correlation scope | `missing_source_order` |
| `verify_sell_packet_source` | >1 buy execution row in correlation scope | `multiple_source_orders` |
| `verify_sell_packet_source` | buy source not in reconciled state | `source_not_reconciled` |
| `verify_sell_packet_source` | execution symbol mismatch | `wrong_symbol` |
| `verify_sell_packet_source` | `max_qty > source filled_qty` | `qty_exceeds_source` |

### Allowed `qty_source` values for sell packets

Only ledger/reconcile-derived sources are accepted. Manual sources are rejected.

- `ledger_filled_qty`
- `ledger_position_snapshot`
- `reconcile_filled_qty`
- `reconcile_position_snapshot`

### Verifier purity guarantees

- No broker calls, no DB writes, no `datetime.now()` inside verifiers.
- `find_executed_by_client_order_id` and `list_by_correlation_id` are read-only SELECT helpers.
- Callers supply wall-clock (`now=`) for deterministic testing.

### New ledger read helpers (ROB-91)

`EXECUTED_LIFECYCLE_STATES` — frozenset of post-submit states used by the idempotency verifier:
`{submitted, filled, position_reconciled, sell_validated, closed, final_reconciled}`.
Excludes pre-submit (`planned/previewed/validated`) and `anomaly`.

`find_executed_by_client_order_id(client_order_id)` — returns the execution row if `record_kind='execution'` and `lifecycle_state` is in `EXECUTED_LIFECYCLE_STATES`; else `None`.

`list_by_correlation_id(lifecycle_correlation_id)` — returns all rows sharing the correlation ID, ordered oldest-first.

## ROB-842 Submit Boundary (packet + atomic claim + replay)

Every real Alpaca Paper broker POST — manual (`alpaca_paper_submit_order`) and
automated (`alpaca_paper_automated_submit_order`) — is routed through
`AlpacaPaperSubmitCoordinator`, which uses this ledger as the single idempotency +
outcome store. No new table/column/migration was added.

- **Atomic claim.** `claim_submit(...)` inserts the single `record_kind='execution'`
  row for a `client_order_id` (`lifecycle_state='submitted'`, `submitted_at`/
  `broker_order_id` NULL) via `INSERT ... ON CONFLICT DO NOTHING RETURNING`. The
  winner (the caller that inserted the row) is the only one that POSTs.
- **In-flight marker.** An execution row with both `submitted_at` and
  `broker_order_id` NULL is a claimed-but-not-yet-recorded submit (`is_inflight_execution`).
- **Success / terminal failure.** After a broker response, `record_submit(...)`
  fills the same row (success). A deterministic broker rejection (HTTP 4xx/422) is
  booked terminal by `record_submit_failure(...)` (`lifecycle_state='anomaly'`,
  `submitted_at` stamped, redacted `error_summary`). Both make the row non-in-flight.
- **Replay.** `get_execution_by_client_order_id(...)` returns the execution row in
  ANY state; the coordinator replays a completed submit or a terminal failure
  before applying any time-dependent (freshness/position) check, so an expired
  packet never turns a completed order into `stale_packet`.
- **Sell reservation (no oversell).** `reserve_sell_and_claim(...)` holds a
  transaction-scoped advisory lock on `(account_mode, execution_symbol)`. Under
  that same lock, broker order statuses and the current position are read, and
  `available = min(qty_available, live qty − Σ(open sell requested_qty))` is used
  before inserting the claim. The claim stores its `qty` / `qty_available` baseline
  in the existing `position_snapshot` JSONB. Immediate and crash-recovered sell
  responses remain `submitted` for open/partial, `filled`, and unknown/unparseable
  statuses. A `filled` sell is released only after current position evidence proves
  the fill is reflected; missing baseline or stale cross-endpoint evidence fails
  closed with `position_reconciliation_pending`, while an unknown status retains its
  reservation. Cancel read-back persists known open/partial/filled broker truth but
  retains the same `submitted` hold. None of these ambiguous paths sends a new POST.
- **Preview binding.** `record_preview(...)` persists the server-owned approval
  packet + provenance (quote snapshot id, content/packet hashes) as the
  `record_kind='preview'` row; `get_preview_by_client_order_id(...)` re-reads it so
  a duplicate-token preview answers from the persisted expiry/hash.

Market evidence for both paths is loaded from a trusted `market_quote_snapshots`
row (`app.services.alpaca_paper_market_evidence.load_market_evidence`); the caller
never supplies correlation/snapshot/market-data/ceiling.

---

## Schema Reference

Table: `review.alpaca_paper_order_ledger`

### Core identity
- `client_order_id`: per-leg caller-supplied correlation key
- `lifecycle_correlation_id`: cross-leg roundtrip key (buy + sell share this value)
- `broker`: always `alpaca`
- `account_mode`: always `alpaca_paper`
- `lifecycle_state`: canonical ROB-90 state (see table above)
- `record_kind`: row type — `plan`, `preview`, `validation_attempt`, `execution`, `reconcile`, `anomaly`

### ROB-90 taxonomy fields
- `leg_role`: `buy`, `sell`, `roundtrip` (optional, separate from broker `side`)
- `validation_attempt_no`: monotonic per `(lifecycle_correlation_id, side)` for validation rows
- `validation_outcome`: `passed`, `failed`, `skipped`, `n_a`
- `confirm_flag`: `false` for validation-only rows, `true` for executed rows, `null` for plans/previews
- `fee_amount` / `fee_currency`: fee labels (Alpaca Paper may return 0)
- `settlement_status`: `pending`, `settled`, `failed`, `n_a`
- `settlement_at`: settlement timestamp
- `qty_delta`: signed quantity effect (buy positive, sell negative, preview null)

### Raw events
- `raw_responses`: JSONB map of sanitized event snapshots, keyed by event type (`preview`, `submit`, `status`, `cancel`, `position`, `reconcile`, `final_reconcile`)

### Indexes
- `ix_alpaca_paper_ledger_correlation_id` on `lifecycle_correlation_id` (roundtrip lookups)
- `ix_alpaca_paper_ledger_record_kind` on `record_kind`
- `uq_alpaca_paper_ledger_client_order_kind` — partial unique on `(client_order_id, record_kind) WHERE validation_attempt_no IS NULL`
- `uq_alpaca_paper_ledger_validation_attempt` — partial unique on `(lifecycle_correlation_id, side, validation_attempt_no) WHERE record_kind = 'validation_attempt'`

ORM model: `app.models.review.AlpacaPaperOrderLedger`

Service: `app.services.alpaca_paper_ledger_service.AlpacaPaperLedgerService`

Migrations:
- `alembic/versions/c1d2e3f4a5b6_add_alpaca_paper_order_ledger.py` (ROB-84 initial)
- `alembic/versions/d4e5f6a7b8c9_normalize_alpaca_paper_ledger_taxonomy.py` (ROB-90 taxonomy normalization)
