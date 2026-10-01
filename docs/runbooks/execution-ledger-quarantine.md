# Execution-ledger quarantine of phantom websocket rows (#1175)

## 1. What it is

`review.execution_ledger` rows written by the websocket tap (`source=websocket`,
today fillwire) can be KIS **accept** notices that were recorded as fills: the
H0STCNI0 frame carries `CNTG_YN=1` (order accepted/confirmed), not `2`
(executed). hk #1172 found four such rows (58051, 58052, 58053, 58064,
2026-09-30, KIS KR buys, 1 share each) whose orders the broker shows as expired
unfilled. The fillwire decoder fix is separate (#1172).

Ledger rows are never hand-edited or deleted. The lever is
`scripts/quarantine_execution_ledger_rows.py`, which goes through
`app/services/execution_ledger/quarantine.py` and
`ExecutionLedgerRepository.mark_quarantined`/`append_quarantine_events`:

- the row stays in the table; three columns are set:
  `quarantined_at`, `quarantine_reason`, `quarantined_by`;
- one append-only row per ledger row is written to
  `review.execution_ledger_quarantine_events` (batch id, reason, actor,
  evidence: the row identity plus the frame's TR, CNTG_YN, order number,
  symbol, side code, received_at and the recomputed fillwire fill_seq; never
  the account fields 0/1 of the frame);
- every reader that treats ledger rows as fills ANDs
  `execution_ledger_in_effect()` (`quarantined_at IS NULL`), so the row stops
  counting everywhere (list in section 6).

## 2. Eligibility (every id, or the whole batch is refused)

| check | refusal verdict |
|---|---|
| id exists | `not_found` |
| not already quarantined (mixed batch) | `already_quarantined` |
| `source = websocket` | `not_websocket` |
| `broker = kis` | `not_kis` |
| `account_mode = live` | `not_live` |
| `instrument_type = equity_kr` | `not_equity_kr` |
| `raw_payload_json` is a non-empty object | `raw_payload_missing` |
| `raw_payload_json.tr == "H0STCNI0"` | `raw_payload_not_domestic_execution_notice` |
| `raw_payload_json.fields` is a list of strings with index 13 | `raw_payload_fields_malformed` |
| `fields[13]` (CNTG_YN) is not `2` | `fill_notice_cntg_yn_2` (a real fill) |
| `fields[13]` is exactly `1` (whitespace-trimmed) | `cntg_yn_not_accept` |
| `fields[2]` (order no) equals `broker_order_id` | `raw_order_no_mismatch` |
| `fields[8]` (symbol) equals `symbol` | `raw_symbol_mismatch` |

The frame shape is fillwire's `RawPayload` (`{"tr", "fields", "received_at"}`,
fields in go-kis `kis/ws` order). `raw_fill_seq_matches` (fillwire's
`DeriveFillSeq` recomputed from the stored fields equals the row's `fill_seq`)
is reported in the preview and stored in the audit evidence but does not gate:
a non-UTF-8 byte in an unrelated field could change it after JSON encoding.

If every id is already quarantined the run is a **no-op** (exit 0, nothing
written, the existing reason/actor are shown). Ids are exact decimal tokens:
`--ids 58051,58052` or repeated `--ids`; ranges (`58051-58064`), patterns,
signs, spaces, leading zeros, non-ASCII digits and duplicates are refused
before the database is opened. At most 50 ids per run.

## 3. Operator procedure (not run by builders or testers)

Prerequisite: the migration `20261001_t1175_ledger_quar` is applied
(`alembic upgrade head`, the usual operator migration step). The database is
named explicitly; prefer an environment variable name so the password never
reaches the process arguments (the value is never printed):

```bash
# 1. preview (default): prints every row and its verdict, writes nothing
uv run python -m scripts.quarantine_execution_ledger_rows \
  --database-url-env <NAME_OF_DB_URL_VARIABLE> \
  --ids 58051,58052,58053,58064 \
  --reason "hk 1172: KIS H0STCNI0 accept notice (CNTG_YN=1) recorded as a fill; broker shows the orders expired unfilled" \
  --actor <operator-name>

# 2. commit: same command plus --commit
uv run python -m scripts.quarantine_execution_ledger_rows \
  --database-url-env <NAME_OF_DB_URL_VARIABLE> \
  --ids 58051,58052,58053,58064 \
  --reason "hk 1172: KIS H0STCNI0 accept notice (CNTG_YN=1) recorded as a fill; broker shows the orders expired unfilled" \
  --actor <operator-name> \
  --commit
```

Output is one JSON object on stdout: `mode`, `status`
(`eligible`/`refused`/`noop`/`committed`), `requested_ids`, `refused_ids`,
`rows[]` (`ledger_id`, `verdict`, `eligible`, `detail`), `batch_id`, `changed`.
Exit codes: 0 eligible preview / committed / no-op, 2 refused, 1 input or
database error (stderr carries only the error class, never the URL).

If the preview refuses with `raw_payload_missing`,
`raw_payload_not_domestic_execution_notice` or
`raw_payload_fields_malformed`, the stored frame cannot prove the row is an
accept notice: stop and report; do not look for another way to exclude it.

## 4. After the commit: opening seeds carved while a phantom existed

Lots never count websocket rows, but an opening seed (`SEED-*`,
`scripts/seed_execution_ledger_opening_lots.py`) is carved as
`broker qty - ledger net since cutover`, and that net used to count websocket
rows. A seed carved while a phantom existed is short by the phantom quantity
and the symbol shows `quantity_mismatch_with_reference` +
`provisional_rows_pending_reconcile`. Quarantine does not rewrite a seed row.
If `get_holdings(include_ledger_lots=True)` still shows the mismatch for a
quarantined symbol, run the seed script **preview** for that symbol (the net
now excludes quarantined rows) and commit only if the preview shows exactly the
expected correction. Test:
`test_quarantine_readers_db.py::test_lots_symbol_whose_only_mismatch_was_the_phantom_becomes_known`.

## 5. Database guarantees: a quarantined row is terminal

- `ck_execution_ledger_quarantine_fields`: the three columns are all NULL or
  all set, with non-blank reason and actor.
- `ck_execution_ledger_quarantine_scope`: only `source='websocket' AND
  broker='kis'` rows can be quarantined, whatever issues the UPDATE.
- `trg_execution_ledger_quarantine_guard` (BEFORE UPDATE OR DELETE, per row):
  once `quarantined_at` is set, **any** UPDATE (payload, source, quarantine
  columns, even `updated_at`) and any DELETE of that row raises
  `review.execution_ledger row <id> is quarantined and terminal; <op> rejected`
  (SQLSTATE 23001 restrict_violation). The quarantining UPDATE itself is
  allowed because the row was not yet quarantined.
- `trg_execution_ledger_quarantine_truncate` (BEFORE TRUNCATE): TRUNCATE of
  `review.execution_ledger` is refused while any quarantined row exists.
- `review.execution_ledger_quarantine_events`: UNIQUE `ledger_id`; UPDATE,
  DELETE and TRUNCATE are rejected by triggers; no foreign key.

What that means for writers (nothing in the ingest path was changed):

- Identical replay of the phantom frame (same key, same payload): the
  repository classifies it `unchanged` and writes nothing; the row stays
  quarantined.
- Any changed write on a quarantined key (a changed phantom replay, or a real
  fill or reconciler/manual_import row that collides with the exact key
  broker/account_mode/venue/broker_order_id/fill_seq): the ON CONFLICT UPDATE
  hits the terminal trigger and fails. Nothing is overwritten or hidden; the
  write is refused loudly for operator review:
  - HTTP ingest (fillwire): that item is `rejected` with reason
    `IntegrityError`, no downstream notification, and the API logs
    `Execution ledger ingest rejected one fill: broker=... order_id=...
    fill_seq=...`; other items in the batch are unaffected (per-item savepoint).
  - websocket monitor `commit_fill`: the upsert raises and the monitor logs the
    error for that fill.
  - reconciler (task or script, commit mode): the run fails, its run record's
    `error_summary` carries the terminal message, the caller rolls the run back
    and re-raises. Until resolved, KIS reconcile runs keep failing, the ledger
    goes stale and lots fall back to `unknown` (fail-closed). A real collision
    needs all five key fields equal; fillwire derives fill_seq from a digest of
    the whole frame, so in practice it is a deliberate or corrupted write.
  - Operator response: read the refused key from the log or run record, compare
    it with the quarantined row and its audit event, and decide out of band
    (hk task). There is no in-app override.
- Any later data migration that UPDATEs or DELETEs execution_ledger rows must
  exclude quarantined rows or it will fail; that is intended.
- Test databases only: `tests/services/execution_ledger/_quarantine_fixtures.py`
  `purge_test_ledger_rows` cleans up with `SET LOCAL session_replication_role =
  replica` (superuser-only, one transaction). The application role cannot do
  this.
- Downgrade drops the columns, triggers and the audit table, i.e. it
  un-quarantines everything. Do not downgrade after a commit without
  re-planning.
- Locking: ADD COLUMN (nullable, no default) is catalog-only, but the two
  ADD CONSTRAINT CHECKs scan `review.execution_ledger` while the migration
  transaction holds ACCESS EXCLUSIVE on it, blocking ledger writes (fill
  ingest, reconcile) for the scan. Apply outside market hours like other
  ledger DDL.

Live migration proof (2026-10-01, round 4, throwaway TimescaleDB 2.22.1-pg17
container, fresh `alembic upgrade` from base with fake placeholder settings):
upgrade to head kept an existing reconciler row in effect and still updatable;
after quarantining a websocket row, DELETE, payload UPDATE, an ON CONFLICT
collision from a reconciler-sourced insert and TRUNCATE were each refused by
the terminal trigger; `downgrade -1` (with a quarantined row present) produced
a `pg_dump -s` identical to the pre-upgrade dump and kept every row;
up/down/up dumps identical; the audit table and the quarantine columns/CHECKs
match the ORM `create_all` copy. The CLI was run end to end against it
(preview, refused mixed batch with a `CNTG_YN=2` row, commit, repeat no-op,
then a DELETE of a committed row was refused).

History: rounds 2-3 tried a delete-plus-replay tombstone instead of a terminal
row; round-3 testing broke it (whitespace parity, same-key real fill hidden,
classify/upsert race). Round 4 (operator decision hk 1224 = A) removed it.

## 6. Readers

Filtered with `execution_ledger_in_effect()` (each has a DB test that also runs
a "filter dropped" mutant):

- `app/services/execution_ledger/kis_lots.py` `load_kis_live_kr_lot_blocks`
  (lots, #963 open_buy_evidence, same_day_sell_evidence, #1087
  open_sell_evidence, same_day_buy_evidence, sellable_by_ledger)
- `app/services/order_proposals/kis_leftover_inference_service.py`
  `_symbol_fills` (#1112 inference no-fill and holding-unchanged facts)
- `app/services/execution_ledger/repository.py` `has_fill_for_order`,
  `list_recent_fills_for_triage`, `net_quantity_by_match_key_since`,
  `position_before_fill`
- `app/services/execution_ledger/query_service.py` `list_recent`,
  `list_by_symbol`, `list_fills_today`, `list_sell_history` (both queries)
- `app/services/fill_event_handoff/broker_risk.py` `list_fills_for_order`
- `app/services/protected_quantity_service.py` `_net_execution_quantity_since`
- `app/services/market_close_digest/queries.py` `_execution_fills`
- `app/services/quotes_consumer/repository.py` `fills_after`

Deliberately unfiltered (declared in `test_quarantine_mutants.py`):
`repository.get_by_key`/`upsert_fill` (upsert identity and writer),
`repository.rows_by_ids`/`mark_quarantined` (the quarantine tool),
`repository.apply_market_filter` (composes onto a filtered statement),
`repository.max_ledger_id` and `quotes_consumer.fills_watermark` (id
watermarks), `kis_mock_lifecycle_service._has_local_fill_row` (mock only; the
tool refuses mock rows and counting only makes a cancel refuse),
`protected_position_auto_follow._follow_ledger_row` (reconciler rows only).
`test_every_ledger_reader_is_filtered_or_declared_exempt` fails when a new
function reading `ExecutionLedger` is neither.
