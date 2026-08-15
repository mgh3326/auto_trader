# Order proposal approval records (ROB-1255)

This runbook defines the persistence contract that an approval-history reader
(including a later apphub design) may rely on. It does not add a UI field,
change an approval rule, or authorize broker execution.

## Storage surfaces

`review.order_proposal_approval_audit_events` is the append-only forensic
ledger. Application code can only insert through
`OrderProposalsService.append_approval_audit_event_best_effort`; the database
rejects `UPDATE`, `DELETE`, and `TRUNCATE`. The repository exposes no mutation
method for existing rows.

`review.order_proposal_approval_dispatch_attempts` remains the normalized
dispatch-history collection. Every card publication receives a distinct
`attempt_id`. `OrderProposalsService.list_approval_dispatch_history` returns all
attempts in `(attempted_at, id)` order across the proposal's root lineage. An
attempt itself moves from `pending` to its final publication state, but a later
attempt never replaces or deletes the earlier row.

The existing `order_proposals.approval_dispatch_*` columns remain the compatible
latest-value projection. They intentionally still point at the newest attempt.
Readers that need history use the ordered attempt collection; existing readers
can continue using the single-value columns unchanged.

## Audit fields

| Field | Meaning |
| --- | --- |
| `event_id` | Unique identity for one immutable fact. |
| `event_type` | `first_stage_approved`, `second_stage_dispatched`, `second_stage_clicked`, `expired`, or `superseded`. A click fact is not an approval decision. |
| `event_result` | Observed result, such as `accepted`, `sent_current`, or `expired`; it is evidence, never an authorization input. |
| `proposal_id` / `root_proposal_id` | The card that produced the fact and its stable supersession lineage. Querying the root preserves facts from older cards. |
| `rung_indices` | Exact rung indexes covered by the card-level fact. An empty array means no rung was available to bind. |
| `actor_kind` / `actor_id` | Telegram/web user identity for click facts. System events have `actor_id = NULL`; this does not assert that anyone approved. |
| `channel` | Channel on which the fact occurred: `telegram`, `web`, or `system`. |
| `nonce_digest` | SHA-256 fingerprint used only to correlate events. A raw nonce is never stored in this table. |
| `nonce_consumed` | Whether this event itself successfully consumed the single-use nonce. Dispatch, expiry, and supersede events are `false`. |
| `nonce_invalidated` | Whether expiry or supersede made an otherwise present nonce unusable without calling that action an approval. |
| `dispatch_attempt_id` | Link to the exact dispatch-history row when a card binding exists. |
| `card_chat_id` / `card_message_id` / `card_kind` | Best available physical card identity. Telegram stage two may edit the same message ID while using a new dispatch attempt. |
| `predecessor_proposal_id` / `successor_proposal_id` | Explicit lineage edge for preserved supersession facts. No approval state is inherited. |
| `reason_code` / `details` | Typed context for a failure, lazy expiry observation, or lineage event. |
| `occurred_at` | Time defined by `timing_source`, below. |
| `observed_at` | Time the server finished observing enough evidence to append the fact. |
| `created_at` | Database insert time; it is not presented as click time. |

## Timing semantics

The code never labels database commit time as a human click time.

- `telegram_callback_received`: `occurred_at` is the server timestamp supplied
  when webhook handling began. Telegram callback payloads contain no trusted
  user-device click timestamp. `observed_at` is the later in-handler sample
  supplied to the nonce-consumption check; the row is appended only after that
  consumption succeeds. A `second_stage_clicked` fact is appended at this nonce
  boundary, before later lease or submission outcomes, and therefore does not
  claim that the proposal was approved.
- `telegram_dispatch_started`: `occurred_at` is the approval-window sample that
  authorized the durable dispatch attempt before channel publication. The event
  is appended only after the publication result returns; that actual completion
  observation is `observed_at`, and `event_result` records the result.
- `approval_deadline`: `occurred_at` is the bound 90-second confirmation
  deadline. Because no scheduler is added here, `observed_at` is when a later
  callback first proves that the deadline passed.
- `proposal_deadline`: `occurred_at` is `valid_until`; `observed_at` is the
  callback or expiry sweep that materialized the expiry.
- `supersede_transaction`: both timestamps identify the transaction that linked
  the replacement and killed the old nonce.

## Failure and decision boundary

Audit insertion runs in a database savepoint. Any validation or database error
rolls back only that savepoint, logs the proposal ID, event type, and exception
class, and returns `None`. Raw nonces and actor secrets are not logged. The
approval, nonce, rung, broker, and Telegram result paths continue with their
pre-existing return values.

No approval decision reads the audit table or dispatch-history query. The
single-use nonce gate, 90-second confirmation rule, principal match, supersede
invalidation, and approval-window checks remain authoritative in their existing
code paths.

## Supersession example

If card A records `first_stage_approved` and card B supersedes it, card A keeps
that immutable event and receives a `superseded` event whose
`successor_proposal_id` is B. B remains unapproved with no nonce until its own
dispatch. A reader starting from B queries by `root_proposal_id` and can show
what happened on A without treating A's approval as B's approval.

For the observed JUP shape, a roughly 5,000-character first card and a
397-character stage-two edit are two ordered dispatch-attempt rows, even when
both reference the same Telegram message. The compatible single-value columns
show the 397-character latest attempt; the history still contains both rows.

## Migration boundary

The Alembic revision is shipped with the code but is not applied to a shared or
production database by this job. Migration acceptance tests may exercise it
only in uniquely named temporary databases that are dropped after the test.
Database rollout remains a separate operator action. Until rollout, audit
writes fail open and approval behavior remains available; operators should
monitor `order_proposals.approval_audit_record_failed` and apply the revision
before relying on completeness of the new history.
