# Alpaca Paper Roundtrip Audit Report Runbook (ROB-92)

## Purpose

The Alpaca Paper roundtrip audit report is a read-only operator view over existing `review.alpaca_paper_order_ledger` rows. It assembles one buy/sell lifecycle into a structured report with candidate/QA provenance, approval context, buy and sell legs, final position evidence, caller-supplied open-order/position snapshots, anomaly flags, and explicit safety metadata.

This report is an audit/read model only. It does not submit, cancel, modify, repair, backfill, or reconcile orders.

## Safety contract

ROB-92 must remain read-only:

- No broker mutation.
- No broker open-order or position fetch from the API/service assembler.
- No database writes, commits, flushes, inserts, updates, deletes, migrations, or repair actions.
- No secrets or raw credentials in report fields.
- HTTP routes are GET-only and use the existing ledger router authentication dependency.
- MCP tool inputs may include caller-supplied `open_orders` and `positions` snapshots; the tool must not fetch those snapshots itself.

The report includes a `safety` block with:

- `read_only=true`
- `broker_mutation_performed=false`
- `db_write_performed=false`
- `broker_snapshot_fetched=false`

## API endpoints

All endpoints are authenticated GET routes under `/trading`.

Single-report lookups:

```text
GET /trading/api/alpaca-paper/roundtrip-report/by-correlation-id/{lifecycle_correlation_id}
GET /trading/api/alpaca-paper/roundtrip-report/by-client-order-id/{client_order_id}
```

List lookups, grouped by lifecycle correlation ID:

```text
GET /trading/api/alpaca-paper/roundtrip-report/by-candidate-uuid/{candidate_uuid}
GET /trading/api/alpaca-paper/roundtrip-report/by-briefing-artifact-run-uuid/{briefing_artifact_run_uuid}
```

Query parameters:

```text
include_ledger_rows=true|false   # default true
stale_after_minutes=30           # must be >= 1
```

HTTP report endpoints do not fetch broker open orders or positions. When no caller-supplied snapshot path exists, `open_orders.source` is `missing` and `final_position.source` is based on ledger snapshots when available.

## MCP tool

```python
alpaca_paper_roundtrip_report(
    lifecycle_correlation_id=None,
    client_order_id=None,
    candidate_uuid=None,
    briefing_artifact_run_uuid=None,
    open_orders=None,
    positions=None,
    stale_after_minutes=30,
    include_ledger_rows=True,
)
```

Exactly one lookup key is required.

Return wrapper:

```json
{
  "success": true,
  "account_mode": "alpaca_paper",
  "source": "alpaca_paper_roundtrip_report",
  "read_only": true,
  "report": { }
}
```

For `candidate_uuid` and `briefing_artifact_run_uuid`, `report` is a list response with `lookup_key`, `count`, and `items`. For correlation/client-order lookups, `report` is a single report.

## Report status

- `complete`: all canonical ROB-90 lifecycle steps are present.
- `incomplete`: ledger rows exist, but one or more expected lifecycle steps are missing.
- `anomaly`: anomaly lifecycle rows or caller-supplied snapshot checks produced blocking findings.
- `not_found`: no matching ledger rows were found.

HTTP routes convert `not_found` or empty list responses to 404.

## Completeness model

The report compares observed lifecycle states against the canonical ROB-90 roundtrip sequence:

```text
planned
previewed
validated
submitted
filled
position_reconciled
sell_validated
closed
final_reconciled
```

`completeness.required_steps`, `observed_steps`, `missing_steps`, and `is_complete` are included so operators can see what evidence is missing.

## Operator examples

Find a complete report by correlation ID:

```text
GET /trading/api/alpaca-paper/roundtrip-report/by-correlation-id/corr-abc?include_ledger_rows=false
```

Find the full roundtrip when you only have a client order ID:

```text
GET /trading/api/alpaca-paper/roundtrip-report/by-client-order-id/buy-001
```

Find all reports associated with a candidate:

```text
GET /trading/api/alpaca-paper/roundtrip-report/by-candidate-uuid/00000000-0000-4000-8000-000000000000
```

Use MCP with pre-fetched read-only snapshots:

```python
await alpaca_paper_roundtrip_report(
    lifecycle_correlation_id="corr-abc",
    open_orders=[{"id": "order-1", "symbol": "BTCUSD", "status": "new"}],
    positions=[{"symbol": "BTCUSD", "qty": "0"}],
    include_ledger_rows=False,
)
```

## Stop conditions

Stop and investigate rather than executing any repair if:

- `status` is `anomaly`.
- `anomalies.should_block` is true.
- `completeness.is_complete` is false for a roundtrip that was expected to be fully closed.
- `final_position.qty` is non-zero after an expected close.
- The report needs broker-state freshness beyond caller-supplied snapshots; that is outside ROB-92 scope.

## Related docs

- `docs/runbooks/alpaca-paper-ledger.md`
- `docs/post-mortems/2026-05-03-rob87-alpaca-paper-roundtrip.md`
