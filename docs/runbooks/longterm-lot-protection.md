# Long-term lot protection

## Status

This feature ships with protection arithmetic disabled by default. All three
scope modes default to off, and this source revision rejects every enforce
configuration at startup. The approval for implementation did not approve
enforcement. A later, separately approved
operator change must both remove that source-level refusal and select a
scope-specific rollout mode.

Toss enforce remains unavailable even after that future authorization until
Q13 has broker evidence that sellableQuantity already excludes pending sells.
The Toss verification setting defaults false and must never be used as a
substitute for that evidence.

This feature registers no scheduler or cron job. Since #943 it does change P
automatically, by rule only and behind a default-off kill switch: see
"Rule-executed P follow" below (operator decision, hk doc 8274 section 7).

Every live sell consults the protected-position table even while its scope
mode is off. Under the approved Q15 rule, a missing table or failed lookup
refuses the sell because the service cannot know which symbols are protected.
Off preserves existing sell sizing and disables protection arithmetic; it is
not a database-outage bypass. The operator desk must apply the migration after
a backup and before starting application code that contains this guard.

## What is protected

One current declaration is keyed by account scope, market, and canonical
symbol in review.protected_positions. The only valid live scopes are kis_live
for KR and US, toss_live for KR and US, and upbit_live for crypto. A release
sets protected_quantity to zero; it does not delete the head or its history.

Every service-layer declaration write records a matching append-only row in
review.protected_position_revisions in the same transaction. The revision
table rejects update, delete, and truncate at the database layer. Protection
never cancels a pending order or marks itself depleted. With the #943 kill
switch on, P follows the holding by rule (hk doc 8274 section 7): it is lowered
to the holding when the holding falls below it, and raised to the holding after
an authoritative buy fill on an already-declared name. With the switch off
(the default) P never changes without an operator write.

The tactical quantity is broker sellable minus protected quantity. Broker raw
sellable, total held, and locked values remain separately visible. A missing,
invalid, negative, or non-finite sellable value is not replaced with total
held. It is unverified instead.

## Send-time behavior

Protected live sells acquire a PostgreSQL session advisory lease keyed by
scope, market, and symbol; Upbit uses its base asset for the lock because
different quote markets draw from the same balance. Its declaration and live
ledger identities remain full market codes. The lease starts before the fresh
broker check and remains held through the broker response. A declaration write takes the same
key transactionally, so it cannot raise P between a successful guard and the
broker response. The declaration service invokes its broker H/S provider only
after acquiring that lock; a pre-lock H snapshot is rejected.

The guard reads the unprojected broker values immediately before sending. It
rejects an active protected sell when sellable is unobserved, the protection
database is unavailable, drift cannot be verified, H is below P, S is below
P, the quantity is unresolved, or the requested amount exceeds headroom.
Shadow records a would-block event but does not block a send. Off preserves
existing sizing behavior.

If one Upbit asset has active protection under another quote-market code,
enforce refuses its sell and shadow records a would-block event. This avoids
treating the same base-asset balance as unprotected through a second quote
market. The displayed tactical quantity for that alias is zero and unverified.

Upbit cancel and replace refreshes the original order remainder after taking
the asset lock, checks before cancellation, and checks again after a
successful cancellation. If the second check fails, the original order stays
cancelled and the replacement is explicitly withheld. The feature never
automatically recreates or cancels another order to repair this condition.

The advisory lease is not broker fencing. Manual broker orders, a second
deployment pointed at a different database, and a PostgreSQL restart can still
change broker state outside the lease. The next fresh read becomes encroached,
shortfall, or unverified and fails closed.

## Rule-executed P follow (#943)

Authority: operator decision 2026-09-29, hk doc 8274 section 7. P is a
computed value, not a hand-tuned number. After the first declaration, P
changes are rule-executed with a result notification only; there is no card
and no question. Until the H6 ledger exists the interim rule is P = the full
holding of each protected name. Monthly recompute from H6 NAV and target
weights, cap-overflow sells, cards, and #728 enforce are out of scope.

Kill switch: PROTECTED_POSITION_AUTO_FOLLOW_ENABLED (settings field
protected_position_auto_follow_enabled), default false. Only an exact true
arms it. While it is false the hooks and the lever read no head, read no
broker, write nothing, and send nothing. Desk decides when to turn it on
after deploy; turning it off again is the rollback.

What changes P, and how:

- Raise: an authoritative live buy fill, meaning an execution-ledger row with
  source reconciler and account_mode live, on a key that already has an active
  declaration (P above zero). P becomes the fresh broker holding. Revision
  reason auto:fill LEDGER_ID, idempotency key auto:fill:LEDGER_ID.
- Lower: whenever a fresh broker holding is below P, from any authoritative
  fill (buy or sell) or from the lever. P becomes the holding; a holding of
  zero is recorded as a release. Revision reason auto:reconcile, idempotency key
  auto:reconcile:POSITION_ID:rREVISION.
- Never: raise above the fresh holding (the service re-reads the broker inside
  the per-key lock and refuses P above held); create a declaration for an
  undeclared key; re-raise a released P of zero (P zero means released until an
  operator declares again); act on websocket rows (provisional) or
  manual_import rows; write from a read path or from the send-time guard.

Every change goes through ProtectedQuantityService.save, the same path as an
operator declaration: origin operator_cli, actor the fixed owner id
(MCP_USER_ID, the value the /invest route resolves as its owner context),
explicit confirmation, optimistic revision, and exact symbol confirmation. A
concurrent writer makes the save fail stale and the rule re-decides from the
new head, so two close fills end at the final holding with no extra revision.
Replaying the same ledger row or re-running the lever adds no revision.

Each revision sends exactly one message through the trade notifier
(notify_agent_message, the alerts channel), for example
"[protected P auto] kis_live kr 005930: P 1 -> 3 (raised, auto:fill 123,
revision 2, broker held 3)". There is no protected-position-specific notifier.
A notification failure is logged and does not undo the change.

Where the rule runs:

1. The execution-ledger reconciler, after its commit (TaskIQ
   execution_ledger.reconcile_execution_ledger_recurring and
   scripts/reconcile_execution_ledger.py --commit). It covers KIS and Upbit,
   including orders placed in the broker apps. Production facts reported by
   desk on 2026-09-29 (relayed by director-1): EXECUTION_LEDGER_RECONCILE_SCHEDULER_ENABLED=true
   and EXECUTION_LEDGER_COMMIT_ENABLED=true on at-scheduler and at-worker
   (.env.api), and that day's KIS fill landed through the reconciler at 09:30.
   If either flag is turned off, KIS and Upbit fills stop reaching this rule.
2. Toss reconcile booking (toss_reconcile_orders), after its session commits.
   It covers only Toss orders placed through auto_trader.
3. The manual lever, which lowers every active declared P that exceeds the fresh
   holding and never raises. Either the CLI
   protected_positions.py (--database-url URL | --database-url-env NAME) auto-reconcile [--scope SCOPE]
   [--commit] (a preview unless --commit; exit 2 while the kill switch is off,
   1 if any key failed) or the TaskIQ task protected_positions.auto_follow_reconcile,
   which has no schedule and commits when called. The code registers no
   schedule. Desk runs the one-shot CLI (auto-reconcile --commit) every 30 minutes during KR and US regular hours from an NCP systemd timer, per operator decision Q-75 on task 944 (option A).
   See "Lever timer unit" below; the timer must use --database-url-env.

Known gap, Toss app sales: a sale made in the Toss app never reaches the
execution ledger (there is no account-wide Toss fill reconciler), so no hook
sees it. P for a toss_live name stays above the holding until the lever runs.
While P is above the holding, the protection state for that name reads
shortfall. The ROB-866 Toss manual-activity sweep is not wired to this rule.

### Lever timer unit

Desk owns the NCP systemd service and timer; this repository ships no unit
and registers no schedule. Constraints the unit must follow:

- Name the database with --database-url-env NAME, never --database-url. A URL
  given with --database-url is in the process arguments, which any user on the
  host can read with ps every time the timer fires. With --database-url-env the
  CLI reads the URL from that one environment variable, supplied by the unit's
  EnvironmentFile (the variable may be DATABASE_URL itself). The CLI never
  prints the value; an error about it names only the variable (not set or
  empty, not a complete PostgreSQL URL, or an invalid variable name).
- The same environment must carry the application's broker credentials and
  settings, including PROTECTED_POSITION_AUTO_FOLLOW_ENABLED, because the lever
  reads fresh broker holdings and honours the kill switch.
- Command shape:
  protected_positions.py --database-url-env NAME auto-reconcile --commit
  with the working directory at the application root. Run it as a oneshot
  service; the timer limits it to KR and US regular hours.
- Output is one JSON line on stdout (per-key outcomes: lowered, unchanged,
  skipped, error). Only P decreases are written, one notification per decrease.

Exit codes (unchanged by the timer; alert on them in the unit's logs):

- 0: every declared key was reconciled or needed no change.
- 1: partial broker-read failure. At least one key's outcome is error (for
  example its broker read failed); the other keys were still processed. It
  also covers a database or unexpected failure before any key was read, which
  prints only {"error": "protected_positions_unavailable"}.
- 2: refused. The kill switch is off (error auto_follow_disabled, nothing was
  read or written; the unit reports failed until desk turns the switch on), or
  the request was invalid, for example a missing or malformed database variable
  (error invalid_request).

## Desk write CLI

scripts/protected_positions.py has reviewed write commands alongside the
read-only list, show, and history commands, to replace ad hoc declaration
scripts:

    protected_positions.py --database-url URL declare  SCOPE MARKET SYMBOL --quantity Q --reason R --confirm-symbol SYMBOL [--commit]
    protected_positions.py --database-url URL increase SCOPE MARKET SYMBOL --quantity Q --expected-revision N --reason R --confirm-symbol SYMBOL [--commit]
    protected_positions.py --database-url URL decrease SCOPE MARKET SYMBOL --quantity Q --expected-revision N --reason R --confirm-symbol SYMBOL [--commit]
    protected_positions.py --database-url URL release  SCOPE MARKET SYMBOL --expected-revision N --reason R --confirm-symbol SYMBOL [--commit]

Without --commit a write prints a preview (current P, requested P, fresh broker
held and sellable, resulting state) and writes nothing. With --commit it calls
ProtectedQuantityService.save with origin operator_cli, the fixed owner actor,
and the fresh broker observation provider that runs after the per-key lock.
--confirm-symbol must normalize to the same key. The command word must match
the result: declare only for a new key, increase above current P, decrease to
a positive quantity below current P, release to zero. --idempotency-key makes
a retry of the same request safe; otherwise one is generated and printed. The
database is only the one named explicitly, by --database-url URL or
--database-url-env NAME (the value is never printed); the broker read uses the
application's broker credentials. Exit codes: 0 ok, 1 broker or database
unavailable, 2 invalid or refused request, 3 stale revision or conflict.

## Operator migration and later rollout

1. Take a database backup and record its restore procedure before applying the
   additive migration.
2. Apply the migration at the operator desk, then verify that both review
   tables and the revision mutation triggers exist. This implementation task
   does not apply it to any production or standby database.
3. Deploy this code only after step 2: even off mode refuses all live sells if
   the protected-position table cannot be read. Keep every scope off while
   declarations are reviewed. Declaration writes require a broker observation
   collected inside the per-asset lock, an explicit confirmation, optimistic
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
declarations and revision evidence, verify the backup restore path, first
roll back or stop every service containing this guard, then perform the
approved downgrade at the operator desk. Even off mode reads the tables. A
downgrade discards the feature tables only after that evidence-handling
decision; it is not a way to bypass a live
sell block.

## Incident handling

For a protected sell block, inspect the read-only protected-position head,
fresh broker held and sellable evidence, and execution-ledger drift evidence.
Resolve a stale declaration through the approved operator UI or the desk
write CLI above; do not use MCP, direct SQL, a placeholder symbol, or an
order-ledger write. To stop rule-executed changes, set
PROTECTED_POSITION_AUTO_FOLLOW_ENABLED=false; revisions already written stay
as evidence. A broker or policy database outage stays fail-closed for live sells.
