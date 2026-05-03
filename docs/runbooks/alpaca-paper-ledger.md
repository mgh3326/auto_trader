# Alpaca Paper Order Ledger Runbook (ROB-84)

## Purpose

`review.alpaca_paper_order_ledger` is an internal operator/audit lifecycle ledger for Alpaca Paper broker execution attempts.

It records the full lifecycle of a paper order: preview → validation → submit → status/fill → cancel → position snapshot → reconcile.

It was introduced in ROB-84 as a prerequisite for automating the preopen→paper buy/sell roundtrip (ROB-85).

Related: ROB-83 introduced the Alpaca Paper smoke workflow. ROB-84 adds persistent records before automation.

---

## Lifecycle States

| State | Meaning |
|-------|---------|
| `previewed` | Approval preview was built; no order submitted yet |
| `validation_failed` | Confirm-false check failed; order not submitted |
| `submitted` | Order was sent to Alpaca Paper broker |
| `open` | Broker accepted; order is live/pending fill |
| `partially_filled` | Partial fill received |
| `filled` | Order fully filled |
| `canceled` | Order was canceled (broker confirmed) |
| `unexpected` | Broker returned a status we do not recognize (rejected, expired, suspended, unknown) |

### Lifecycle Transitions

```
record_preview()
  └─ lifecycle_state = previewed  OR  validation_failed

record_submit()
  └─ lifecycle_state = _derive_lifecycle_state(order.status, order.filled_qty)
       canceled / filled / partially_filled / open / unexpected

record_status()
  └─ lifecycle_state = _derive_lifecycle_state(order.status, order.filled_qty)
       (updated on each status poll)

record_cancel()
  └─ writes cancel_status + canceled_at; lifecycle_state is set by record_status()

record_position_snapshot()
  └─ writes position_snapshot JSONB; {qty, avg_entry_price, fetched_at}

record_reconcile()
  └─ writes reconcile_status + reconciled_at
```

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
GET /trading/api/alpaca-paper/ledger/recent?limit=50&lifecycle_state=canceled
GET /trading/api/alpaca-paper/ledger/{ledger_id}
GET /trading/api/alpaca-paper/ledger/by-client-order-id/{client_order_id}
```

### MCP tools (read-only, in `ALPACA_PAPER_READONLY_TOOL_NAMES`)

```
alpaca_paper_ledger_list_recent(limit=50, lifecycle_state=None)
alpaca_paper_ledger_get(client_order_id)
```

---

## Operator FAQ

**Q: How do I find all canceled smoke orders?**

```
GET /trading/api/alpaca-paper/ledger/recent?lifecycle_state=canceled&limit=100
# or MCP:
alpaca_paper_ledger_list_recent(lifecycle_state="canceled", limit=100)
```

**Q: How do I look up a specific order?**

```
GET /trading/api/alpaca-paper/ledger/by-client-order-id/{client_order_id}
# or MCP:
alpaca_paper_ledger_get(client_order_id="...")
```

**Q: The lifecycle_state is `unexpected` — what happened?**

Check the `order_status` and `error_summary` fields. Unexpected maps to broker statuses: `rejected`, `expired`, `suspended`, or any unrecognized status.

**Q: Can I write directly to the table with SQL?**

No. All writes must go through `AlpacaPaperLedgerService`. Direct SQL inserts/updates/deletes are not permitted.

---

## Safety Boundaries

- **No broker mutation**: The ledger service does not submit, cancel, or modify orders.
- **No live trading routes**: This ledger is strictly `alpaca_paper`.
- **No direct SQL backfill**: Use `AlpacaPaperLedgerService` methods only.
- **No scheduler changes**: Ledger writes are triggered by execution flow, not by scheduler.
- **No secrets persisted**: All JSONB payloads are run through `_redact_sensitive_keys()` before persistence. Keys matching `api_key`, `secret`, `authorization`, `token`, `account_no`, `account_number`, `account_id`, `email` are replaced with `[REDACTED]`.
- **Signal/execution separation**: `signal_symbol`/`signal_venue` (e.g. Upbit) are stored separately from `execution_symbol`/`execution_venue` (e.g. Alpaca Paper).

---

## Schema Reference

Table: `review.alpaca_paper_order_ledger`

- `client_order_id`: caller-supplied unique correlation key (up to 128 chars)
- `broker`: always `alpaca`
- `account_mode`: always `alpaca_paper`
- `lifecycle_state`: application state (see table above)
- `raw_responses`: JSONB map of sanitized event snapshots, keyed by event type (`preview`, `submit`, `status`, `cancel`, `position`, `reconcile`)

ORM model: `app.models.review.AlpacaPaperOrderLedger`

Service: `app.services.alpaca_paper_ledger_service.AlpacaPaperLedgerService`

Migration: `alembic/versions/c1d2e3f4a5b6_add_alpaca_paper_order_ledger.py`
