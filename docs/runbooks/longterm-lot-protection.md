# Long-term lot protection

## Status

This feature ships inert. All three scope modes default to off, and this
source revision rejects every enforce configuration at startup. The approval
for implementation did not approve enforcement. A later, separately approved
operator change must both remove that source-level refusal and select a
scope-specific rollout mode.

Toss enforce remains unavailable even after that future authorization until
Q13 has broker evidence that sellableQuantity already excludes pending sells.
The Toss verification setting defaults false and must never be used as a
substitute for that evidence.

This feature registers no scheduler, task, cron job, or automatic remediation.

## What is protected

One current declaration is keyed by account scope, market, and canonical
symbol in review.protected_positions. The only valid live scopes are kis_live
for KR and US, toss_live for KR and US, and upbit_live for crypto. A release
sets protected_quantity to zero; it does not delete the head or its history.

Every service-layer declaration write records a matching append-only row in
review.protected_position_revisions in the same transaction. The revision
table rejects update, delete, and truncate at the database layer. Protection
never automatically decreases after a sell, cancels a pending order, or marks
itself depleted.

The tactical quantity is broker sellable minus protected quantity. Broker raw
sellable, total held, and locked values remain separately visible. A missing,
invalid, negative, or non-finite sellable value is not replaced with total
held. It is unverified instead.

## Send-time behavior

Protected live sells acquire a PostgreSQL session advisory lease keyed by
scope, market, and symbol. The lease starts before the fresh broker check and
remains held through the broker response. A declaration write takes the same
key transactionally, so it cannot raise P between a successful guard and the
broker response.

The guard reads the unprojected broker values immediately before sending. It
rejects an active protected sell when sellable is unobserved, the protection
database is unavailable, drift cannot be verified, H is below P, S is below
P, the quantity is unresolved, or the requested amount exceeds headroom.
Shadow records a would-block event but does not block a send. Off preserves
existing sizing behavior.

Upbit cancel and replace checks before cancellation and again after a
successful cancellation. If the second check fails, the original order stays
cancelled and the replacement is explicitly withheld. The feature never
automatically recreates or cancels another order to repair this condition.

The advisory lease is not broker fencing. Manual broker orders, a second
deployment pointed at a different database, and a PostgreSQL restart can still
change broker state outside the lease. The next fresh read becomes encroached,
shortfall, or unverified and fails closed.

## Operator rollout, after separate approval

1. Take a database backup and record its restore procedure before applying the
   additive migration.
2. Apply the migration at the operator desk, then verify that both review
   tables and the revision mutation triggers exist. This implementation task
   does not apply it to any production or standby database.
3. Keep every scope off while declarations are reviewed. Declaration writes
   require a fresh broker observation, an explicit confirmation, optimistic
   revision match, and exact symbol reconfirmation for decrease or release.
4. A later approved rollout may use shadow per scope for observation. Do not
   treat this implementation approval as authorization to make any scope
   enforce.
5. Before a future enforce change, retain the shadow evidence and verify the
   broker-specific assumptions. Toss requires Q13 evidence; KIS KR amendment
   remains conservative until Q19 evidence exists.

## 6.3 Rollback

Rollback is a controlled operator decision, not an automated response to a
guard failure. First return every scope to off and preserve the head and
revision evidence for investigation. Do not delete rows or mutate revision
history. If a schema downgrade is separately approved, export the affected
declarations and revision evidence, verify the backup restore path, perform
the approved downgrade at the operator desk, and verify no service remains
configured to read the removed tables. A downgrade discards the feature tables
only after that evidence-handling decision; it is not a way to bypass a live
sell block.

## Incident handling

For a protected sell block, inspect the read-only protected-position head,
fresh broker held and sellable evidence, and execution-ledger drift evidence.
Resolve a stale declaration through the approved operator UI in the follow-up
surface; do not use MCP, direct SQL, a placeholder symbol, or an order-ledger
write. A broker or policy database outage stays fail-closed for live sells.
