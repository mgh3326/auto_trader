# Validated Run-Card Ingest (ROB-332)

Ingest a `validated_run_card.v1` JSON artifact (produced by
`research/nautilus_scalping`) as an immutable `InvestmentSnapshot`
(`snapshot_kind="validated_run_card"`, `source_kind="manual"`). Audit-only:
no broker/order/watch mutation, no scheduler.

## Dry-run (default — no DB write)

    uv run python -m scripts.ingest_validated_run_card \
      --file path/to/run_card.json --market crypto

Prints the JSON-safe citation headline (`verdict`, `framing`, `trade_count`,
`is_pass_stamp`, `symbols`). `insufficient_data` / `not_validated` is NOT a
pass stamp.

## Commit (operator-gated)

    uv run python -m scripts.ingest_validated_run_card \
      --file path/to/run_card.json --market crypto --commit --confirm

`--commit` requires `--confirm`. Prints the created (or, on re-ingest of an
identical payload, the reused) `snapshot_uuid`. Re-ingesting the same payload
is idempotent (dedup on canonical payload hash).

## Arguments

- `--file` (required): run-card JSON path. The path is never recorded as a
  source URI; only the sanitized payload is persisted.
- `--market` (required): `kr` | `us` | `crypto`. Binance-demo run cards use
  `crypto` (there is no `binance_demo` account scope).
- `--account-scope` (optional): `kis_live` | `kis_mock` | `alpaca_paper` |
  `upbit_live`.
- `--as-of` (optional): ISO-8601; defaults to the run card's `generated_at`.

## Citation in /invest/reports

Once a `validated_run_card` snapshot is a member of a report bundle, a
report item whose symbol matches the run card's `gate_report.symbols` cites it
under `evidence_snapshot["run_card"]` (verdict-first; bootstrap/Monte-Carlo
stats stay nested under `validation`). Linking a run-card snapshot into a
bundle is out of scope for this CLI (operator/Hermes/future work).

## Exit codes

`0` ok · `1` file not found · `2` payload/as-of parse error · `3` ingest
failure · `4` `--commit` without `--confirm`.
