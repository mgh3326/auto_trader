# Execution Ledger Backfill Runbook

## Review Hold

Do not run commit mode until ROB-478~481 are reviewed under `high_risk_change` and `needs_stronger_model_review`.

## Phase 1: KIS Dry Run

```bash
uv run python -m scripts.reconcile_execution_ledger \
  --broker kis \
  --start-date 2026-02-01 \
  --end-date 2026-06-10 \
  --max-pages 100 \
  --dry-run
```

Archive JSON output with `would_insert`, `would_update`, `unchanged`, and sample rows.

## Phase 2: Upbit Dry Run

```bash
uv run python -m scripts.reconcile_execution_ledger \
  --broker upbit \
  --start-date 2026-02-01 \
  --end-date 2026-06-10 \
  --dry-run
```

Archive JSON output and confirm no truncation error.

## Phase 3: Coverage SQL

Run the ROB-478 coverable SQL before and after dry-run planning against the target DB.

## Phase 4: KIS/Upbit Commit

Only after reviewer approval:

```bash
EXECUTION_LEDGER_COMMIT_ENABLED=true uv run python -m scripts.reconcile_execution_ledger \
  --broker kis \
  --start-date 2026-02-01 \
  --end-date 2026-06-10 \
  --max-pages 100 \
  --commit

EXECUTION_LEDGER_COMMIT_ENABLED=true uv run python -m scripts.reconcile_execution_ledger \
  --broker upbit \
  --start-date 2026-02-01 \
  --end-date 2026-06-10 \
  --commit
```

## Phase 5: Opening Lot Seed Dry Run

Run only after Phase 4 commits:

```bash
uv run python -m scripts.seed_execution_ledger_opening_lots \
  --cutover 2026-05-10 \
  --dry-run
```

Archive skipped rows. Modified Upbit average prices and ambiguous/non-positive prices must remain skipped.

## Phase 6: Opening Lot Seed Commit

Only after reviewer approval:

```bash
EXECUTION_LEDGER_COMMIT_ENABLED=true uv run python -m scripts.seed_execution_ledger_opening_lots \
  --cutover 2026-05-10 \
  --commit
```

## Phase 7: UI Verification

Open `/invest/my?tab=sellHistory` and confirm matched rows show 판매수익/수익률 and currency summary cards render.
