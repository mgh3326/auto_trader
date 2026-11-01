# dots history export (ROB-1438)

dots is an external research and design AI session that reads Google Drive and
Linear. This runbook gives dots real order and fill history without account
numbers, broker order ids, broker account identifiers or credentials. The desk
runs the export by hand and uploads the result. Nothing schedules it.

The tool is `scripts/export_dots_history.py`, backed by
`app/services/dots_history_export/`. It reads in one read-only, repeatable-read
transaction, calls no broker and writes only to its output directory.

## Run the export

Do these steps as the desk, on a host that can reach the database.

1. Create the salt once. Keep it outside the repository and reuse it, because
   a new salt breaks joins with every earlier export.

   ```bash
   mkdir -p ~/.config/auto_trader
   ( umask 077; openssl rand -hex 32 > ~/.config/auto_trader/dots_export_salt )
   ```

   The tool refuses a salt file inside the repository, a file that other users
   can read, and a salt shorter than 32 bytes.

2. Put the database URL in an environment variable. Use a role with `SELECT`
   only, if one is available. The tool sets its transaction to read-only either
   way. Do not pass the URL on the command line.

3. Optional: list values that must never appear, one per line, in a file outside
   the repository. Account numbers are the usual entries. The tool refuses a file
   with a value shorter than six characters, because the check could match it only
   against a whole cell.

4. Run the export. The default window is the last six months, and `--until` is
   exclusive.

   ```bash
   mkdir -p ~/dots-exports
   uv run python -m scripts.export_dots_history \
     --database-url-env DOTS_EXPORT_DATABASE_URL \
     --salt-file ~/.config/auto_trader/dots_export_salt \
     --out ~/dots-exports/$(date -u +%F)
   ```

   `--out` must be new or empty, must be outside the repository, and its parent
   must exist. If you made the file in step 3, add `--deny-values-file <path>`.
   Where git cannot name the commit, such as in a container, also pass
   `--code-commit <sha>`.

5. Read the printed row counts. Open `manifest.json` and confirm the window and
   `code_commit`. `code_dirty: true` means the checkout had local changes, so
   rerun from a clean checkout.

6. Upload the whole directory to Drive under `auto_trader/dots/<date>/`. Upload
   `manifest.json` with the data files. Keep the folder shared with the dots
   session and the desk only.

7. Delete the local copy once dots has read it, if the host is shared.

### Exit codes

| Code | Meaning | What to do |
| --- | --- | --- |
| 0 | Exported and published. | Continue with step 5. |
| 1 | Input or database error. The message never contains the URL or the salt. | Fix the input and rerun. |
| 2 | The leak check failed. Nothing was published. | See below. |

### When the leak check fails

The tool prints each violation's table, column, row number and rule, and never
the value. A run either publishes everything or nothing.

Do not edit the output, loosen a pattern or remove a column to get past a
failure. Look at that ledger row. A real identifier in a label column is a data
problem worth reporting to the operator. A false positive needs a reviewed code
change with a test.

## What the export contains

| File stem | Source | Window column |
| --- | --- | --- |
| `kis_live_order_ledger` | KIS live KR orders | `created_at` |
| `live_order_ledger` | KIS live US and Upbit live orders | `created_at` |
| `toss_live_order_ledger` | Toss live orders, including modify and cancel rows | `created_at` |
| `kis_mock_order_ledger` | KIS mock orders | `created_at` |
| `alpaca_paper_order_ledger` | Alpaca paper ledger records | `created_at` |
| `paper_pending_orders` | DB paper resting limit orders | `placed_at` |
| `paper_trades` | DB paper executed trades | `executed_at` |
| `execution_ledger` | Fills that are in effect | `filled_at` |
| `order_proposals` | Order proposals | `created_at` |
| `order_proposal_rungs` | Proposal rungs | the parent proposal's `created_at` |

Each stem has a `.csv` and a `.parquet` file with the same rows. All seven order
tables share one column list. `manifest.json` lists every table's columns, row
count, file hashes and sizes, the window, the commit and the leak-check rules.
Parquet decimals are `decimal128(38, 12)`, and CSV keeps the database scale.

### How to join

| Key | Joins | Notes |
| --- | --- | --- |
| `order_ref` | Order tables, `execution_ledger`, `order_proposal_rungs` | Same broker order. Leading zeros do not matter. |
| `order_ref` + `trading_day` | KIS orders and KIS fills | Required for KIS, because KIS reuses order numbers across days. |
| `correlation_ref` | Order tables, fills, proposals, rungs | Empty on older rows. |
| `proposal_ref` | `order_proposals`, `order_proposal_rungs` | |
| `target_order_ref` | `order_proposals` to the order it replaces or cancels | |
| `order_ref` | `paper_pending_orders`, `paper_trades` | The trade a resting order became. |

`trading_day` is filled for KIS only. Other brokers' ids are not reused, so
they need no day. Rungs have no `trading_day`: a KIS rung's `order_ref` can match
orders on several days, so choose the one nearest the rung's `updated_at`.

Hashes are HMAC-SHA256 with the desk's salt, so they stay stable across exports
that use the same salt. `salt_fingerprint` in the manifest shows whether two
exports can be joined.

### `terminal_outcome`

| Value | Meaning |
| --- | --- |
| `filled` | The ledger status says filled. |
| `partial` | The status says partial, the KIS mock attributed quantity is below the order quantity, or the order was cancelled, expired or replaced with `filled_qty > 0`. |
| `expired` | Expired with no recorded fill. |
| `cancelled` | Cancelled with no recorded fill. |
| `rejected` | Refused, including a refused modify or cancel row. |
| `replaced` | A Toss order replaced by a modify with no recorded fill. The modify row carries the replacement. |
| `open` | No ending is recorded yet. **This is not proof the order is still live.** |
| `not_submitted` | An Alpaca plan, preview or validation record, or a mock row never sent. |
| `anomaly` | The ledger flagged the row, or a KIS mock fill has an attributed quantity of zero. |
| `unknown` | A status with no mapping, or an Alpaca lifecycle that claims a fill with no `filled_qty`. `status` keeps the original. |

## Read this before drawing conclusions

- **`open` is the main caveat for the buy-fill drought.** An accepted order stays
  `accepted` or `pending` until a reconcile records its ending, so a large `open`
  count can mean missing reconcile evidence rather than orders still resting.
- **Reconciler fill times are not always fill times.** Reconciler rows can carry
  the order's time. Do not read `filled_at - accepted_at` on those rows as a
  latency. Websocket rows carry the execution time, so check `fill_source`.
- **Count fills once.** One execution can have a reconciler row and a websocket
  row. Filter on `superseded_duplicate = false` before summing quantities.
- **Exclude opening seeds.** `fill_source = manual_import` includes opening-lot
  seeds, which are not executions from the window.
- **Order rows hold the current state.** Statuses are as of the export, not as of
  `--until`. An order placed in the window can show a fill after the window.
  Only `execution_ledger` rows are limited to the window by fill time.
- **Toss rows are per operation.** A modify adds a row with its own `order_ref`
  and an `original_order_ref`, and a later fill lands on that row, not on the
  `place` row, which becomes `replaced`. Do not filter on `operation_kind = place`
  to count fills. Follow `original_order_ref` to treat a chain as one order.
- **Alpaca rows are per record.** Exclude `terminal_outcome = not_submitted`. Do
  not filter on `record_kind = execution`, because a final reconcile changes a
  finished row's `record_kind` to `reconcile`.
- **Some timestamps are recording times.** `accepted_at` on KIS, live-ledger and
  Toss rows is our send time, not a broker time. `expired_at` and `cancelled_at`
  on KIS and live-ledger rows are when reconcile recorded the ending. Only Toss
  `expired_at` is the broker's own time.
- **KIS US reconciler times are unverified.** The normalizer stamps KIS overseas
  order date and time as KST. If the broker reports them in another zone, those
  `filled_at` and `trading_day` values are shifted, and the US order row's
  `filled_at` stays empty. If that happens, match on `order_ref` and the nearest
  day.
- **Some cells are empty because the source has no value:**
  - KIS KR orders have no `venue` or `time_in_force`.
  - KIS mock rows have no `fill_price`, and their `filled_qty` is the quantity the
    holdings reconciler attributed, not a broker fill. Use `execution_ledger` where
    it has rows.
  - Alpaca rows have no `filled_at`, because the time sits in a raw payload this
    tool does not read.
  - `filled_at` on KIS, Upbit and Toss order rows comes only from in-effect
    `execution_ledger` rows.
- **Not exported.**
  - Kiwoom mock has no order ledger table, so its orders can only be read from
    the broker, and this tool never calls one. `manifest.json` records this
    under `sources_without_ledger`.
  - The NHPLUG mock and Binance demo ledgers are outside this export's scope.
  - Quarantined `execution_ledger` rows are excluded (#1175).
- **`oth_` labels.** A label is copied only if it is short ASCII with no spaces
  and does not look like an identifier. Anything else, including Korean text, is
  replaced by a stable `oth_` hash. Rows still group, but the text is gone.
  `tags_hashed` in the manifest counts these, along with void reasons cut down to
  their code.
- **`void_reason` holds the code only.** The detail after the code can be raw
  exception text, so `submit_exception:<text>` is exported as `submit_exception`.
- **De-identified is not anonymous.** Exact symbols, quantities and times can be
  matched against a broker statement by anyone who holds one. Treat the export as
  private trading data and keep the Drive folder restricted.

## How the leak check works

The check runs on the rows before writing, and again on the CSV and Parquet files
read back from a staging directory. The directory is moved into place only after
both pass. A run fails on any of these:

- A column outside the table's allowlist, a missing column, or a missing or
  unexpected file.
- A cell that does not fit its column's shape. References must be a prefix plus
  32 hex characters, enums are closed, and decimals and timestamps have fixed
  forms.
- A bare integer longer than nine digits in a decimal column.
- A label or symbol that matches an account-number, long-digit, UUID, JWT, token
  prefix, credential-assignment, long-hex or long-opaque pattern.
- A label or symbol containing a raw order or account identifier the tool read
  from any row, a value from `--deny-values-file`, or the salt. An identifier
  shorter than six characters must equal the whole cell to match. A digit-only
  identifier is not compared with symbols, because a KR symbol can equal one by
  coincidence.
- The salt's bytes anywhere in a written CSV or Parquet file.

Label columns are protected before the check as well. An identifier-shaped label
is hashed instead of copied, so one odd row cannot block every export. A failure
therefore usually means a symbol that is not a symbol, a real order id used as a
label, or a code change that wired a raw value to a column.

Columns that can carry payloads or prose are never selected from the database:
raw responses, response messages, thesis, notes, reason, rationale and approval
identities. The KIS mock read takes one field, `attributed_fill_qty`, from the
reconcile detail and nothing else. The tests in
`tests/services/dots_history_export/` plant each kind of value and confirm the run
fails.

The patterns cannot recognize every identifier. A digit run of four or fewer in a
short label, such as a small order number, passes them, and a value's meaning is
not checked. List account numbers in `--deny-values-file`.

## Option: connecting dots to read-only MCP

This section is documentation only. Nothing here is enabled, and connecting an
external AI to MCP is an operator decision that this export does not make.

| | `account_read` | `analysis_readonly` |
| --- | --- | --- |
| Purpose | Account sync for TradingCodex | Analysis for Codex and headless consumers |
| Exposes | Holdings, Toss positions, cash balance, orderable cash, order history for KIS and Toss, Kiwoom mock positions, cash and order history | Market data, screening, analysis, policy, briefing, holdings, Toss positions, journal, forecast and retrospective reads, and order history for each broker |
| Order tools | None: place, modify, cancel, preview and reconcile are not registered | None: the same tools are not registered |
| Writes | None | **Some:** `analysis_artifact_save`, `forecast_save`, `session_context_append`, and side effects such as `get_news` storing articles and `screen_stocks_enrich` filling sectors |
| Token at startup | Required in every transport | Not required by the profile's startup check; the NCP deployment supplies `MCP_ANALYSIS_READONLY_AUTH_TOKEN` |
| NCP endpoint | `http://100.122.100.56:8769/mcp` | `http://100.122.100.56:8768/mcp` |

The tool lists are in `app/mcp_server/tooling/account_read_registration.py` and
`app/mcp_server/tooling/analysis_readonly_registration.py`. See also
`docs/runbooks/account-read-mcp.md` and `docs/runbooks/analysis-readonly-mcp.md`.

Neither profile is equivalent to this export:

- **They are not de-identified.** Order-history tools return raw broker order ids,
  and holdings and cash tools return live balances. The operator's decision allows
  history without those identifiers, so using these profiles would go beyond it.
- **`analysis_readonly` is not strictly read-only.** An external session could
  write forecasts, artifacts and session context into the operator's records.
- **Reads reach live brokers.** `account_read` calls broker APIs on demand, so an
  external session adds load and can use up rate limits.

What connecting an external AI would need:

- **A network path.** Today HAProxy binds only loopback and the tailnet address
  `100.122.100.56`, and the deploy render step refuses any other bind. A hosted AI
  outside the tailnet cannot reach these endpoints.
  - Joining the tailnet needs a dedicated node with an ACL limited to that one
    port.
  - A public endpoint needs a change to the render guard, TLS termination (the
    endpoints are plain HTTP), and an allowlist or authenticating proxy.
- **Its own token.** A dedicated bearer token for dots, held in the secret store
  and never shared with another consumer, with a rotation and revocation plan.
- **A decision on writes.** Choose `account_read`, or a new profile without the
  write tools, if dots must not write.
- **A decision on identifiers.** Add redaction to the tool responses, or accept
  raw order ids. Accepting them changes the operator's current decision.
