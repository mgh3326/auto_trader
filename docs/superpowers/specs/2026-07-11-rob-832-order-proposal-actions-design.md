# ROB-832 Order Proposal Replace/Cancel Design

## Goal

Extend `order_proposals` with `replace` and `cancel` actions while preserving
the existing `place` behavior. Every live mutation remains gated by a
Telegram button click and click-time broker evidence. A replacement must
never submit its new order until cancellation of the target order is
confirmed by the broker.

The operational-policy recipe in section B of ROB-832 is out of scope.

## Compatibility and Persistence

Add two columns to `review.order_proposals` in an additive Alembic migration:

- `action`: nullable text constrained to `place`, `replace`, or `cancel`.
  `NULL` and `place` both mean the existing place behavior. New callers
  default to `place`; historical rows require no backfill.
- `target_broker_order_id`: nullable text. It is required for `replace` and
  `cancel` and forbidden for `place`.

The ORM exposes the same two nullable fields. The immutable payload hash adds
the normalized action, target broker order ID, and canonical target snapshot,
so approval is bound to the intended mutation and broker order.

The existing `source_asof` JSON stores a canonical creation-time target
snapshot without requiring more schema:

```json
{
  "target_order_snapshot": {
    "broker_order_id": "...",
    "symbol": "KRW-AVAX",
    "side": "sell",
    "order_type": "limit",
    "limit_price": "42000",
    "remaining_quantity": "3.5",
    "status": "open",
    "observed_at": "2026-07-11T17:23:00+09:00"
  }
}
```

Numeric values use the same canonical fixed-point normalization as proposal
rungs. Session attribution is deliberately absent: manually created broker
orders and proposal-created orders are treated identically once the broker
returns matching evidence.

## Creation Contract

`OrderProposalsService.create_proposal` validates action structure before any
row is inserted:

- `place`: target ID is absent; one or more rungs remain allowed.
- `replace`: target ID and target snapshot are required; exactly one rung is
  required; the rung is the proposed new order specification.
- `cancel`: target ID and target snapshot are required; exactly one rung is
  required; the rung must exactly equal the snapshot's side, remaining
  quantity, and limit price. It represents the order the user believes will
  be cancelled, not advisory metadata.

The MCP create boundary performs a read-only broker lookup for replace/cancel,
normalizes the open order, and passes that snapshot into the service. Creation
fails closed when the target is missing, not open, has no positive remaining
quantity, or conflicts with the requested symbol/side. Creation never mutates
the broker.

Supported combinations are explicit per action:

- `place`: retain the existing `kis_live × equity_kr`,
  `kis_live × equity_us`, and `upbit × crypto` combinations.
- `replace` and `cancel`: support the same three combinations only where the
  normalized broker gateway provides target lookup, cancellation, and
  post-cancel confirmation. Any unsupported `account_mode × market × action`
  tuple is rejected at creation.

Multiple proposals for the same symbol, account, or target are not globally
blocked. Ladder repricing is intentionally modeled as one proposal and one
Telegram approval message per independent broker order.

## Broker Evidence Gateway

Add a focused proposal-internal gateway that adapts existing broker read and
cancel paths into these operations:

```python
async def fetch_target_order(...) -> BrokerOrderSnapshot
async def cancel_target_order(...) -> CancelAttempt
async def confirm_target_cancelled(...) -> BrokerOrderSnapshot
```

`BrokerOrderSnapshot` has a stable contract across Upbit and KIS: broker order
ID, symbol, side, order type, original price, remaining quantity, normalized
status, and observation time. The gateway reuses existing Upbit/KIS services
and cancellation implementations but does not call the composite
`modify_order_impl` path. Upbit's existing cancel-and-new helper cannot prove
at the proposal layer that cancellation was confirmed before new submission,
so replace is composed from explicit read, cancel, confirm, and place steps.

Tests inject every broker function. No test reaches a real broker, network, or
account.

## Telegram Rendering

The initial approval message identifies the action and target broker order.
It reuses the existing before/after diff renderer:

- `replace`: before is the canonical target snapshot; after is the single new
  rung.
- `cancel`: before is the canonical target snapshot; after is a zero-remaining
  cancelled state, rendered explicitly as cancellation while retaining the
  same diff section.
- `place`: existing rendering is unchanged.

The callback data, nonce replay guard, chat allowlist, commit lease, and
best-effort Telegram notification behavior remain unchanged.

## Click-Time Replace Flow

The replace action executes one rung in this strict order:

1. Consume the approval nonce and acquire the existing commit lease.
2. Fetch the target order from the broker.
3. Require the target to be open with positive remaining quantity and require
   broker order ID, symbol, side, order type, price, and remaining quantity to
   match the approved creation snapshot. A partial fill or any mismatch stops
   before cancellation.
4. Run the existing fresh place-order dry-run for the replacement rung. This
   refreshes quote-dependent normalization and reruns the complete placement
   guard chain, including the sell profit floor.
5. If normalized replacement price or quantity differs from the approved
   rung, use the existing `NEEDS_RECONFIRM` outcome and diff message. Do not
   cancel the target.
6. Request cancellation through the target broker's existing cancellation
   path.
7. Independently fetch broker evidence again. Only a confirmed non-open,
   cancelled target with no remaining executable quantity permits the flow to
   continue. A successful cancel response alone is insufficient.
8. Submit the replacement through the existing place-order path using the
   fresh approval hash from step 4.
9. Record lineage: the group retains the original
   `target_broker_order_id`; the rung records the newly accepted
   `broker_order_id`, correlation ID, idempotency key, and approval digest.

Cancellation failure, lookup failure, timeout, contradictory evidence, or
unconfirmed cancellation records a fail-closed outcome and never invokes the
new-order submit function. A submit exception after confirmed cancellation is
still ambiguous and uses the existing `unverified` semantics; it must not
pretend the original order still exists or mark a fill.

## Click-Time Cancel Flow

The cancel action executes one rung as follows:

1. Consume the nonce and acquire the existing commit lease.
2. Fetch and compare fresh broker evidence against every approved snapshot
   field, including price and remaining quantity.
3. On any mismatch, missing order, non-open state, or non-positive remaining
   quantity, stop before mutation and report rejection.
4. Request cancellation.
5. Fetch broker evidence independently and require confirmed cancellation.
6. Record the rung as `cancelled`, with the target broker order ID retained for
   audit.

The cancel action never calls preview/place submission and never creates a
new broker order.

## State and Failure Semantics

The existing rung state machine remains the single lifecycle authority.
Replace follows the existing revalidation and submission states; cancel uses
the same pre-submit states and terminates in the existing `cancelled` state.
Only legal transitions are added if the current graph does not already allow
them.

Failures before a broker mutation return the rung to a retryable pre-submit
state with a bounded operator-visible reason. Ambiguous broker mutation
outcomes use `unverified`; they are never auto-voided. Accepted replacement
orders remain `acked` or `resting`, never `filled`, until later reconcile
evidence arrives.

The callback transaction is committed before Telegram edits exactly as in the
existing flow. Telegram delivery failure cannot roll back recorded broker
evidence.

## Testing

Implementation follows RED-GREEN-REFACTOR. Tests use fake sessions where
appropriate and inject/mock every broker and Telegram dependency.

Required coverage:

- additive migration upgrade/downgrade and ORM smoke checks;
- payload-hash binding for action, target ID, and target snapshot;
- place backward compatibility and multi-rung preservation;
- replace/cancel exact-one-rung validation;
- supported and unsupported create combinations;
- manual/unattributed target order creation;
- cancel snapshot mismatch on price or remaining quantity;
- initial replace/cancel before/after Telegram rendering;
- replace call ordering and lineage recording;
- no new submission after cancel failure or absent/ambiguous confirmation;
- partial-fill/remaining-quantity drift rejection before cancellation;
- fresh quote normalization and profit-floor guard before cancellation;
- cancel confirmation with no place/submit call;
- nonce replay, lease, commit-before-notify, and existing place regression
  coverage.

Final verification is the focused proposal test suite, relevant MCP order
tests, the repository's broader test gate as proportionate to runtime, and a
clean `make lint`. No live or `--run-live` test is permitted.

## Documentation and PR Handoff

Update `app/mcp_server/README.md` and `docs/runbooks/order-proposals.md` with
the action contract, supported combinations, evidence requirements, lineage,
and operator recovery guidance.

The PR body must include a no-live-order operator smoke checklist covering:

- migration inspection and feature-flag state;
- create-only replace/cancel proposals against known open test targets;
- Telegram before/after and target-ID inspection;
- mocked/stubbed cancellation failure proving no replacement submit;
- broker evidence verification steps for a future separately authorized live
  smoke;
- rollback/reconcile guidance for `unverified` outcomes.

The PR is opened but not merged, and its number is reported to the user.
