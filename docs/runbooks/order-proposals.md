# Order Proposals Runbook (ROB-816)

## Purpose

`review.order_proposals` + `review.order_proposal_rungs` is the SOT (source-of-truth)
ledger for **proposed** orders awaiting human approval, prior to any broker
submission. It replaces ad-hoc "propose in chat, submit blind" flows with a
persisted, replayable record: one `order_proposals` row per proposal group
(symbol/side/market/account context + thesis/rationale), and one or more
`order_proposal_rungs` child rows (one per execution ladder rung — price/qty
pair) tracking each rung's own execution lifecycle independently.

This is **PR 1** of ROB-816: data model + pure state machine + service +
three read/create MCP tools. It intentionally ships with:

- No Telegram approval surface (PR 2).
- No submit/approve MCP tool — there is no path from a proposal row to a live
  broker order in this PR.

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
- **No submit without Telegram approval (PR 2).** There is deliberately no
  `order_proposal_approve` / `order_proposal_submit` MCP tool in this PR. The
  only way a proposal reaches a live broker order is the Telegram
  approve/deny flow shipped in PR 2 — not implemented yet.
- **Default OFF.** `ORDER_PROPOSALS_ENABLED=false` by default
  (`app/core/config.py`). When off, the three MCP tools are not registered
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

Setting this env var (or config override) surfaces the three MCP tools
(`order_proposal_create`, `order_proposal_get`, `order_proposal_list`) in:

- The default MCP profile (`app/mcp_server/tooling/registry.py`).
- The 8770 TradingCodex execution profile allowlist
  (`app/mcp_server/tooling/tradingcodex_execution_registration.py`).

With the flag off (default), none of the three tools are registered anywhere
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

Read + create only — **no approve/submit tool exists in this PR.**

### `order_proposal_create(symbol, market, account_mode, side, order_type, proposer, rungs, thesis=None, strategy=None, rationale=None, broker_account_id=None, lot_context=None, valid_until=None, supersedes_proposal_id=None)`

Persists a new proposal group + its rungs. `rungs` is a list of
`{"rung_index": int, "side": str, "quantity": str, "limit_price": str|None,
"notional": str|None}`. NOT a broker mutation. Returns
`{success, proposal_id, lifecycle_state, rungs}` on success or
`{success: false, error}` on validation failure.

If `supersedes_proposal_id` is given, the referenced proposal is marked
`superseded` and lineage (`root_proposal_id`, `supersedes_proposal_id`) is
linked on the new row.

### `order_proposal_get(proposal_id)`

Read-only fetch of a proposal group and its rungs by `proposal_id` (UUID).
Returns `{success: false, error: "not_found"}` if missing.

### `order_proposal_list(limit=50, symbol=None, lifecycle_state=None)`

Read-only list of recent proposal groups, optionally filtered by `symbol`
and/or group-level `lifecycle_state`. `limit` is clamped to `1..200`.

---

## DB Verification

```sql
-- A specific proposal group + its rungs
SELECT
  id, proposal_id, root_proposal_id, revision,
  supersedes_proposal_id, superseded_by_proposal_id, no_resubmit, void_reason,
  symbol, market, account_mode, side, order_type, proposer,
  lifecycle_state, valid_until, validated_at, created_at, updated_at
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

## Telegram Approval

Not implemented in this PR — see PR 2. Once shipped, proposal creation will
optionally (behind `ORDER_PROPOSALS_TELEGRAM_ENABLED`, default off) dispatch
an approval-table Telegram message with inline `[승인]`/`[거부]` buttons; a
token-authed webhook will receive the callback, re-validate, and submit via
the existing `approval_hash` place-order path. This runbook will be expanded
with bot setup, webhook registration, operator activation steps, and an
evidence template once PR 2 lands.

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
surface in PR 1 since there is no transition-driving MCP tool yet — it is
exercised directly by `tests/services/order_proposals/test_state_machine.py`
and the service's internal `transition_rung` (PR-2 callers only).

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
