# Execution Ledger Runbook (ROB-211)

This ledger records broker-filled executions from read-only broker history. ROB-211 K1 ships inert: reconciliation defaults to dry-run, the commit flag is disabled by default, and no scheduler is activated.

## Safety boundaries

- No broker submit/cancel/modify calls are part of `app/services/execution_ledger/`.
- No production backfill or DB commit is performed by this PR.
- `EXECUTION_LEDGER_COMMIT_ENABLED` defaults to `False`.
- The TaskIQ task is manual/scheduleless only.

## Dry-run smoke

```bash
uv run python -m scripts.reconcile_execution_ledger --broker kis --window-hours 24 --dry-run
uv run python -m scripts.reconcile_execution_ledger --broker upbit --window-hours 24 --dry-run
```

Dry-runs produce a `ReconcileDiff` and roll back the session in the CLI.

## Activation checklist (future ops change, not ROB-211 K1)

1. Collect at least 3 days of clean dry-run evidence for KIS and Upbit.
2. Confirm no `error_summary` in recent reconcile runs and diff sizes are expected.
3. Obtain reviewer/operator approval in the activation log below.
4. Flip `EXECUTION_LEDGER_COMMIT_ENABLED=True` in the target environment only after approval.
5. Run one bounded `--commit` reconciliation and verify row counts/idempotency.
6. Keep recurring scheduler activation paused until at least 7 days of stable commit runs.

## Activation log

No activation approved in ROB-211 K1.
