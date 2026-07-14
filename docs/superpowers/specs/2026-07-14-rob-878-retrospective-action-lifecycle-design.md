# ROB-878 Retrospective Action Lifecycle Design

## Goal

Turn retrospective `next_actions` from an embedded, read-only checklist into a
durable lifecycle with stable identity, atomic manual transitions, bounded
analysis consumption, and an evidence-safe path to later automation.

The first release preserves every existing action and keeps `/invest/insights`
read-only. It does not infer completion from free-form text, elapsed time,
Linear state, order submission, or a symbol match.

## Baseline and Root Cause

The 2026-07-14 production snapshot contained 53 retrospectives and 75 actions:

- 54 actions had status `open`, 19 omitted status, and 2 were `done`;
- therefore 73 were effectively active under the current reader;
- 18 had a due date, of which 8 were overdue and 10 were future-dated;
- 48 had no owner, and only one action had a Linear `issue_id`;
- 56 had neither an issue ID nor a due date;
- one retrospective with four actions had no `correlation_id`, so the public
  save path could not target it for an update.

These counts are a point-in-time diagnostic, not migration constants. The
migration must derive and verify its counts from the database it runs against.

The backlog is structural rather than a UI-only defect:

- `review.trade_retrospectives.next_actions` is one nullable JSONB array;
- an action has no stable ID, row version, actor, transition timestamp, reason,
  or evidence;
- omitted status is stripped from stored JSON and interpreted as active;
- action changes require replacing the whole array through the retrospective
  upsert, which can lose concurrent changes and cannot target rows without a
  correlation ID;
- active reads use `status != "done"`, so adding `obsolete` or `expired`
  without changing the reader would incorrectly keep them active;
- `/invest/insights` and stock detail are read-only displays, while
  `decision_history` does not consume actions at all;
- `issue_id` is opaque metadata. There is no Linear API client or status sync.

## Scope

This design includes:

1. a normalized canonical action ledger and lossless legacy backfill;
2. explicit active and terminal states;
3. a locked, versioned transition service;
4. an authenticated web mutation and a profile-gated MCP mutation;
5. manual evidence-based triage of the existing backlog;
6. bounded active-action injection into `decision_history`;
7. a read-only action queue in `/invest/insights`;
8. the boundary for a later typed, dry-run-first resolver follow-up.

The following are intentionally out of scope:

- application-runtime creation or update of Linear issues;
- using Linear issue status as completion evidence;
- NLP or string matching against action text;
- marking an action terminal solely because its due date passed;
- write controls in `/invest/insights`;
- scheduled automatic resolution in the initial release;
- reopening terminal actions or editing an accepted terminal audit record.

## Decision

Add `review.trade_retrospective_actions` as the canonical lifecycle store and
retain `TradeRetrospective.next_actions` temporarily as a compatibility
projection. All application action reads move to the child table. New saves
and action transitions update the child row and the compatibility projection
in the same transaction.

This is preferred over two alternatives:

- Enhancing only the JSONB array would still require embedded IDs, atomic
  element updates, optimistic locking, lifecycle audit fields, and a query/index
  rewrite. It would leave independently changing actions inside the parent
  aggregate.
- Adding only UI controls would continue to resubmit entire retrospectives,
  retain lost-update risk, and leave background consumers without a canonical
  state boundary.

The normalized boundary matches the repository's action-like child models,
while transition behavior follows the stronger order-proposal precedent:
central state rules, row locking, explicit evidence, and idempotent terminal
handling.

## Canonical Data Model

Add an ORM model `TradeRetrospectiveAction` backed by
`review.trade_retrospective_actions` with these columns:

| Column | Type | Contract |
| --- | --- | --- |
| `id` | UUID PK | Stable server-generated action identity |
| `retrospective_id` | bigint FK | References `review.trade_retrospectives.id`, `ON DELETE CASCADE` |
| `creation_key` | UUID nullable | Caller idempotency key required only for an intentional duplicate |
| `position` | integer | Display order within the parent; non-negative |
| `action` | text | Nonblank action text |
| `owner` | text nullable | Existing free-form owner metadata |
| `issue_id` | text nullable | Opaque external issue key; no sync semantics |
| `status` | text | Non-null, default `open`, checked state set |
| `due_kst_date` | date nullable | Calendar date in KST, not an expiry trigger |
| `version` | integer | Non-null optimistic version, initially `1` |
| `status_changed_at` | timestamptz | Time of the latest real state change |
| `resolved_at` | timestamptz nullable | Set once on entry to a terminal state |
| `status_actor` | varchar(128) | Server-derived operator or runtime identity |
| `status_source` | varchar(32) | `migration`, `retrospective_save`, `web`, `mcp`, `triage`, or `reconciler` |
| `status_reason` | text nullable | Bounded human-readable transition reason |
| `status_evidence` | JSONB nullable | Structured evidence envelope for the latest terminal transition |
| `legacy_payload` | JSONB | Non-null, default `{}`; exact source object preserving unknown legacy keys |
| `created_at` | timestamptz | Row creation time |
| `updated_at` | timestamptz | Latest metadata or lifecycle update |

Database constraints enforce:

- `status IN ('open', 'in_progress', 'done', 'obsolete', 'expired')`;
- `status_source IN ('migration', 'retrospective_save', 'web', 'mcp', 'triage', 'reconciler')`;
- `version >= 1` and `position >= 0`;
- terminal states have `resolved_at` and active states do not;
- `obsolete` and `expired` satisfy `status_reason IS NOT NULL`,
  `btrim(status_reason) <> ''`, and a 2,000-character maximum;
- `expired` satisfies `status_evidence IS NOT NULL` and
  `jsonb_typeof(status_evidence) = 'object'`; checks are written so PostgreSQL
  cannot pass a required field through SQL's null result.

Indexes are:

- deferrable, initially deferred uniqueness on `(retrospective_id, position)`,
  which permits atomic reordering but rejects duplicate final positions;
- partial uniqueness on `(retrospective_id, creation_key)` where the key is
  non-null;
- `(retrospective_id, position, id)` for parent hydration;
- partial `(due_kst_date, id)` for `status IN ('open', 'in_progress')`;
- partial `(issue_id)` where `issue_id IS NOT NULL`;
- `(status, updated_at, id)` for lifecycle operations and metrics.

`issue_id` is deliberately non-unique because historical actions can refer to
the same external issue. All reorder paths defer the position constraint until
commit and still use `id` as a deterministic query tie-breaker.

The transport `NextAction` shape gains `action_id`, `version`, lifecycle audit
fields, and the two new terminal statuses. `due_kst_date` becomes an actual
date at validation boundaries while remaining ISO `YYYY-MM-DD` in JSON. A
transport-only `force_new=false` flag plus required caller-generated
`creation_key` lets an ID-less caller intentionally create another action with
an otherwise identical tuple. The flag is not persisted; the creation key is
returned and projected so a lost-response retry remains idempotent.

The MVP stores the latest active-state transition metadata and one immutable
terminal resolution, not an append-only history of every `open`/`in_progress`
toggle. Full transition-event history requires a separate table and is not part
of ROB-878. “Durable audit” in this design means that the accepted terminal
resolution can never be overwritten.

The supporting singleton table
`review.trade_retrospective_action_control` has `id smallint PRIMARY KEY CHECK
(id = 1)`, checked `mode IN ('shadow', 'canonical')`, nullable `cutover_at`,
nullable `cutover_action_count`, and `updated_at`. The migration inserts exactly
`id=1, mode='shadow'`; absence or duplication is a fail-closed health error.

## State Machine

The complete transition graph is:

```text
open <----------------------> in_progress
  |                                |
  +----> done                      +----> done
  +----> obsolete                  +----> obsolete
  +----> expired                   +----> expired

done / obsolete / expired --------> same state: idempotent no-op
done / obsolete / expired --------> any other state: conflict
```

Rules:

- `open` and `in_progress` are the only active states.
- `done`, `obsolete`, and `expired` are terminal and immutable.
- Repeating the same terminal target returns the stored row without changing
  version or audit metadata, even if the retry carries the previous version.
- Repeating the same active target with the current expected version is also an
  idempotent no-op; a stale active retry is a version conflict.
- A conflicting terminal request returns a conflict and never overwrites the
  first resolution.
- `obsolete` means the work was superseded or its scoped thesis/work item no
  longer exists. It requires a reason.
- `expired` means an explicitly modeled validity condition ended and requires
  both a reason and authoritative evidence. A past due date is insufficient.
- `done` means an operator completed the work or an affirmative typed condition
  was proven. Manual completion may omit evidence; reconciler completion may
  not.
- Reopening is not supported. A correction creates a new action and retains the
  terminal row as audit history.

An action is `overdue` only when it is active and
`due_kst_date < current_date_in_Asia/Seoul`. `overdue` is derived response data,
not a stored state.

### Evidence envelope

Every non-null evidence value uses this versioned base envelope:

```json
{
  "schema_version": 1,
  "kind": "operator_attestation",
  "source": "postmortem review",
  "reference": "stable internal or external reference",
  "observed_at": "2026-07-14T12:00:00+09:00",
  "summary": "Bounded explanation of what was observed"
}
```

All six keys are required for `expired`; `observed_at` is RFC 3339 with an
offset, and the other strings are nonblank. Manual API/MCP expiration uses
`kind=operator_attestation`, whose schema has exactly these six keys and rejects
extras. Later reconcilers define their own versioned `extra=forbid` schemas.
Canonical JSON is limited to 16 KiB, nesting depth to five, key length to 64
characters, and each string to 2,000 characters. Keys containing
`secret`, `token`, `password`, `api_key`, `authorization`, or `cookie` are
rejected case-insensitively. Raw provider responses and credentials are never
stored; evidence contains references and a bounded summary only.

## Migration, Mixed-Version Cutover, and Compatibility

The schema migration is an expand-only shadow release:

1. Classify legacy values before casting. SQL `NULL`, JSONB `null`, and `[]`
   each mean zero actions. Any other non-array value fails preflight. Every
   array element must be an object with a nonblank `action`; status must be
   null/blank or one of `open/in_progress/done`; a nonblank due date must pass
   strict `YYYY-MM-DD` validation before any `date` cast. Errors report the
   retrospective ID and element ordinal.
2. Create the child table and a singleton lifecycle-control row in mode
   `shadow`. Install a parent JSON write-fence trigger: shadow mode permits
   legacy writes; canonical mode rejects inserts or updates that change
   `next_actions` unless the transaction has set the internal projection-writer
   marker `app.retrospective_action_projection_writer=v1` with `SET LOCAL`. The
   marker is a compatibility fence, not caller authentication, and is set only
   by repository code immediately before its projection write.
3. Backfill with `jsonb_array_elements(... WITH ORDINALITY)`. Missing, null, or
   blank status becomes `open`; existing `open`, `in_progress`, and `done` are
   preserved; overdue rows remain active. Zero-based `position` is
   `ordinality - 1`. Every migrated row uses parent
   `created_at`/`updated_at`, parent `updated_at` as the approximate
   `status_changed_at`, `migration:rob-878` as actor, and `migration` as source.
   Historical `done` also uses parent `updated_at` as approximate `resolved_at`
   and a complete versioned `legacy_status` evidence envelope explaining that
   the exact completion time was unavailable.
   The entire original element is copied to `legacy_payload` so projection
   rebuilds cannot discard unknown keys.
4. Assert one child per valid legacy element and field/ordinal parity, and emit
   counts. The migration leaves parent JSON byte-for-byte unchanged and does
   not activate canonical reads. Persistent test-database bootstrap versioning
   is bumped so the ORM table exists in reused pytest databases.

The later canonical cutover is the idempotent operator command
`uv run python scripts/retrospective_action_cutover.py --if-shadow`. It ships
with the canonical repository release and, in one transaction, it:

1. takes a transaction-scoped advisory lock and `LOCK TABLE
   review.trade_retrospectives IN SHARE ROW EXCLUSIVE MODE`, blocking
   inserts/updates while permitting reads and waiting for in-flight writers;
2. changes the control row to `canonical`, so every later old-version action
   write fails closed at the database trigger;
3. deletes the still-unpublished shadow children and rebuilds them from the
   now-frozen current parent JSON, assigning final stable IDs and the same
   deterministic backfill metadata;
4. verifies exact count and field parity, records cutover time/count, and only
   then commits;
5. enables canonical readers and manual mutation only after the command has
   succeeded. A failed parity check rolls back the control-mode change and the
   rebuilt rows together.

This full catch-up is intentionally cheap at the observed scale. The native
blue/green flow first health-checks and switches the new candidate while the
database is still in shadow mode and drains the old colors. `scripts/deploy-native.sh`
then switches/restarts the single-active services, completes its ordinary
health check, and invokes this command as a new explicit post-switch cutover
step after `BLUEGREEN_COMMITTED=1`. Until that step succeeds, all new processes
continue serving legacy JSON behavior and publish no shadow IDs. A pre-commit
cutover/parity failure leaves mode shadow, and the existing deploy trap may
safely roll traffic/code back. After a successful cutover, the script performs
a canonical health/parity check. A later failure may return traffic to old code,
but action mutation is fenced and the recovery instruction is roll-forward;
the rollback handler emits that degraded-mode warning. Non-native deployments
run the same command only after all old writers are switched/drained and before
enabling manual mutation. The control row is never switched from canonical
back to shadow after cutover.

Child drift after the initial shadow backfill is expected because legacy code
still writes only parent JSON; shadow parity is diagnostic, never an alert or a
read source. Only the locked cutover parity result establishes canonical
readiness.

New code reads the control row at the repository boundary. In `shadow` mode it
uses the legacy JSON reader/writer and refuses transition calls; in `canonical`
mode it uses children and the guarded projection writer. Thus the candidate can
pass health checks before cutover without publishing shadow IDs, and there is
no process-local feature flag that can disagree with database authority.

No legacy row is automatically completed, obsoleted, expired, merged, or
deleted. Duplicate text and duplicate issue IDs remain distinct rows.

For compatibility, the parent JSON projection keeps the old `status` vocabulary:
`open` and `in_progress` remain exact, while all canonical terminal states are
projected as `done`. It also includes `terminal_status` when the canonical state
is `obsolete` or `expired`. This lets an older reader continue hiding all
terminal actions after an application rollback. New application code never
uses this projection as lifecycle authority.

After the canonical application is deployed, the projection is rebuilt while
holding the parent row lock whenever actions are created, reordered, or
transitioned. Every action-writing path uses the same parent-before-child lock
order: parent `FOR UPDATE`, then every child for that parent `ORDER BY id FOR
UPDATE`, then mutation and projection rebuild. This serializes sibling changes
and prevents lost projections or opposite-order deadlocks. Projection output
starts from `legacy_payload`, overwrites canonical known fields, and enriches
all elements with action IDs and versions without changing unknown
user-authored fields or order. Projection failure rolls back the transaction;
there is no best-effort dual write.

Migration downgrade is supported only while the control row is still
`shadow`. Downgrade aborts if mode is canonical, any action has `version > 1`,
any source is not `migration`, or a canonical application has created an
action. After cutover, database downgrade and downgrade/re-upgrade are
unsupported because they would discard authoritative identities and terminal
audit. Recovery is mutation-disable plus roll-forward. Retiring the trigger and
parent projection is a separate cleanup issue after the rollback observation
window.

## Retrospective Save Semantics

`save_retrospective` remains the action creation/import boundary, not the
lifecycle mutation boundary.

- On a new retrospective, each action receives a server ID and normalized
  initial status. Missing status becomes `open`.
- On an existing retrospective, an incoming `action_id` must belong to that
  parent. Without an ID, the service reuses the first unmatched child
  with the exact canonical tuple `(action, owner, issue_id, due_kst_date)`, in
  existing display order. This occurrence-aware matching makes legacy retries
  idempotent even when identical text appears more than once or an action is
  already terminal.
- On an existing match, omitted status means “leave lifecycle unchanged”; it
  never normalizes back to `open`. An explicit conflicting status is rejected
  with guidance to use the transition API. Repeating the exact terminal tuple
  reuses the terminal row.
- Unmatched input creates a new action. Omitting an existing action does not
  delete or terminalize it; the omitted row follows incoming rows in its prior
  relative order. `force_new=true` plus a previously unused UUID
  `creation_key` is required to create an intentional second occurrence that
  otherwise matches an existing tuple; retrying that key reuses its row.
- Matched action text, owner, issue ID, and due date are immutable through the
  parent save path. An amendment creates a new row and explicitly obsoletes the
  old row; it is never inferred from omission. Duplicate `action_id` values in
  one request and IDs belonging to another parent return 422.
- After canonical cutover, new actions may begin only as `open` or
  `in_progress`. `done`, `obsolete`, and `expired` creation through
  `save_retrospective` is rejected; every new terminal state must use the
  authorized transition boundary. Initial status actor is derived from the
  authenticated web user, resolved MCP server profile, or internal service
  identity—not caller-controlled `created_by_profile`—with source
  `retrospective_save`, current timestamp, version 1, and no terminal fields.
  Legacy `done` values that existed before cutover remain migration records.
- Every optional retrospective input preserves field presence from HTTP/MCP
  transport through the repository. On update, omitted fields are untouched;
  explicit null clears only nullable fields. An omitted or null `next_actions`
  field performs no child reconciliation. This expands the existing `_UNSET`
  pattern to the full payload and prevents an action retry from clearing
  unrelated parent fields.
- The repository locks an existing parent while reconciling children. A
  concurrent first insert relies on the existing parent identity constraint,
  contains the insert in a database savepoint, reloads after a uniqueness race,
  and then performs the same locked reconcile without aborting the outer
  transaction. Correlation-less saves retain the current append-only behavior.

Full retrospective serializers hydrate canonical children and continue to
expose them under `next_actions`, so current callers receive richer action
objects without a top-level response break. Async repositories use
`selectinload` or one bounded child query for all selected parent IDs; the
synchronous serializer receives already-loaded child DTOs and never triggers
lazy IO. Query-count tests cover single and list reads to prevent N+1 and
`MissingGreenlet` regressions.

## Transition Service

Add a single domain operation with no dependency on the retrospective upsert:

```python
transition_retrospective_action(
    db: AsyncSession,
    *,
    action_id: UUID,
    target_status: ActionStatus,
    expected_version: int,
    actor: TransitionActor,
    reason: str | None,
    evidence: dict[str, object] | None,
    dry_run: bool = False,
) -> ActionTransitionResult
```

The service:

1. resolves the immutable parent ID, locks the parent row, then locks all of
   that parent's children with `ORDER BY id FOR UPDATE` and selects the target;
   every action writer follows this same order;
2. handles same-terminal retry or conflicting-terminal rejection first;
3. requires `expected_version` to match when the target row is active;
4. evaluates the state graph, reason, evidence, and actor/source policy without
   mutation when `dry_run=True`;
5. writes lifecycle timestamps and increments version exactly once;
6. rebuilds the parent JSON projection in the same transaction;
7. returns `changed`, `idempotent`, current action data, and bounded validation
   diagnostics.

The service flushes but never commits or rolls back the caller's session. Each
HTTP, MCP, or triage caller owns an explicit transaction and commits only after
the canonical row and projection both succeed; insert-race recovery is confined
to a nested savepoint. Caller-provided actor strings are never trusted. The web
boundary records `user:<stable database user id>`, the MCP boundary records its
server profile, and triage records the approving stable user ID with source
`triage`. Active transitions replace the latest active-state metadata; terminal
metadata is immutable. No transition path calls a broker, Linear, Telegram, or
another external system.

## HTTP and MCP Contracts

### HTTP

Add endpoints under the existing invest router:

- `GET /trading/api/invest/retrospectives/actions`
- `PATCH /trading/api/invest/retrospectives/actions/{action_id}`

GET is available to every active authenticated role. Its query contract is:
comma-separated canonical `status`, `market`, exact `symbol`, symbol-prefix
`q`, `owner`, `issue_id`, `overdue_only`, `due_before`, parent `trigger_type`,
parent `outcome_filter`, `kst_date_from`, `kst_date_to`, `limit`, and `offset`.
Omitted status means exactly `open,in_progress`; terminal history requires an
explicit status filter. Unknown statuses, malformed dates, negative offsets,
and invalid limits return 422. Limit defaults to 50 and is capped at 200.

The canonical response is
`{total, count, limit, offset, as_of, items}` where `count == len(items)` and
`total` and page rows come from one CTE/window statement, including an empty
page past the final offset. Ordering is
overdue first, then `in_progress`, earliest due date with nulls last, newest
update, and action ID. Each item has `action_id`, `version`, action, owner,
issue ID, canonical status, due date, overdue, status-changed/resolved time,
status actor/source/reason, and parent retrospective ID, correlation ID,
symbol, market, trigger, outcome, realized PnL, and created time. Raw evidence
and `legacy_payload` are not returned by list endpoints.

The existing `GET .../next-actions` route remains temporarily and preserves its
current `market`, `symbol`, and comma-separated `status` request contract plus
the `{market, symbol, count, scan_limit, items}` response envelope. With no
status it returns all active actions. It returns every match rather than adding
a new default page that could silently hide backlog; `count` is exact and
`scan_limit=0` is the documented sentinel for “no parent JSON scan cap.” Items
gain action ID, version, and overdue additively. The route is deprecated only
after every frontend caller moves to `/actions`.

PATCH is a global operator action because the data has no per-owner access
model. It requires an active `trader` or `admin`; a `viewer` receives 403. Since
`/trading/` is globally CSRF-exempt while this endpoint accepts the web session
cookie, a route-level dependency requires the existing signed `csrftoken`
cookie to match the `X-CSRFToken` header. Missing or invalid CSRF also returns
403. The body contains `target_status`, `expected_version`, optional reason,
and optional evidence. A success response is
`{changed, idempotent, dry_run: false, item}`. A stale or conflicting response
is 409 and includes current ID, status, and version; unauthenticated, missing,
and invalid requests map to 401, 404, and 422. Tests cover role and CSRF before
the service is called.

### MCP

Expose `get_retrospective_actions` and the read-only
`retrospective_action_transition_preview` through the ordinary retrospective
registration. The separate committing `retrospective_action_transition` tool
is physically registered only in the `tradingcodex_execution` profile, which
already requires a non-empty MCP bearer token. It is added explicitly to that
profile's learning-write allowlist and forbidden-set tests; it is not inherited
from the broad default registration.

`get_retrospective_actions` mirrors the canonical HTTP filters, ordering,
active-by-default semantics, and page envelope. The preview tool exposes no
`dry_run` argument; its server wrapper always passes `dry_run=True`, so a caller
cannot smuggle a false value through an input payload.

The committing tool still defaults to `dry_run=true`. A committed response records the
server-attested actor `mcp:tradingcodex_execution`. The unrestricted/default
profile has only the read and preview tools and therefore cannot commit, even
if a caller supplies a profile-like argument. Tool descriptions state that due
date alone cannot produce `expired`. Because `tradingcodex_execution` returns
early from the broad registry, both `get_retrospective_actions` and the preview
tool are also added explicitly to its learning-read allowlist. Exact allowlist,
forbidden-set, and real-FastMCP boot tests prove that default has no commit tool,
the restricted profile can read IDs/versions before transition, and no
duplicate registration occurs.

## Consumer Cutover

### `decision_history`

Add `open_actions` to the core decision context after the canonical read path
lands. It uses the caller's normalized symbol and the same account-mode,
smoke-test, and mock-counterfactual cohort rules as existing retrospective
lessons and outcomes. Extract one shared retrospective-visibility predicate and
use it for lessons, outcomes, and actions: `kis_mock` is exact only when that
account mode is requested; the default path excludes the mock-counterfactual
cohort instead of inventing a new exact account or market predicate.

The field contains at most five active actions, ranked by overdue,
`in_progress`, due date, recency, and action ID as the final tie-breaker. Action
text is truncated to 220 characters, owner to 80, and issue ID to 32. The full
canonical JSON for `open_actions` is capped at 3 KiB by dropping lowest-ranked
items after field truncation. Each compact item includes action ID, action text,
status, owner, issue ID, due date, and overdue; terminal actions and lifecycle
evidence are excluded. Every returned decision context includes `open_actions`
(possibly empty) and `open_actions_meta` with `authority=historical_advisory`,
`executable=false`, returned count, and truncation flag. Action text is data,
never an executable instruction or authorization for a tool call. An action by
itself is a valid signal, so context is returned when every older section is
empty.

The compact `analyze_stock_batch` path receives this only when `quick=true`,
matching its current decision-history injection boundary. Frozen analysis
bundles capture the same context through their separate decision-history
section. Tests cover both explicit paths, empty-field presence, string and
aggregate budgets, and the advisory trust marker. Stock-detail's separate
decision-history schema is not extended, avoiding duplication with its existing
retrospective card.

### `/invest/insights`

The page remains read-only. Its action section moves to the paginated canonical
GET endpoint and shows:

- distinct `open` and `in_progress` labels;
- owner, due date, and overdue indicator;
- a Linear link only for syntactically valid `ROB-<number>` issue IDs when the
  build-time `VITE_LINEAR_WORKSPACE_URL` is configured as an HTTPS origin/path;
  the client appends `issue/<url-encoded-key>` to that validated base.
  Otherwise it renders the issue key as plain text and never hardcodes a
  workspace URL;
- exact total, loading/error state, and pagination or progressive expansion;
- market, trigger, outcome, symbol query, and retrospective date filters passed
  to both retrospective and action requests with the same semantics. The action
  request also sends `status=open,in_progress` explicitly even though that is
  the server default.

No checkbox, status button, or inline editor is added. Mutation stays in the
authenticated API/MCP operator flow until transition audit data has been
observed in production.

Stock-detail rendering switches from `status != done` to the explicit active
allowlist. It receives canonical children through retrospective hydration and
does not add a second action copy inside `DecisionHistoryCard`.

`RetrospectivesPanel` is shared by desktop/mobile insights and desktop/mobile
portfolio pages. A single canonical frontend `RetrospectiveAction` type backs
both normal and compact modes, and issue coverage includes all four hosts so a
portfolio regression cannot hide behind insights-only tests.

## Existing Backlog Triage

Backfill is structural only. After manual transitions are available, run a
separate evidence-preserving triage over every active action. A repository
operator CLI, `uv run python scripts/retrospective_action_triage.py`, owns this
workflow with `export` and `apply` commands. Export writes versioned JSONL
containing action ID/version, immutable parent context, current fields, and
blank proposed target/reason/evidence fields. The operator completes the
manifest, records an active `trader`/`admin` database user ID as approver, and
cites the manifest SHA-256 explicitly when applying it.

The workflow is:

1. export action ID, retrospective context, owner, issue ID, due date, and
   proposed disposition without mutation;
2. classify each row as still open, in progress, done, obsolete, expired, or
   insufficient evidence;
3. require an operator-approved reason for terminal changes and structured
   evidence for expiration;
4. run `apply` in dry-run mode by default; commit requires both an explicit
   commit flag and the matching manifest hash;
5. process at most 25 rows per invocation with one transaction per action, so a
   stale row cannot roll back already audited siblings;
6. append a result JSONL keyed by action ID with changed, idempotent, conflict,
   invalid, or unresolved outcome. Re-running the same manifest skips recorded
   successes and safely resumes conflicts after a fresh export.

The triage must not infer state from keywords. Actions without sufficient
evidence remain active even when overdue. The production snapshot's 73 active
rows are the initial planning baseline, not a completion target that justifies
forced closure. The CLI uses the same transition service, stable actor, source
`triage`, role check, evidence validation, and expected-version rules as HTTP
and MCP; it has no private state-mutation shortcut. Its runbook defines manifest
storage/cleanup and prohibits committing evidence files containing credentials
or raw provider payloads.

## Later Automation Boundary

Automation starts only after manual lifecycle metrics are available. It adds a
typed resolution binding to an action; free-form action text is never parsed.
Initial binding families are independently implemented and tested:

- delivered watch event, keyed by exact alert/event identity;
- closed forecast, keyed by exact forecast ID;
- broker-reconciled journal close or position-flat condition, keyed by account,
  market, symbol, and an explicit choice between one journal and the whole
  position.

A watch trigger can make an action due for operator work without completing it.
It produces a terminal transition only when the binding explicitly defines the
delivered event as completion. Order submission, an accepted cancel response,
or a partial FIFO journal close is never sufficient position-flat evidence.

The resolver follows the forecast resolver's external contract but adds row
locking: batchable, idempotent, and `dry_run=true` by default. It is initially
manual/scheduleless and emits shadow summaries. Scheduling requires a later
rollout decision backed by no-evidence, conflict, and false-positive metrics.
No resolver performs broker mutations.

The projection-retirement follow-up cannot begin until all application and MCP
clients use canonical IDs, deprecated-alias traffic is zero, and at least 14
consecutive production days show zero parity mismatch and no emergency rollback
requiring the compatibility reader.

## Observability and Rollout

Emit structured metrics/logs for:

- lifecycle control mode, cutover parity, and fenced legacy-write rejections;
- child/projection parity mismatches;
- active totals by status, owner presence, issue-ID presence, and overdue;
- transitions by source, target, changed/idempotent/conflict result;
- missing-evidence and invalid-transition rejections;
- decision-context truncation count;
- resolver dry-run candidates and no-op reasons.

Roll out in this order:

1. deploy the additive schema, shadow backfill, control row, trigger, and parity
   checks without publishing child IDs;
2. deploy a candidate that falls back to legacy behavior in shadow mode,
   health-check it, switch traffic, and drain the old process;
3. switch/restart the remaining single-active services, pass the ordinary
   health check, then run the native post-switch catch-up/cutover step and a
   canonical health/parity check with manual mutation still disabled;
4. enable authorized manual transition surfaces;
5. perform bounded manual backlog triage;
6. release decision-history and read-only UI consumers in parallel;
7. scope the related typed resolver in shadow mode; scheduling remains
   disabled and does not block the manual lifecycle.

## Testing

Implementation follows RED-GREEN-REFACTOR. Required coverage includes:

- migration upgrade/downgrade, malformed-data preflight, null-to-open mapping,
  SQL/JSON null and non-array handling, unknown-key preservation, strict dates,
  timestamp provenance, schema-bootstrap bump, and field/count parity;
- mixed-version writes during shadow, atomic writer fencing/catch-up, failed
  cutover rollback, native post-switch step ordering, canonical second
  health failure, old-writer rejection after cutover, and guarded downgrade;
- parent cascade, constraints, indexes, and ORM smoke tests;
- legacy save retry after terminal resolution, duplicate occurrences/IDs,
  omitted-status preservation, `force_new` creation-key idempotency, initial
  terminal rejection/server-attested actor, full field-presence semantics,
  parent/ordered-child locking, projection atomicity, and no unrelated
  parent-field changes;
- the full transition matrix, terminal idempotence, conflicting terminal
  rejection, stale versions, save-versus-transition and sibling concurrency,
  deadlock regression, transaction ownership, and audit persistence;
- a ROB-665-style regression proving due date alone never expires an action;
- evidence envelope exact-key/shape/size/depth/secret-key rejection and stable
  actors;
- web 401, viewer/CSRF 403, 404, 409, and 422 behavior;
- MCP dry-run default, default-profile commit absence, restricted-profile
  read/write registration/authentication, preview false-value bypass rejection,
  real boot, and provenance;
- exact action pagination/total/filter/order semantics and compatibility alias;
- eager/batch hydration query count plus full retrospective and stock-detail
  active-state compatibility;
- bounded/trusted `decision_history`, `quick=true` batch-analysis injection, and
  frozen-bundle capture without terminal actions;
- read-only insights rendering, overdue/status/owner/issue display, filter
  parity, pagination, URL-config fallback, visible fetch errors, and all four
  desktop/mobile insights/portfolio hosts;
- triage manifest/hash/role validation, dry-run, resume, and conflict reporting;
- later adapter regressions for duplicate watch events, undelivered events,
  missing forecast evidence, and partial-position closes.

No test may contact a live broker, Linear, Telegram, or another external
service.

## Linear Issue Decomposition

ROB-878 is too broad for one implementation issue. Split it into these seven
children after this design is approved:

1. **회고 액션 shadow 원장 — schema/preflight/backfill/write fence**
   - Child/control schema, trigger, legacy-payload preservation, strict
     preflight, shadow backfill/parity, guarded downgrade, bootstrap, and
     migration tests. No canonical read or mutation.
2. **회고 액션 canonical cutover — repository/save projection/read API**
   - Locked/idempotent catch-up command, native deployment post-switch/drain/
     restart step and canonical re-probe, rollback warning, control-mode
     repository, parent-before-children locking, field-preserving save
     reconcile, compatibility projection, eager hydration, active-reader
     cutover, canonical GET and legacy alias, mixed-version runbook, and
     parity/query tests.
3. **회고 액션 전이 코어 — state machine/version/evidence**
   - Domain graph, locked service, transaction boundary, version/idempotency,
     typed base evidence, immutable terminal audit, and concurrency tests. No
     HTTP or MCP registration.
4. **회고 액션 operator surface — authorized PATCH + privileged MCP**
   - Trader/admin plus CSRF HTTP contract, canonical read/preview tools,
     execution-only commit tool, profile allowlists, error DTOs, documentation,
     and authorization/boot tests.
5. **기존 미완료 회고 액션 증거 기반 triage tool/run**
   - Versioned JSONL export/apply, manifest hash approval, dry-run default,
     bounded resumable commits, operator runbook, actual review, and final
     unresolved/overdue report. No keyword auto-classification.
6. **`decision_history.open_actions` 주입**
   - Bounded canonical context, analyze/bundle propagation, token limits, tests,
     advisory trust boundary, and MCP documentation.
7. **`/invest` 회고 액션 read-only triage UX**
   - Exact paginated endpoint consumption, state/owner/issue/due visibility,
     shared filters/types, configured Linear link fallback, stock-detail
     active-state fix, four-host coverage, and frontend tests.

Create two related follow-up issues rather than ROB-878 blockers:

- **증거 기반 action reconciler — typed binding + dry-run 우선**: binding
  contract, locked batch resolver, shadow metrics, default-off scheduleless
  task, and runbook. Watch, forecast, and position adapters remain separate.
- **회고 액션 legacy projection retirement**: remove the parent projection,
  write-fence trigger, fallback reader, and deprecated alias only after the
  rollback window and sustained parity metrics.

Dependencies are:

```text
1 -> 2 -> 3 -> 4
          |    |
          |    +------> 5
          +-----------> 6
          +-----------> 7

5 -> related reconciler -> watch / forecast / position adapter issues
4 + 7 -> related projection-retirement issue (after observation window)
```

Issues 6 and 7 may run in parallel after issue 2. Issue 5 starts only after the
transition core and operator surface are deployed. The reconciler and cleanup
issues do not block ROB-878 and neither enables scheduling by itself.

## Acceptance Criteria for ROB-878

ROB-878 is complete when:

- every valid legacy action has one stable canonical ID with verified parity;
- active and terminal semantics are explicit across all readers;
- authorized operators can transition one action atomically with durable audit
  data, and unauthorized/default-profile MCP callers cannot commit;
- the existing active backlog has been reviewed without time- or text-based
  forced closure, with unresolved actions reported honestly;
- active actions are bounded in decision context and usable in
  `/invest/insights` without adding UI mutation;
- mixed-version rollback cannot silently overwrite the child ledger, and the
  legacy projection has an explicit later retirement gate.

The related automation phase is complete only when its typed resolver is
dry-run-first, each evidence adapter has its own acceptance contract, and no
automatic schedule is enabled without a separate evidence-backed rollout
decision.
