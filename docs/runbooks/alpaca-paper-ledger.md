# Alpaca Paper Order Ledger Runbook (ROB-84/ROB-90)

## Purpose

`review.alpaca_paper_order_ledger` is an internal operator/audit lifecycle ledger for Alpaca Paper broker execution attempts.

It records the full lifecycle of a paper roundtrip: plan → preview → validation → submit → fill → position → sell → close → final reconcile.

It was introduced in ROB-84 as a prerequisite for automating the preopen→paper buy/sell roundtrip (ROB-85). ROB-90 normalized the taxonomy to canonical states.

Related: ROB-83 introduced the Alpaca Paper smoke workflow. ROB-84 adds persistent records. ROB-90 normalizes lifecycle states and adds roundtrip correlation.

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
       submitted / filled / anomaly
       record_kind = execution, confirm_flag = true

record_status()
  └─ lifecycle_state = _derive_lifecycle_state(order.status, order.filled_qty)
       (updated on each status poll)

record_cancel()
  └─ writes cancel_status + canceled_at; lifecycle_state is set by record_status()

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
```

### MCP tools (read-only, in `ALPACA_PAPER_READONLY_TOOL_NAMES`)

```
alpaca_paper_ledger_list_recent(limit=50, lifecycle_state=None)
alpaca_paper_ledger_get(client_order_id)
alpaca_paper_ledger_get_by_correlation(lifecycle_correlation_id)
```

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
