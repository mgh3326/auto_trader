# ROB-870 Telegram Batch Approval Design

## Decision

Proceed with a durable, additive Telegram batch-approval surface for manual
order proposals. The feature does not replace ROB-871 auto-approval or the
existing individual approval buttons. It reduces repeated clicks for proposals
that still require a human decision.

The batch owns only grouping metadata and its single-use trigger nonce. Every
member proposal keeps and consumes its existing approval nonce, records its
existing approval audit, and runs the existing `revalidate_and_submit` path.

## Scope Revalidation

The 2026-07-13 US-session production database is canonical:

| Population | Groups | Rungs |
|---|---:|---:|
| Raw proposals in the session window | 19 | 20 |
| ROB-869 superseded proposals excluded | 3 | 3 |
| Valid population | 16 | 17 |
| Counterfactual ROB-871 auto candidates | 3 | 3 |
| Manual remainder | 13 | 14 |

The manual remainder has this exclusive primary-reason breakdown:

| Primary reason | Groups | Rungs | Long-term interpretation |
|---|---:|---:|---|
| US per-order cap above $150 | 9 | 9 | Canary seed; this share can shrink when the cap rises |
| Distance below 3% | 3 | 3 | Structural immediate-execution-class manual work |
| Replace/cancel action | 0 | 0 | Structural policy exclusion, absent from this session |
| Multi-rung all-or-human fallback | 1 | 2 | Structural until atomic ladder auto-submit exists |
| **Total** | **13** | **14** | |

Even if the canary cap is raised enough to remove every cap-only case, this
session leaves four groups and five rungs of structural manual work. During an
intentional ROB-871 gate-off or rollback period, all valid proposals use the
manual path. The feature's product justification is therefore the structural
manual path and operational fallback, not the temporary $150 cap.

The Linear ROB-870 description is updated with these corrected counts and the
same breakdown.

## Alternatives Considered

### 1. Durable batch and membership tables — selected

Persist batch lifecycle, nonce, TTL, summary message identity, and immutable
membership in relational rows. Row locks and unique constraints make concurrent
dispatch and replay behavior explicit and testable.

Cost: one additive migration plus repository/service code.

### 2. Anchor proposal `source_asof` envelope

Store the batch on the first proposal and append member UUIDs to JSONB. This
avoids a migration, but makes concurrency, membership constraints, querying,
and operator audit depend on a mutable metadata blob.

Rejected because the batch is a security-sensitive approval capability, not
incidental display metadata.

### 3. Stateless click-time time-window query

Encode a time window in the callback and discover members when clicked.

Rejected because the set displayed to the operator could differ from the set
approved after late arrivals or state changes. A batch approval must have a
durable displayed membership envelope.

## Persistence Model

Add two tables under the `review` schema.

### `order_proposal_approval_batches`

- `id`: bigint primary key.
- `batch_id`: UUID, unique public identity used by callback prefix resolution.
- `chat_id`: destination Telegram chat as text.
- `window_started_at`: first eligible member dispatch time.
- `window_closes_at`: fixed collection boundary, ten minutes after the first
  member. New members never extend this boundary.
- `expires_at`: click deadline, extended to ten minutes after the latest member
  but bounded by the earliest non-null member `valid_until`.
- `approval_nonce`: URL-safe token dedicated to the batch trigger.
- `approval_nonce_used_at`: null until atomically consumed.
- `approved_by_telegram_user_id`, `approved_at`: batch trigger audit.
- `summary_message_id`: Telegram summary message identity, nullable until the
  second member makes a summary useful and delivery succeeds.
- `summary_dispatch_state`, `summary_dispatch_lease_until`: a short delivery
  claim preventing concurrent member dispatches from sending duplicate summary
  messages. A failed or expired claim is retryable by a later member.
- `created_at`, `updated_at`.

An open-batch lookup is serialized by a transaction-scoped advisory lock over
`chat_id`. A new eligible proposal joins the newest batch whose collection
window is still open and whose batch nonce is unused. Otherwise dispatch creates
a new batch.

### `order_proposal_approval_batch_members`

- `id`: bigint primary key.
- `batch_pk`: foreign key to the batch with cascade delete.
- `proposal_pk`: foreign key to `review.order_proposals` with restrict delete.
- `approval_nonce_snapshot`: the proposal approval nonce displayed by its
  individual message and later consumed by batch execution.
- `approval_message_id`: the individual Telegram message to edit with results.
- `result`, `result_detail`, `processed_at`: batch-observation metadata written
  after each independent proposal transaction. Proposal/rung state remains the
  trading source of truth.
- `added_at`.
- Unique constraints on `(batch_pk, proposal_pk)` and
  `(proposal_pk, approval_nonce_snapshot)`. A proposal with a newly issued
  reconfirmation nonce may join a later batch, but the same displayed approval
  capability cannot be registered twice.

The membership snapshot binds the batch to exactly the same individual approval
capabilities shown to the operator. Re-dispatch with a fresh individual nonce
cannot silently inherit an old batch membership.

## Eligibility and Grouping

`send_proposal_for_approval` remains responsible for sending the individual
message and minting its proposal nonce. After a successful individual send, it
offers the proposal to the batch coordinator.

A proposal may be registered only when all conditions hold:

- destination `chat_id` matches the batch;
- at least one rung is `pending_approval`;
- `proposal_approval_block_reason` returns no superseded or terminal reason;
- `exit_intent != "loss_cut"`;
- `source_asof.auto_approved` is absent;
- its individual approval nonce is present and unused.

These checks run again at click time. Registration-time checks prevent bad
summaries; click-time checks fail closed against later supersede, terminal, or
individual-button activity.

ROB-871-eligible proposals do not reach manual dispatch while its master gate is
enabled. When the gate is intentionally off, manual proposals may batch because
that is the operational fallback this feature is meant to support. A proposal
already carrying `source_asof.auto_approved` is never batch eligible.

The collection window is ten minutes from the first eligible member. The batch
summary is sent when the second member joins and edited for later additions.
Single-member batches remain internal and expose no redundant batch button.

## Telegram Surface

`approval_message.py` gains pure builders/parsers for batch callbacks and
summaries. Batch callback data uses a distinct `ba` action, a short batch UUID
prefix, and the batch nonce while staying below Telegram's 64-byte limit.

The pending summary contains:

- symbol, side, rung prices, and notional for every member;
- total notional;
- subtotals by `account_mode` and broker account identity when available;
- expiry time;
- one `전체 승인` button.

Account identifiers follow existing redaction rules; the summary must not expose
credentials, raw payload hashes, or unrestricted broker identifiers.

Individual messages and their approve/deny buttons are unchanged.

## Callback and Transaction Flow

`telegram_callback.py` adds a batch branch without changing the existing `op`,
`dn`, `lc`, or `vc` paths.

1. Authenticate chat and parse `ba` callback data.
2. Resolve the full batch UUID from its short prefix, failing closed on zero or
   multiple matches.
3. Lock and consume the batch nonce in one transaction. Validate TTL, chat,
   replay status, and minimum two-member membership. Record batch approver and
   approval time, snapshot ordered members, then commit before broker work.
4. Process members sequentially. Each member gets a fresh DB session and calls
   the existing single-proposal approval helper with its snapshotted individual
   nonce and individual message ID.
5. The helper consumes the proposal nonce, applies ROB-869 lifecycle guards,
   records the existing proposal approval audit, obtains the existing commit
   lease, and invokes the existing `revalidate_and_submit` function.
6. Any exception or rejected member is recorded as that member's result and the
   loop continues. No member transaction can roll back another member.
7. Existing individual messages are edited by the reused approval helper. A
   ROB-861 shortfall remains `needs_reconfirm`, receives the existing fresh
   proposal nonce/message, and is marked as such in the batch result.
8. After all members, edit the batch summary into a terminal result grouped by
   approved, needs-reconfirm, skipped/stale, and failed. Remove the batch button.

The batch nonce is only the trigger. It never substitutes for an individual
proposal nonce or approval audit.

## Failure and Race Semantics

- **Batch replay:** rejected by `approval_nonce_used_at` before member work.
- **Expired batch:** rejected before member work; summary button is removed when
  Telegram editing succeeds.
- **Individual approval wins race:** the member's snapshotted nonce is already
  used, so that member is skipped while later members continue.
- **Supersede/terminal wins race:** the existing ROB-869 approval guard rejects
  that member; later members continue.
- **Loss-cut appears through malformed data:** click-time eligibility excludes
  it, preserving the two-step flow.
- **ROB-861 insufficient buying power:** only affected rungs become
  `needs_reconfirm`; other members still execute.
- **Telegram edit/send failure:** DB broker outcomes remain committed first;
  notifications are best effort and cannot rewrite trading truth.
- **Concurrent dispatch:** advisory locking and membership uniqueness prevent a
  proposal from appearing in two open batches.
- **Process crash after batch nonce commit:** replay remains blocked. Completed
  members preserve their own commits; untouched members keep their individual
  buttons and can still be approved separately. The batch summary may be stale,
  but no ambiguous broker retry occurs.

## Components and File Boundaries

- `app/models/order_proposals.py`: additive batch and membership ORM models.
- `alembic/versions/*_rob870_approval_batches.py`: additive schema migration.
- `app/services/order_proposals/repository.py`: locked batch lookup, creation,
  membership, prefix resolution, and nonce consumption primitives.
- `app/services/order_proposals/service.py`: batch invariants and audit methods.
- `app/services/order_proposals/approval_message.py`: pure summary and callback
  rendering/parsing.
- `app/services/order_proposals/dispatch.py`: post-individual-send batch
  registration and summary send/edit orchestration.
- `app/services/order_proposals/telegram_callback.py`: batch callback
  orchestration and reuse of single-proposal approval behavior.
- `docs/runbooks/order-proposals.md`: operator contract, TTL, exclusions, and
  fallback behavior.

ROB-868 websocket code and ROB-877 broker-gateway code are outside the change
surface.

## Test Design

Tests must cover:

- batch callback render/parse and Telegram's 64-byte bound;
- second-member summary creation and later summary edits;
- same-chat and ten-minute collection boundaries;
- batch nonce single use and TTL expiry;
- immutable membership nonce binding;
- partial member failure with later members still processed;
- per-member individual message result edits;
- loss-cut exclusion at registration and click time;
- superseded and terminal exclusion at registration and click time;
- auto-approved exclusion;
- individual approval path regression with identical behavior;
- mixed ROB-861 buying-power shortfall where only affected rungs remain
  `needs_reconfirm` and the summary reports them;
- concurrent registration does not duplicate membership;
- migration upgrade/downgrade smoke coverage where the repository convention
  requires it.

The required final gates are the focused order-proposal tests, the full relevant
suite, and `make lint`.

## Non-goals

- Changing ROB-871 policy thresholds or auto-approval eligibility.
- Atomic broker submission of a multi-rung ladder.
- Removing or weakening individual approval/deny buttons.
- Batching loss cuts.
- Changing broker gateway or websocket behavior.
