# ROB-864 Loss-Cut Telegram Confirmation Design

## Goal

Replace the unavailable Paperclip issue-status signature for `loss_cut` order proposals with an explicit, per-order Telegram two-step confirmation while preserving every existing retrospective, caller, approval-hash, price, and slip-band guard.

## Decision

Use the existing proposal-level `approval_nonce` and `approval_nonce_used_at` fields for both single-use clicks, plus a server-side audit envelope in `OrderProposal.source_asof`. A new table would provide a normalized event log but would expand the migration and ROB-861 conflict surface. Reusing only the two nonce columns would lose the first click and could not bind the second click to every rung revision. The JSON audit envelope provides the required durable binding without a schema change.

The envelope records the proposal id, every eligible rung index and `approval_revision`, the second-step nonce, issue/expiry timestamps, and both click actors/timestamps/nonces. The second nonce expires after 90 seconds. Validation rejects a mismatched, replayed, expired, wrong-proposal, changed-rung-set, or changed-revision envelope before any broker mutation.

## Flow

1. Proposal creation remains non-mutating and dispatches the existing approval message.
2. For a normal proposal, `op` retains the existing one-click revalidate-and-submit flow.
3. For a `loss_cut` proposal, `op` consumes the initial nonce, runs a fresh preview to produce the confirmation evidence, records the first-click audit, mints a new nonce, and edits the message into a `⚠️ 손절 확인` prompt. It never submits.
4. The confirmation prompt shows symbol, rung quantity and limit, current price, current loss percentage versus average cost, retrospective id and a bounded lesson excerpt, and the configured slip-band floor.
5. The second callback action validates and consumes the bound nonce and envelope, records the second-click audit, then invokes the existing full click-time revalidation and submit path. Price/quantity drift still transitions to `needs_reconfirm`; guard failures remain fail-closed.
6. A reconfirmation caused by price/quantity normalization returns to the ordinary proposal approval message. A subsequent first click starts a new loss-cut two-step cycle bound to the incremented rung revision.

## Validation Boundary

`_validate_loss_cut_preconditions` gains an explicit proposal-flow signal. Proposal previews/submits preserve caller allowlisting, sell/limit/live-only checks, retrospective existence/symbol/trigger/72-hour checks, and slip-band construction, but do not require or query `approval_issue_id`. The field becomes optional free-text audit metadata and stays in payload hashes and ledgers when supplied.

Direct `place_order`, direct Toss preview/place, and `defensive_trim` cannot obtain the Telegram signature. They fail closed with an error directing callers to `order_proposal_create`; they never query Paperclip. `_fetch_approval_issue_status` therefore has no remaining runtime caller and is removed. Non-loss-cut proposals and ordinary orders are unchanged.

## Testing

Tests prove first-click no-submit and confirmation issuance, second-click full revalidation and submit, replay/TTL/rung/revision rejection, second-click stale retrospective and slip-band failures, optional `approval_issue_id`, Paperclip-free proposal E2E through reconcile, unchanged normal one-click proposals, and explicit direct-path rejection. Broker and Telegram calls remain mocked.

## Documentation and Compatibility

Update the proposal runbook, MCP README, and tool descriptions to describe Telegram two-step confirmation and mark ROB-858's Paperclip decision as superseded by ROB-864. No database migration is required. The branch follows the newer repository convention from `docs: remove obsolete Paperclip commit trailer`, so commits do not add the obsolete Paperclip co-author trailer.
