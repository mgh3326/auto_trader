# Order Proposals Runbook (ROB-816)

## Purpose

`review.order_proposals` + `review.order_proposal_rungs` is the SOT (source-of-truth)
ledger for **proposed** orders awaiting human approval, prior to any broker
submission. It replaces ad-hoc "propose in chat, submit blind" flows with a
persisted, replayable record: one `order_proposals` row per proposal group
(symbol/side/market/account context + thesis/rationale), and one or more
`order_proposal_rungs` child rows (one per execution ladder rung — price/qty
pair) tracking each rung's own execution lifecycle independently.

This runbook covers the deployed ROB-816 foundation through native Toss
proposal execution and ROB-858 Toss loss-cut/reconcile convergence:

- **PR 1** — data model + pure state machine + service + three read/create
  MCP tools (`order_proposal_create`/`get`/`list`). Merged as
  [#1491](https://github.com/mgh3326/auto_trader/pull/1491) and deployed on
  `main`/production.
- **PR 2** — the Telegram button-approval flow documented in the
  "Telegram Approval" sections below (bot setup, webhook receiver,
  click-time revalidation, safety boundaries, live smoke, troubleshooting,
  evidence template). Merged as
  [#1490](https://github.com/mgh3326/auto_trader/pull/1490) and deployed on
  `main`/production; Linear records the Telegram activation smoke as deployed.
- **PR 3a** — loss-cut approval bindings, proposal expiry/void safety, and
  bounded Telegram rejection reasons. This slice adds
  `order_proposal_void` and the four nullable loss-cut columns documented
  below. Merged as
  [#1497](https://github.com/mgh3326/auto_trader/pull/1497) and deployed on
  `main`/production.
- **PR 3a-2 + PR 3b** — fail-closed Telegram submit identity binding, Upbit
  loss-cut support, and native Toss proposal preview/submit routing.
- **PR 3c / #1498** — generic live-ledger proposal-rung projection.
- **ROB-858** — opens `toss_live × equity_kr|equity_us` loss-cut only after
  adding shared ROB-800 guards, Toss audit fields, broker-evidence projection,
  and a terminal-row repair sweep. The canonical GO rationale and risks are in
  [`docs/plans/2026-07-13-rob-858-toss-losscut-decision.md`](../plans/2026-07-13-rob-858-toss-losscut-decision.md).

There is still **no**
`order_proposal_approve` / `order_proposal_submit` MCP tool in any slice —
the only path from a proposal row to a live broker order is the Telegram
button flow described below.

See the full design in
[`docs/plans/2026-07-10-rob-816-order-proposals-telegram-approval-implementation-plan.md`](../plans/2026-07-10-rob-816-order-proposals-telegram-approval-implementation-plan.md).

---

## Safety Boundaries

- **Writes only via `OrderProposalsService`** (`app/services/order_proposals/service.py`).
  `OrderProposalRepository` is imported only by its owning service module — an
  AST guard test (`tests/services/order_proposals/test_no_repository_imports.py`)
  enforces this. Direct SQL INSERT/UPDATE/DELETE against `review.order_proposals`
  / `review.order_proposal_rungs` is not permitted.
- **Proposal creation is NOT a broker mutation.** `order_proposal_create`
  persists a row describing an intended order; it never calls a broker
  client, never submits, and never touches `_place_order_impl` or any
  existing order-send path.
- **No submit without Telegram approval.** There is deliberately no
  `order_proposal_approve` / `order_proposal_submit` MCP tool in either PR.
  The only way a proposal reaches a live broker order is the Telegram
  approve/deny flow shipped in PR 2 (`app/services/order_proposals/telegram_callback.py`
  → `app/services/order_proposals/revalidation.py`). See "Telegram Approval
  — Activation" below for how to turn it on.
- **Accepted != filled.** A broker ACK (`record_ack`) or resting-order
  confirmation (`record_resting`) is recorded on the rung as `acked` /
  `resting` — never as a fill. The Telegram approval flow itself never writes
  `filled`/`partially_filled`; broker reconcile does so only from confirmed
  evidence through `record_fill_evidence`. Toss non-dry reconcile also repairs
  terminal ledger rows whose first proposal projection failed. See
  `revalidation.py`'s module docstring ("Principle #6").
- **Nonce replay defense.** Every Telegram button click must present the
  `approval_nonce` currently stored on the group row; `consume_approval_nonce`
  (`app/services/order_proposals/service.py`) takes a `for_update=True` row
  lock and raises `OrderProposalError("nonce_mismatch")` on a stale nonce (a
  newer message already minted a different one) or `OrderProposalError(
  "nonce_replay")` on an already-consumed nonce (a double-tap or replayed
  callback) — so neither a stale cached message nor a duplicate callback can
  ever re-trigger approval/deny. See Troubleshooting for the two error
  strings.
- **Chat allowlist (authz, separate from the webhook secret).**
  `handle_callback_update` rejects any callback whose `chat.id` is not in
  `settings.order_proposals_telegram_chat_allowlist`
  (`ORDER_PROPOSALS_TELEGRAM_CHAT_ALLOWLIST_STR`) before doing anything else
  — distinct from the `ORDER_PROPOSALS_TELEGRAM_TOKEN` webhook secret, which
  only proves the request came from Telegram (authn), not that it came from
  an approved chat (authz).
- **Fresh broker-specific checks at every click.** A Telegram approve does not
  submit the payload that was true at proposal-create time. KIS/Upbit rerun
  their applicable Paperclip/ROB-800 checks through the shared
  `_place_order_impl` dry-run preview and repeat final guards at submit. Toss
  instead calls `toss_preview_order`, never `_place_order_impl`; that preview
  supplies the normalized payload/tick plus its actual read-only warning,
  price/cost, NXT-context, and advisory sector-concentration checks. A Toss
  `loss_cut` preview also reruns the shared Paperclip/caller/retrospective gate
  and validates the current-price slip band. On unchanged payload,
  `toss_place_order` applies the mutation activation/confirmation gates,
  high-value check, warnings guard, opposite-pending-order guard, sell-loss
  guard, and configured NXT preflight immediately before the POST. Sell-loss
  and required mutation gates fail closed; sector concentration is advisory
  and never authorizes or blocks the send. A create-time check is never an
  approval-time bypass. See "Approval Flow" below.
- **Explicit submit identity, scoped to Telegram.**
  `ORDER_PROPOSALS_SUBMIT_AGENT_ID` defaults to the empty string. The Telegram
  approve callback trims it and temporarily binds it as caller identity only
  around proposal revalidation/submission, then restores the previous context.
  Missing or whitespace-only values bind `None`; they never inherit an outer
  request identity and a `loss_cut` submission therefore fails closed.
  Operators must set a deliberate identity and include the exact same trimmed
  value in `LOSS_CUT_ALLOWED_AGENT_IDS`. Do not add a hardcoded UUID to satisfy
  this contract.
- **Loss-cut binding scope.** `exit_intent="loss_cut"` proposals are supported
  for `kis_live|toss_live` + `equity_kr|equity_us` and `upbit` + `crypto`.
  All lanes remain limit-sell/live only and rerun the shared Paperclip,
  caller, retrospective-symbol/trigger, and 72-hour freshness checks at
  preview and submit. The second freshness check is intentional: a valid
  create can become stale while waiting for Telegram approval.
- **Native Toss routing and exact handoff.** `toss_live` +
  `equity_kr|equity_us` proposals route through `toss_preview_order` and
  `toss_place_order`, never the shared `_place_order_impl`. Preview supplies
  the canonical wire price/quantity, including KR tick normalization, while
  `toss_place_order` owns the fail-closed sell-loss and mutation checks directly
  before broker POST. Sector concentration from preview is advisory/soft.
  Proposal `Decimal` values cross the Toss tool boundary as exact `str | int`
  values, with no float coercion. Revalidation privately binds a stable client
  ID derived only from `proposal_id + rung` around both preview and submit,
  fails closed unless preview returns that exact ID, and privately hands the
  proposal correlation/rung into the Toss ledger. These values are not public
  MCP inputs. Submit reuses the preview's raw `approval_hash`; the proposal
  rung stores the actual `approval_hash_digest` returned by `toss_place_order`.
  For `loss_cut`, that supplied token is mandatory even when
  `TOSS_APPROVAL_HASH_MODE=off`. The Toss ledger also stores `exit_intent`,
  `retrospective_id`, and `approval_issue_id` for durable audit.
  Broker acceptance is not a fill, and any post-send ambiguity remains
  `unverified` for reconciliation rather than being retried or voided.
- **Default OFF.** `ORDER_PROPOSALS_ENABLED=false` by default
  (`app/core/config.py`). When off, the four MCP tools are not registered
  in either the default profile (`registry.py`) or the 8770 TradingCodex
  execution profile allowlist (`tradingcodex_execution_registration.py`).
- **Pure state machine.** `app/services/order_proposals/state_machine.py` is
  stdlib-only (no broker/DB/network imports). The DB `CheckConstraint`s only
  validate the string bag (`RUNG_STATES` / `GROUP_STATES`); the actual
  transition graph is enforced by `assert_rung_transition(...)` in the
  service layer, fail-closed via `OrderProposalInvalidStateTransition`.
- **Immutable payload + replacement lineage.** A proposal is never mutated
  in place for a price/qty change — a same-price/qty revalidation (e.g. TTL
  refresh) is a new validation revision on the same row; an actual
  price/qty change creates a new proposal row linked via
  `supersedes_proposal_id` / `superseded_by_proposal_id`, and the original is
  marked `superseded`.

---

## Activation

```bash
ORDER_PROPOSALS_ENABLED=true
```

Setting this env var (or config override) surfaces the four MCP tools
(`order_proposal_create`, `order_proposal_get`, `order_proposal_list`,
`order_proposal_void`) in:

- The default MCP profile (`app/mcp_server/tooling/registry.py`).
- The 8770 TradingCodex execution profile allowlist
  (`app/mcp_server/tooling/tradingcodex_execution_registration.py`).

With the flag off (default), none of the four tools are registered anywhere
and the tables remain unused (migration is additive and applies regardless
of the flag — `alembic upgrade head` is safe to run at any time).

Restart the MCP process after changing the flag:

```bash
uv run python -m app.mcp_server.main
```

---

## Lifecycle States

### Rung state diagram (per-rung execution lifecycle)

```
DRAFT → PENDING_APPROVAL → REVALIDATING → APPROVED → SUBMITTING → ACKED | RESTING
  → (FILLED | PARTIALLY_FILLED | CANCELLED | EXPIRED)
```

Branches:

- `REVALIDATING → NEEDS_RECONFIRM` — payload changed; requires re-approval.
- `REVALIDATING → PENDING_APPROVAL` — transient revalidation/guard failure,
  retryable, fail-closed.
- `PENDING_APPROVAL → REJECTED` — denied.
- `SUBMITTING / ACKED / RESTING → UNVERIFIED` — broker timeout/unknown.
  **Never auto-voided.**
- `PENDING_APPROVAL → VOIDED_LOCAL_STALE` — evidence-absent local staleness
  cleanup only.
- `* → SUPERSEDED` — replacement lineage (new proposal supersedes this rung's
  group).
- `* → VOIDED` — explicit void.

**Terminal rung states:** `FILLED, CANCELLED, EXPIRED, REJECTED, VOIDED,
VOIDED_LOCAL_STALE, SUPERSEDED`.

`UNVERIFIED` is a **holding** state, resolvable by later broker evidence —
never terminal, never auto-voided (occupancy + evidence-based stale cleanup
principle: a proposal only becomes `VOIDED_LOCAL_STALE` when broker evidence
is **absent**, not merely delayed or unknown).

### Group rollup (`order_proposals.lifecycle_state`)

The group-level state is a coarser rollup over all sibling rungs, recomputed
by `OrderProposalsService._recompute_group_state(rungs)` after every rung
transition (`transition_rung(...)`):

| Group state | When |
|---|---|
| `proposed` | Default; no rung has reached `approved` or later. |
| `approved` | At least one rung `approved`, none submitting/executed yet. |
| `partially_submitted` | Some rungs at/past `submitting` (acked/resting/partially_filled/filled/submitting) **and** some rungs still pre-submit (pending_approval/revalidating/approved/needs_reconfirm). |
| `submitted` | All rungs at/past `submitting`, none still pre-submit. |
| `terminal` | All rungs in a terminal state, and not fully `rejected` or fully `voided`/`voided_local_stale`. |
| `rejected` | All rungs `rejected`. |
| `voided` | All rungs `voided` or `voided_local_stale`. |
| `superseded` | Set directly when this proposal is replaced by a newer revision. |
| `expired` | *(member of the DB `GROUP_STATES` CHECK bag, but the current rollup never assigns it — see Known Item below.)* |

**Known non-blocking item:** `_recompute_group_state` never rolls an
all-`expired`-rung group up to the group-level `expired` state — an
all-`expired` rung set falls into the generic `terminal` bucket instead
(`expired` is a subset check that only special-cases `rejected` and
`voided`/`voided_local_stale`, not `expired`). This is transcribed verbatim
from the plan's own service logic and is a known item flagged for the plan
author, not a bug fixed in this PR. **Rung-level state is still correctly
recorded as `expired`** — only the group-level label is coarser than it
could be. Do not rely on `lifecycle_state='expired'` at the group level to
find all-expired proposal groups; query rung state instead (see DB
Verification below).

---

## MCP Tools

Read, create, and local void only — **no approve/submit tool exists in this PR.**

### `order_proposal_create(symbol, market, account_mode, side, order_type, proposer, rungs, thesis=None, strategy=None, rationale=None, broker_account_id=None, lot_context=None, valid_until=None, supersedes_proposal_id=None, exit_intent=None, exit_reason=None, retrospective_id=None, approval_issue_id=None)`

Persists a new proposal group + its rungs. `rungs` is a list of
`{"rung_index": int, "side": str, "quantity": str, "limit_price": str|None,
"notional": str|None}`. NOT a broker mutation. Returns
`{success, proposal_id, lifecycle_state, rungs}` on success or
`{success: false, error}` on validation failure.

If `supersedes_proposal_id` is given, the referenced proposal is marked
`superseded` and lineage (`root_proposal_id`, `supersedes_proposal_id`) is
linked on the new row.

When `valid_until` is omitted, the service assigns the next `00:00 KST`. The
loss-cut fields `exit_intent`, `exit_reason`, `retrospective_id`, and
`approval_issue_id` are nullable for non-loss-cut proposals. For
`exit_intent="loss_cut"`, all three companion fields are required. At create
time, the retrospective must exist, belong to the same symbol, have
`trigger_type="stop_loss"` or `trigger_type="thesis_change"`, and be no more
than 72 hours old. The Paperclip
approval issue is revalidated later at the Telegram click; passing this
create-time validation never authorizes a future click by itself.

### `order_proposal_get(proposal_id)`

Read-only fetch of a proposal group and its rungs by `proposal_id` (UUID).
Returns `{success: false, error: "not_found"}` if missing.

### `order_proposal_list(limit=50, symbol=None, lifecycle_state=None)`

Read-only list of recent proposal groups, optionally filtered by `symbol`
and/or group-level `lifecycle_state`. `limit` is clamped to `1..200`.

### `order_proposal_void(proposal_id, reason)`

Locally voids a proposal only when every rung is safely pre-submit. `reason`
must be non-blank and is retained as the audit reason. The tool refuses rather
than partially mutating a proposal when any rung might have an outstanding
broker state: rungs at or after submit (`submitting`, `acked`, `resting`, or
`unverified`) require broker-evidence handling instead of a local void.

### Expire versus void

- **Expire automatically:** clicking Telegram **Approve** after `valid_until`
  expires the proposal during click handling; operators do not need to issue a
  separate expiry command.
- **Void explicitly:** use `order_proposal_void` only to abandon a still
  pre-submit proposal deliberately, with a non-blank operator reason. Do not
  use it to resolve a possible broker submission; inspect/reconcile that state
  first.

---

## ROB-832 Replace and Cancel Actions

`order_proposal_create` now accepts `action="place"` (the default) and
`target_broker_order_id=None` (the default). `place` keeps the existing
multi-rung contract and forbids a target. `replace` requires a target and
exactly one new-order rung. `cancel` requires a target and exactly one rung
that exactly snapshots the target's side, remaining quantity, and limit price;
it never submits a new order.

The supported target-action tuples are only `kis_live/equity_kr`,
`kis_live/equity_us`, and `upbit/crypto`. Create-time target lookup is
read-only and captures the open-order evidence. Manual or unattributed broker
orders are valid targets when that evidence matches the requested proposal.

At the Telegram click, replace re-fetches and compares the target evidence,
performs the fresh replacement preview, requests cancellation, and requires an
independent broker confirmation that the target is no longer executable before
submitting the replacement. A cancel action follows the same evidence and
confirmation path but never previews or submits a new order. A cancel failure,
timeout, mismatch, or ambiguous confirmation must therefore leave replacement
submission at zero.

### Lineage and recovery queries

```sql
SELECT
  proposal_id, action, target_broker_order_id, lifecycle_state,
  source_asof->'target_order_snapshot' AS approved_target_snapshot
FROM review.order_proposals
WHERE proposal_id = :proposal_id;

SELECT rung_index, state, broker_order_id, correlation_id, void_reason
FROM review.order_proposal_rungs
WHERE proposal_pk = (
  SELECT id FROM review.order_proposals
  WHERE proposal_id = :proposal_id
);
```

For a replacement, `target_broker_order_id` always identifies the original
order selected for cancellation. The replacement's newly accepted broker order
ID is the rung's `broker_order_id`; use its `correlation_id` with the original
target ID to reconstruct the full lineage. A cancel action retains the target
ID as its audit identity and does not create a replacement broker order.

### Replace/cancel recovery by rung state

| Rung state and outcome | Retry? | Operator action |
|---|---|---|
| `pending_approval` with `result="error"` caused by a transient target fetch failure | **Yes** | Retry the approval after broker connectivity/evidence retrieval recovers. No cancel request or replacement submit was attempted. |
| `rejected` because the fresh target snapshot mismatched the approved snapshot | **No** | Do not retry the existing proposal: the approved target is stale. Create a new proposal from a fresh target snapshot and obtain a new approval. |
| `unverified` after cancel/confirmation ambiguity | **No — not until reconciled** | Stop retries. Reconcile fresh broker evidence for the original `target_broker_order_id` and, where a replacement may have been submitted, the rung's `correlation_id` and `broker_order_id`. Take further action only after operator reconciliation establishes the broker's actual state. |

---

## DB Verification

```sql
-- A specific proposal group + its rungs
SELECT
  id, proposal_id, root_proposal_id, revision,
  supersedes_proposal_id, superseded_by_proposal_id, no_resubmit, void_reason,
  symbol, market, account_mode, side, order_type, proposer,
  lifecycle_state, valid_until,
  exit_intent, exit_reason, retrospective_id, approval_issue_id,
  validated_at, created_at, updated_at
FROM review.order_proposals
WHERE proposal_id = '<PROPOSAL_UUID>';

SELECT
  id, proposal_pk, rung_index, side, quantity, limit_price, notional,
  state, approval_hash_digest, approval_revision, idempotency_key,
  broker_order_id, correlation_id, filled_qty, void_reason,
  created_at, updated_at
FROM review.order_proposal_rungs
WHERE proposal_pk = (
  SELECT id FROM review.order_proposals WHERE proposal_id = '<PROPOSAL_UUID>'
)
ORDER BY rung_index;

-- All groups with at least one rung stuck UNVERIFIED (needs operator attention)
SELECT p.proposal_id, p.symbol, p.lifecycle_state, r.rung_index, r.state, r.broker_order_id
FROM review.order_proposals p
JOIN review.order_proposal_rungs r ON r.proposal_pk = p.id
WHERE r.state = 'unverified'
ORDER BY p.created_at DESC;

-- All-expired rung groups (group-level lifecycle_state stays 'terminal',
-- NOT 'expired' -- see Known Item above; query rung state directly)
SELECT p.proposal_id, p.symbol, p.lifecycle_state AS group_state
FROM review.order_proposals p
WHERE p.id IN (
  SELECT proposal_pk FROM review.order_proposal_rungs
  GROUP BY proposal_pk
  HAVING bool_and(state = 'expired')
);

-- Recent proposals by symbol
SELECT proposal_id, symbol, side, lifecycle_state, created_at
FROM review.order_proposals
WHERE symbol = '<SYMBOL>'
ORDER BY created_at DESC
LIMIT 50;

-- Replacement lineage for a superseded proposal
SELECT proposal_id, revision, supersedes_proposal_id, superseded_by_proposal_id, lifecycle_state
FROM review.order_proposals
WHERE root_proposal_id = (
  SELECT root_proposal_id FROM review.order_proposals WHERE proposal_id = '<PROPOSAL_UUID>'
)
ORDER BY revision;
```

No SQL INSERT/UPDATE/DELETE against these tables is supported outside
`OrderProposalsService` — treat the above as read-only verification only.

---

## Telegram Approval — Activation

PR 2 adds a second, independent env gate on top of `ORDER_PROPOSALS_ENABLED`
(§ Activation above). Both must be true for a proposal to actually reach a
Telegram chat and be approvable; `ORDER_PROPOSALS_ENABLED=true` alone still
only gets you the read/create MCP tools with no approval surface.

The approval settings live in `app/core/config.py`:

| Env var | Default | Purpose |
|---|---|---|
| `ORDER_PROPOSALS_TELEGRAM_ENABLED` | `false` | Master gate. When `false`: `order_proposal_create` never dispatches an approval message (best-effort no-op — see `order_proposal_tools.py`), and `POST /trading/api/telegram/callback` returns `503 {"error": "order_proposals_telegram_disabled", ...}` without touching the DB. |
| `ORDER_PROPOSALS_TELEGRAM_BOT_TOKEN` | `""` | Defined in the config schema for this feature, but **not currently read by any runtime code path** — confirmed by repo-wide grep (only referenced in the plan doc and this config declaration). The Telegram Bot API calls that actually send/edit/answer approval messages go through the existing process-wide `TradeNotifier` singleton, which is configured from the **pre-existing** `TELEGRAM_TOKEN` / `TELEGRAM_CHAT_IDS_STR` settings (`app/monitoring/trade_notifier/runtime.py::configure_trade_notifier_from_settings`) — the same bot/token already used for every other trade notification in this repo (Task 10's "reuse `TradeNotifier`, no regression" design). **Set `TELEGRAM_TOKEN` (not this variable) to the BotFather token that should actually send approval messages.** |
| `ORDER_PROPOSALS_TELEGRAM_TOKEN` | `""` | The webhook **secret token** — a value you choose, registered with Telegram via `setWebhook`'s `secret_token` param (see "Telegram Bot Setup" below). Distinct from the bot token. Gates every request under `/trading/api/telegram/` in `AuthMiddleware` (`TELEGRAM_CALLBACK_PATH_PREFIX`). |
| `ORDER_PROPOSALS_TELEGRAM_TOKEN_HEADER` | `X-Telegram-Bot-Api-Secret-Token` | The HTTP header Telegram sends the secret token back in. Telegram's own webhook mechanism hard-codes this header name — only override if you're proxying through something that renames headers. |
| `ORDER_PROPOSALS_TELEGRAM_CHAT_ALLOWLIST_STR` | `""` | Comma-separated Telegram chat IDs, parsed via `settings.order_proposals_telegram_chat_allowlist`. Does double duty: (1) `handle_callback_update` uses the full list as the approve/deny **authz allowlist** (any chat not in it gets `chat_not_allowed`); (2) `send_proposal_for_approval` (`dispatch.py`) sends the initial approval message to `allowlist[0]` only — the **first** entry. Empty means no chat is allowed to approve/deny and no message is ever dispatched (`dispatch.py` no-ops). This is a distinct setting from the pre-existing `TELEGRAM_CHAT_IDS_STR` (used by `TradeNotifier`'s other, non-approval notifications) — the two lists are not required to match. |
| `ORDER_PROPOSALS_SUBMIT_AGENT_ID` | `""` | Caller identity temporarily bound only during Telegram approval revalidation/submission. There is no default submit identity. The operator must set a non-blank value and add the exact same trimmed identity to `LOSS_CUT_ALLOWED_AGENT_IDS`. Missing or whitespace-only input becomes no identity and keeps `loss_cut` fail-closed. Never solve this with a hardcoded UUID. |

For a deployment that will approve loss cuts, configure both sides of the
identity binding with an operator-managed identity (placeholder shown; do not
copy a real ID into this runbook):

```bash
ORDER_PROPOSALS_SUBMIT_AGENT_ID=<proposal-submit-agent-id>
LOSS_CUT_ALLOWED_AGENT_IDS=<existing-allowed-agent-ids>,<proposal-submit-agent-id>
```

### Paperclip issue contract for each loss-cut order

`approval_issue_id` is a Paperclip issue key, not a Telegram callback ID or an
internal approval row. It must match `^[A-Z]+-\d+$`, and the execution
environment must receive HTTP 200 JSON with exact lowercase `status="done"`
from `$PAPERCLIP_API_URL/api/issues/<key>`. Issue lookup failure, malformed
JSON, timeout, non-200, or any other status fails closed.

Create a new issue for each order. Record account, symbol, `side=sell`, limit,
quantity, retrospective ID, and expiry in its title/body, obtain the human
decision, then mark it done. Do not reuse ROB-800/ROB-858 or another unrelated
completed implementation/umbrella issue: the current verifier checks status
but cannot yet prove that issue metadata matches the order payload.

Verify the dedicated issue read-only in the execution environment without
printing the API token:

```bash
APPROVAL_ISSUE_ID=ROB-<number>
curl -fsS \
  -H "Authorization: Bearer $PAPERCLIP_API_KEY" \
  "$PAPERCLIP_API_URL/api/issues/$APPROVAL_ISSUE_ID" \
  | jq -e '.status == "done"'
```

Create a fresh retrospective for the execution decision. Its symbol and
trigger (`stop_loss|thesis_change`) must match, and its `created_at` must still
be within 72 hours at both proposal create and submit. Updating an older row
does not renew `created_at`; create a new correlation/row instead.

### Toss loss-cut fill polling requirement

Toss acceptance is not fill evidence, and automatic polling is default-off.
Do not enable Toss loss-cut proposals until one of these operational paths is
owned and tested:

1. Set `TOSS_FILL_POLL_ENABLED=true`, choose an operator-reviewed
   `TOSS_FILL_POLL_CRON`, deploy the worker/scheduler, and verify
   `toss_live.poll_fills_periodic` is firing during the relevant KR/US session;
   or
2. after every accepted order, run
   `toss_reconcile_orders(order_id=<broker-order-id>, dry_run=False)` and repeat
   until broker evidence is terminal.

The non-dry reconcile books only cumulative broker-confirmed deltas, projects
proposal rungs to `partially_filled|filled|cancelled`, and sweeps terminal Toss
ledger rows whose earlier proposal projection failed. `dry_run=True` is useful
for inspection but does not satisfy this convergence SLA.

Restart the process that serves both the MCP tools and the FastAPI app after
changing any of these (same restart as § Activation above).

---

## Telegram Bot Setup

1. **Create the bot.** In Telegram, message `@BotFather`, run `/newbot`,
   follow the prompts. BotFather returns a bot token — set this as the
   **pre-existing** `TELEGRAM_TOKEN` env var, not
   `ORDER_PROPOSALS_TELEGRAM_BOT_TOKEN` (see the note in "Telegram Approval
   — Activation" above: the latter is not currently read by any runtime
   code — `TradeNotifier` is configured from `TELEGRAM_TOKEN`). Never
   commit the token; place it in your operator secret store / `.env`.
2. **Find your chat ID.** Send any message to the new bot (or add it to a
   group), then call
   `https://api.telegram.org/bot<BOT_TOKEN>/getUpdates` and read
   `result[].message.chat.id`. Put that value in
   `ORDER_PROPOSALS_TELEGRAM_CHAT_ALLOWLIST_STR` — this is both the
   dispatch target (message goes to the first entry) and the approve/deny
   authz allowlist (comma-separate additional approver chat IDs if more
   than one person should be able to click approve/deny; only the first
   entry receives the initial message).
3. **Choose a webhook secret.** Generate a random string yourself (e.g.
   `openssl rand -hex 32`) and set it as `ORDER_PROPOSALS_TELEGRAM_TOKEN`.
   This is not issued by Telegram — you pick it and hand it to Telegram in
   the next step.
4. **Register the webhook** against your public HTTPS host:

   ```bash
   curl "https://api.telegram.org/bot<BOT_TOKEN>/setWebhook" \
     -d "url=https://<host>/trading/api/telegram/callback" \
     -d "secret_token=<ORDER_PROPOSALS_TELEGRAM_TOKEN>"
   ```

   Telegram will call this URL on every button click and echo the secret
   token back on the `X-Telegram-Bot-Api-Secret-Token` header (the default
   value of `ORDER_PROPOSALS_TELEGRAM_TOKEN_HEADER` — confirmed at
   `app/core/config.py:771`), which `AuthMiddleware` compares against
   `ORDER_PROPOSALS_TELEGRAM_TOKEN` via `hmac.compare_digest` before letting
   the request reach the router.
5. **Verify registration:**
   ```bash
   curl "https://api.telegram.org/bot<BOT_TOKEN>/getWebhookInfo"
   ```
   Confirm `url` matches your callback endpoint and `last_error_message` is
   empty.

---

## Callback Receiver — Design

**Implemented (default): webhook.** `POST /trading/api/telegram/callback`
(`app/routers/telegram_callback.py`) is the only receiver that exists in
this repo. It:

- Is gated `503` when `ORDER_PROPOSALS_TELEGRAM_ENABLED=false` (checked in
  the router, before any DB/service work).
- Is auth-gated `403`/`401` at the middleware layer via the webhook secret
  token (see "Telegram Approval — Activation" and Troubleshooting below) —
  the router itself does no auth.
- Accepts the raw Telegram `Update` payload as a permissive `dict` (Telegram's
  schema is large/evolving; the endpoint must not reject on unknown fields).
- Delegates everything else to
  `app.services.order_proposals.telegram_callback.handle_callback_update`,
  which never raises (fail-closed webhook contract — always returns
  `200 {"ok": true}` once past the enable gate, regardless of what happened
  inside).
- Is driven purely by Telegram's own push (Telegram calls your server); there
  is **no Telegram long-polling loop, TaskIQ receiver, cron, or Prefect flow**.
  This receiver statement does not cover broker fill reconciliation: the
  default-off Toss fill-poller TaskIQ path may later project confirmed broker
  evidence onto proposal rungs, as required by the Toss loss-cut SLA above.

**NOT implemented — long-polling alternative (documented only, out of
scope).** `scripts/order_proposals_telegram_poller.py` is referenced in the
plan's "Out of Scope (PR 3)" section as the alternative receiver for
deployments that lack a public HTTPS endpoint (e.g. local dev behind NAT). It
does **not exist in this repo today** — do not attempt to run it, and do not
assume any polling infrastructure is present. If you need this mode, it is a
bounded follow-up PR, not a flag to flip.

---

## Approval Flow

A Telegram approve click does **not** submit "whatever was true when the
proposal was created." It re-validates from scratch, at click time, and only
submits if nothing about the world has moved since. This is deliberate:
proposals can sit unapproved for minutes-to-hours, and a stale price/qty
submitted blind would defeat the entire point of a human-approval gate.

**Step by step** (`app/services/order_proposals/telegram_callback.py` →
`app/services/order_proposals/revalidation.py`):

1. **Chat-allowlist authz.** `handle_callback_update` checks
   `chat.id` against `settings.order_proposals_telegram_chat_allowlist`
   first, before parsing anything else about the callback.
2. **Callback-data parse + proposal resolution.** `parse_callback_data`
   extracts `(action, proposal_short, nonce)` from the compact
   `op:<8-char-prefix>:<nonce>` / `dn:<8-char-prefix>:<nonce>` string (no raw
   `approval_hash` ever appears in a Telegram message —
   `build_approval_message` explicitly redacts `payload_hash`,
   `approval_hash`, the nonce, and every rung's `approval_hash_digest` from
   the rendered text). `_resolve_proposal_id` then matches the 8-char prefix
   against `proposed`-state candidates, failing closed (no match / multiple
   matches → unresolved) rather than guessing.
3. **Nonce replay guard.** `consume_approval_nonce` takes a row lock
   (`for_update=True`) and marks the nonce used; a second click with the
   same already-consumed nonce raises `nonce_replay`, and a click after a
   fresh nonce has already been minted for a newer message (e.g. a
   `NEEDS_RECONFIRM` cycle) raises `nonce_mismatch` — see Troubleshooting
   for the distinction.
4. **Commit lease.** For approve only, `acquire_commit_lease` takes a
   short-lived (`lease_seconds=10` default) in-flight lock on the group row
   — this is what prevents a double-tap on the approve button (two Telegram
   updates arriving almost simultaneously) from racing two submits. If the
   lease is already held, the second click is answered "처리 중" and does
   nothing further.
5. **Fresh dry-run preview → broker-specific checks.** `revalidate_and_submit`
   re-runs every `pending_approval` rung through a fresh
   `place_order_fn(dry_run=True, ...)` call. For `kis_live` equities and
   `upbit` crypto this delegates to `_place_order_impl`, which enforces the
   applicable Paperclip and ROB-800 guards and repeats final guards on live
   submit. For `toss_live` equities it delegates to `toss_preview_order`,
   never `_place_order_impl`; Toss preview returns the canonical wire
   `payload_preview`, including KR tick normalization, and performs only its
   read-only warning, price/cost, NXT-context, and advisory sector-concentration
   checks. For Toss `loss_cut`, it additionally runs the shared ROB-800
   authorization and loss-cut slip-band guard. After an
   unchanged comparison, `toss_place_order` runs confirmation/activation,
   high-value, warnings, opposite-pending-order, sell-loss, and configured NXT
   guards immediately before POST. Sell-loss and required mutation gates fail
   closed; sector concentration remains advisory/soft. This happens on every
   click regardless of the create-time retrospective check. A preview guard
   rejection comes back as `guard_blocked` and the rung returns to
   `pending_approval` (retryable, not terminal).
6. **Price/qty comparison against what the operator approved.** The fresh
   preview's normalized `price`/`quantity` is compared (`_norm`, which
   canonicalizes `NUMERIC(38,12)` DB values against fresh preview values so
   `Decimal("2226000.000000000000")` and `Decimal("2226000")` compare equal)
   against the rung's stored `limit_price`/`quantity`. Market-order rungs
   compare quantity only (`limit_price` is always `None` by design for
   market orders, so comparing it would always spuriously mismatch).
7. **Unchanged → submit; changed → `NEEDS_RECONFIRM`.** If the comparison
   matches, the rung transitions `approved → submitting` and is actually
   submitted (`dry_run=False`) using the **freshly minted** `approval_hash`
   from step 5's preview — never the one from the original proposal-create
   preview. Toss receives a privately bound client ID derived only from the
   proposal ID and rung for both preview and submit, so retries and date
   boundaries cannot change the identity; preview must return the exact bound
   ID or submission fails closed. The proposal correlation and rung use the
   same private binding and are not operator-controlled MCP parameters. Toss
   proposal numerics are converted at the adapter boundary to canonical
   `str | int` values, never floats. After an accepted Toss send, the proposal
   ledger records `toss_place_order`'s actual `approval_hash_digest`; it does
   not substitute the raw approval token. If the comparison does *not* match, the
   rung transitions to
   `needs_reconfirm` and a brand-new Telegram message is sent
   (`build_approval_message(..., diff=...)`) showing an explicit
   before/after, with a freshly minted `approval_nonce` — the operator must
   click again to approve the *new* numbers. **This is the load-bearing
   distinction: auto-revalidation is not the same as auto-approving a
   payload change.** The system re-checks freely; it never silently accepts
   a different price/qty on the operator's behalf.
8. **Submit outcome classification.** `_classify_submit` records `acked`
   (market orders) or `resting` (limit orders) on explicit broker success,
   `rejected` on explicit broker/guard rejection, and `unverified` — never a
   terminal state — on anything ambiguous (submit exception, unrecognized
   response shape, missing `broker_order_id`). Toss reconcile later books
   confirmed fill evidence and projects partial/fill/cancel states into the
   proposal rung. A terminal-ledger sweep retries any projection that failed
   after the row left the open scan. See Safety
   Boundaries above for why `unverified` is never auto-voided.

For a loss-cut proposal, the Telegram approval text shows the operator-facing
`손절 근거` and `회고 #<retrospective_id>`. It intentionally does **not** expose
the internal `approval_issue_id`; that identifier remains an audit and
revalidation input rather than an operator instruction.

**The four time concepts** (all columns on `order_proposals` /
`order_proposal_rungs`, see `app/models/order_proposals.py`):

| Concept | Column | Scope | Meaning |
|---|---|---|---|
| `valid_until` | `order_proposals.valid_until` | proposal-level | When this operator-approval *offer* stops being worth acting on at all — a proposal an operator never got to in time should not be approvable indefinitely. |
| `validated_at` | `order_proposal_rungs.validated_at` | rung-level | When this specific rung last completed a `revalidate_and_submit` pass — set by `record_ack`/`record_resting`/`record_unverified`/`mark_needs_reconfirm` in `service.py`. (`order_proposals.validated_at` exists as a group-level column in the model but is not written by any current PR-1/PR-2 code path — do not rely on it.) |
| `commit_lease_until` | `order_proposals.commit_lease_until` | proposal-level | The short (~10s) in-flight lock from `acquire_commit_lease` that stops a double-click from double-submitting — not a business-meaningful deadline, purely a mutex with a TTL. |
| *(resting deadline)* | rendered from `group.source_asof["resting_deadline"]` in `build_approval_message`'s `_build_time_lines` | proposal-level, optional | How long a resting (limit, unfilled) order is expected to stay open before it would be expected to expire/need attention — surfaced in the Telegram message when present, not a DB column of its own. |

**Server-internalized TTL (Task 13's design).** The `_place_order_impl` path
underneath `_default_place_order_fn` has its own `approval_hash` TTL (~300s,
ROB-651/ROB-653) meant to bound the time between "operator saw this exact
price/qty" and "it actually got submitted." Because `revalidate_and_submit`
mints a **brand-new** `approval_hash` from a **fresh** preview at the moment
of submission (step 5–7 above), that hash's age at submit time is always
~0 seconds — the underlying 300s TTL is structurally never at risk of
tripping from a slow *human* round-trip (an operator taking 20 minutes to
click "approve" doesn't matter; what matters is the freshness of the preview
taken *at the click*, not the freshness of the proposal's original numbers).

---

## Live Smoke (operator-only)

**Not run in CI.** Every test in this repo's suite mocks the real
Telegram Bot API, the real broker, and every `httpx` call — see the global
ROB-816 constraint. This section is a manual, staged operator playbook
against a real Telegram bot and a real (or KIS/Kiwoom **mock**) broker
account. Never point this at a live-money account you are not prepared to
place a real (small) order through.

### Post-merge operator smoke checklist — not performed now

These are intentionally unchecked. Run them only after the PR is merged and
deployed, with the operator present and all normal live-order gates satisfied.
No test, documentation command, or pre-merge verification should perform either
live action.

- [ ] **DOT residual loss-cut canary:** approve the residual KRW-DOT stop-loss
  proposal and verify the Upbit accepted-only ledger/correlation record, then
  cancel or reconcile as planned.
- [ ] **Toss KR loss-cut canary during market hours:** create a fresh <=72h
  retrospective and dedicated completed Paperclip issue, then approve a
  minimal KR limit-sell proposal. Verify tick normalization, the supplied
  `approval_hash`/rung handoff, exact preview `clientOrderId`, accepted-only
  Toss audit fields, and no KIS submission. Acceptance alone must not be
  reported as filled. Prove either the enabled fill-poller cadence or run
  targeted `toss_reconcile_orders(order_id=<broker-order-id>, dry_run=False)`;
  verify the proposal rung reaches partial/filled/cancelled from broker evidence.

### Preflight

1. Confirm both env gates are set in the target process's environment:
   `ORDER_PROPOSALS_ENABLED=true` and `ORDER_PROPOSALS_TELEGRAM_ENABLED=true`.
2. For a loss-cut canary, confirm `ORDER_PROPOSALS_SUBMIT_AGENT_ID` is
   explicitly non-blank and its exact trimmed value appears in
   `LOSS_CUT_ALLOWED_AGENT_IDS`. Do not rely on an implicit or hardcoded
   identity; missing/whitespace must remain fail-closed.
3. Confirm `TELEGRAM_TOKEN` (the actual bot token — not
   `ORDER_PROPOSALS_TELEGRAM_BOT_TOKEN`, which is unused), `TELEGRAM_CHAT_IDS_STR`,
   `ORDER_PROPOSALS_TELEGRAM_TOKEN`, and
   `ORDER_PROPOSALS_TELEGRAM_CHAT_ALLOWLIST_STR` are all set (see
   "Telegram Approval — Activation").
4. `curl .../getWebhookInfo` (see "Telegram Bot Setup" step 5) and confirm
   the registered `url` points at the target host and `last_error_message`
   is empty.
5. Restart the MCP process / FastAPI app so the new env values are loaded.

### Staged run

1. **Create a proposal** via `order_proposal_create` with a real (or mock)
   `account_mode` and a deliberately small quantity/notional — treat this
   like the first real order on a new integration, not a routine trade.
   **What to check:** the tool returns `{success: true, proposal_id, ...}`,
   and within a few seconds a Telegram message with `[✅ 승인]`/`[❌ 거부]`
   buttons arrives in the allowlisted chat, rendering symbol/market/side/
   order type/rungs/thesis/strategy/valid_until — and **no raw hash or nonce
   text visible anywhere in the message** (redaction check).
2. **Verify DB state matches the message** using the DB Verification
   queries above: `lifecycle_state='proposed'`, rung `state='pending_approval'`,
   `approval_nonce` set, `approval_nonce_used_at IS NULL`.
3. **Click Deny on a throwaway proposal first** (cheapest path to exercise
   the whole plumbing without touching a broker). **What to check:** the
   Telegram message updates to "❌ 거부됨", the rung's `state` becomes
   `rejected` in the DB, and a second click on the same (now-stale) buttons
   answers "이미 처리되었거나 유효하지 않은 요청입니다" and changes nothing
   (nonce-replay proof).
4. **Click Approve on a real small-quantity proposal** against a mock/paper
   account first (`kis_mock`, Binance Spot Demo, etc. per your broker
   preference — never start this staged run against a live account).
   **What to check:** the Telegram message updates with a per-rung result
   summary (e.g. "체결 대기(접수)" / "주문 유지(대기)"), the rung transitions
   through `revalidating → approved → submitting → acked|resting` in the DB,
   `broker_order_id` and `correlation_id` are populated, and (for a market
   order) `record_ack` fired / (for a limit order) `record_resting` fired —
   confirm via the DB Verification query, not just the Telegram text.
5. **Force a `NEEDS_RECONFIRM` cycle deliberately**: create a limit-order
   proposal, then before approving, move the market away from the limit
   price enough that a fresh preview would price it differently (or just
   wait through a volatile few minutes on a liquid symbol). Click Approve.
   **What to check:** a **new** Telegram message arrives with "재확인 필요"
   framing and an explicit before/after diff, the original message is
   edited to "⚠️ 재확인 필요...", the rung is `needs_reconfirm` in the DB,
   and a fresh `approval_nonce` was minted (different from the original).
6. **Only after 3–5 clean cycles on mock/paper**, repeat step 4 once against
   a real live account with the smallest possible size, with a human
   watching the whole way through — this is the actual bar for calling PR 2
   "live-verified," not just "code review passed."
7. **Confirm `UNVERIFIED` handling** by killing network connectivity (or
   otherwise forcing a submit-phase exception) mid-approve on a mock/paper
   proposal, if your test environment supports it. **What to check:** the
   rung lands in `unverified` (never a terminal state). Broker reconcile can
   later establish what happened in the broker ledger, but this branch does
   not automatically converge the proposal rung; that wiring is PR 3c / #1498.
   See Troubleshooting.

Record the `proposal_id` and DB query output for every step you run (see
"Evidence Template" below) so results are auditable, not just "it worked."

---

## Evidence Template

Paste this block (filled in) when reporting an issue with the Telegram
approval flow. Every field maps to a real, queryable column — pull the
values from the DB Verification queries above (`review.order_proposals` /
`review.order_proposal_rungs`), not from memory or the Telegram message
text alone.

```
proposal_id:            <review.order_proposals.proposal_id>
symbol / market:        <symbol> / <market>
rung_index:             <order_proposal_rungs.rung_index>

Timestamps:
  created_at:            <order_proposals.created_at>
  validated_at (rung):   <order_proposal_rungs.validated_at>
  approved_at:           <order_proposals.approved_at>

States:
  rung state:             <order_proposal_rungs.state>
  group lifecycle_state:  <order_proposals.lifecycle_state>

Telegram chat_id:        <redact last digits if sharing outside the team>

Broker evidence:
  broker_order_id:       <order_proposal_rungs.broker_order_id>
  correlation_id:        <order_proposal_rungs.correlation_id>
  approval_hash_digest:  <order_proposal_rungs.approval_hash_digest>
  idempotency_key:       <order_proposal_rungs.idempotency_key>

What happened (free text):
  <expected vs. actual behavior, exact Telegram button clicked, any
   error text shown by Telegram (e.g. "이미 처리되었거나 유효하지 않은
   요청입니다"), and whether this is reproducible>
```

---

## Troubleshooting

### MCP tools not visible

Confirm `ORDER_PROPOSALS_ENABLED=true` is set in the MCP process environment
(not just the shell you're running a script from), then restart:

```bash
uv run python -m app.mcp_server.main
```

Check both the default profile (`registry.py`) and, if using the 8770
TradingCodex execution profile, that `ORDER_PROPOSAL_TOOL_NAMES` appears in
`tradingcodex_execution_registration.py`'s allowlist.

### `order_proposal_create` returns `{success: false, error: ...}`

The error string comes from either a `ValueError` (bad decimal in a rung
quantity/price/notional, malformed `valid_until` ISO timestamp, malformed
`supersedes_proposal_id` UUID) or an `OrderProposalError` subclass raised by
the service (e.g. an invalid rung transition, unknown market/account_mode/side
value rejected by the DB `CheckConstraint`). Fix the input and retry — no
partial row is left behind (the session is not committed on the exception
path).

### `OrderProposalInvalidStateTransition`

Raised by `assert_rung_transition` when a rung is asked to move to a state
not in its allowed-next set (see the state diagram above). This should never
surface from the PR 1 read/create MCP tools (`order_proposal_create`/`get`/
`list` never drive a transition). It is exercised directly by
`tests/services/order_proposals/test_state_machine.py`, and in normal
operation by the PR 2 Telegram approval flow's internal callers
(`app/services/order_proposals/telegram_callback.py`,
`app/services/order_proposals/revalidation.py`) via the service's
`transition_rung`. If you see it surface from a live Telegram click, it
means the click-time code attempted a transition the state machine
considers illegal from the rung's current state — treat it as a bug report,
not routine operator error.

### A proposal I expect to be `expired` at the group level shows `terminal`

Expected — see the Known Item in Lifecycle States above. Check rung-level
`state = 'expired'` directly; the group rollup does not (yet) special-case
an all-expired rung set.

### Migration state

```bash
uv run alembic current
uv run alembic heads
```

The ROB-816 migration (`20260710_rob816_order_proposals`) is additive-only
(two new tables, no existing table changes) and chains off
`20260710_rob800_exit_intent`. `downgrade()` drops indexes then tables in
reverse order and is safe in non-production environments.

### `403 Telegram callback token not configured`

`AuthMiddleware` returns this when `ORDER_PROPOSALS_TELEGRAM_TOKEN` is unset
or empty on the process serving `/trading/api/telegram/*` — the webhook
secret must be set fail-closed rather than accepted with a blank/missing
value. Set `ORDER_PROPOSALS_TELEGRAM_TOKEN` (see "Telegram Approval —
Activation") and restart. (The middleware also returns this same 403 shape,
with a different detail string, if `ORDER_PROPOSALS_TELEGRAM_TOKEN_HEADER`
is set to an empty string — don't blank that setting out.)

### `401 Invalid Telegram callback token`

`AuthMiddleware` returns this when the value in the
`X-Telegram-Bot-Api-Secret-Token` header (or whatever
`ORDER_PROPOSALS_TELEGRAM_TOKEN_HEADER` is set to) does not match
`ORDER_PROPOSALS_TELEGRAM_TOKEN` (compared via `hmac.compare_digest`).
Usually means the webhook was registered with a different secret than the
one currently configured — re-run `setWebhook` with the current
`ORDER_PROPOSALS_TELEGRAM_TOKEN` value (see "Telegram Bot Setup" step 4), or
check `getWebhookInfo` for a stale registration.

### `chat_not_allowed`

`handle_callback_update` returned `{"handled": false, "reason": "chat_not_allowed"}`
because the callback's `chat.id` is not in
`settings.order_proposals_telegram_chat_allowlist`. Add the chat ID to
`ORDER_PROPOSALS_TELEGRAM_CHAT_ALLOWLIST_STR` (see "Telegram Bot Setup" step
2 for how to find it) and restart. This is an authz failure, not an authn
failure — the webhook secret token check already passed by the time this
fires.

### `nonce_mismatch`

`consume_approval_nonce` raised `OrderProposalError("nonce_mismatch")` — the
`nonce` embedded in the clicked button's callback data does not equal the
row's *current* `order_proposals.approval_nonce` value. Expected and correct
when clicking a button on a Telegram message that has since been superseded
by a fresh message minting a new nonce — either a `NEEDS_RECONFIRM` resend
(`telegram_callback.py`'s reconfirm branch) or a brand-new
`send_proposal_for_approval` dispatch (`dispatch.py`) for the same proposal.
The old message's buttons are stale by design; approve/deny only the most
recent message for a given proposal.

### `nonce_replay`

`consume_approval_nonce` raised `OrderProposalError("nonce_replay")` — the
clicked nonce *does* match `order_proposals.approval_nonce`, but
`approval_nonce_used_at` is already set (that nonce was already consumed).
Expected and correct in two cases: (1) a genuine double-tap/duplicate
Telegram update delivery for the same click, (2) clicking an already-
approved/denied proposal's message again (approve/deny does not mint a new
nonce on that message, so its buttons remain clickable but inert). If
neither explains it, check whether something outside the Telegram flow
called `set_approval_nonce`/`consume_approval_nonce` directly (should never
happen outside `dispatch.py`/`telegram_callback.py`).

### `NEEDS_RECONFIRM` loop (a rung keeps coming back needs_reconfirm)

Each `NEEDS_RECONFIRM` cycle mints a fresh nonce and re-sends the message —
a rung that needs reconfirmation on every approve attempt usually means the
symbol is fast-moving or thin/illiquid enough that the price at click time
never matches the price at the *original* proposal-create time (or the
previous reconfirm's numbers). This is the guard chain working as intended,
not a bug. Options: widen the operator's price tolerance before creating the
next proposal revision, act manually through the normal order tools instead
of via a proposal for that specific symbol, or accept the latest diff and
click Approve again promptly (before the price moves further).

### `UNVERIFIED` rung — never auto-voided

A rung in `unverified` state means `revalidate_and_submit` could not
classify the broker's response after a real submit attempt (network
exception, ambiguous status, missing `broker_order_id` — see
`revalidation.py`'s `_classify_submit`). By design this is a **holding**
state, not terminal, and nothing in this feature auto-voids it. To investigate,
run the existing broker-evidence reconcile tools —
`kis_live_reconcile_orders` (KR) or `live_reconcile_orders` (US/crypto,
see `docs/runbooks/kis-live-order-reconcile.md` /
`docs/runbooks/live-order-reconcile.md`) — against the rung's
`correlation_id`/`broker_order_id` (from the DB Verification query or the
Evidence Template above) to pull real order-status evidence and determine
whether it actually got submitted, filled, or rejected. For Toss use targeted
`toss_reconcile_orders(order_id=<broker-order-id>, dry_run=False)`; it projects
confirmed partial/fill/cancel evidence through `record_fill_evidence` and
retries terminal-row projection drift. Pending/unknown evidence never infers a
terminal proposal state.
