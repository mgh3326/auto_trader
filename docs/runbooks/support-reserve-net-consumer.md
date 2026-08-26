# Support reserve-net consumer — seam-gated proposal creation

## Current state

`app.services.support_reserve_net_consumer.SupportReserveNetConsumer` is a
deterministic consumer of `decision_rules.buy.support_reserve_net`. It still has
no scheduler registration, broker client, account lookup, DB-session
factory, or deployment activation. It receives all evidence and an already-open
`OrderProposalsService` transaction from its caller; it creates proposal rows
only through the public watcher-scope seam.

`support_reserve_net_consume` is its explicit MCP call surface. The MCP caller
owns the DB session and one transaction and supplies the complete evidence
packet. The tool performs no broker read or account lookup.

The consumer itself does not send a broker order. After the proposal transaction
commits, the MCP caller invokes the existing proposal dispatch path. Existing
classification, approval, veto, revalidation, and submit gates remain unchanged;
reserve-net provenance is observation metadata and is not a classification
input. Consequently, an already-armed downstream auto-approval configuration
may submit an eligible proposal. This PR does not arm it.

## Call-surface decision

**Selected — explicit MCP trigger (B).** An operator-controlled session decides
when to assemble fresh evidence and call `support_reserve_net_consume`. Human or
session intervention therefore occurs before proposal creation, at the trigger
and evidence-review boundary. A validation or pre-commit failure leaves only the
structured tool result and logs; it leaves no proposal. A post-commit dispatch
failure leaves durable proposals plus each proposal's dispatch evidence.

The rejected alternatives are:

- **Scheduler (A):** a timer would move intervention to runtime arming and
  could create proposals when no session is present to inspect the evidence or
  immediate failure. Schedule registration needs separate operator approval,
  and unattended rearm additionally needs the fill-triage account×currency
  state promoted to a durable lock. No flow, task, schedule registration, or
  timer is added here.
- **Insertion into an existing session flow (C):** the available discovery
  fan-out is deliberately observation-only and is also exposed in read-only
  profiles. Making it persist proposals would turn an analysis helper into an
  order-intent boundary, make intervention implicit, and couple its failures to
  unrelated discovery work. A failure could then leave a proposal from a
  partially completed broader session flow.
- **Combination (D):** multiple triggers would give the same candidate more
  than one initiation path, make the human intervention point inconsistent, and
  increase duplicate/race pressure. Its failure residue would depend on which
  trigger ran. One explicit surface is kept instead.

The tool is registered only inside the existing
`ORDER_PROPOSALS_ENABLED` default-off surface. It is a conditional buy helper,
not a `route_request` standard-sequence step. No new environment gate is added
or armed.

## Seam readiness and call order

Readiness is not an environment switch or a constant set to true. At each
`consume` call, the consumer checks that the supplied object actually exposes
both public operations:

- `inspect_watch_to_order_scope`;
- `create_proposal_in_watch_to_order_scope`.

If either operation is unavailable, `consume` returns
`atomic_self_open_order_read_seam_unavailable` and creates zero proposals.
It does not fall back to `list_recent`, an internal repository import, a cached
open-order answer, or ordinary `create_proposal`.

For every selected proposal, the required sequence is:

```text
require explicit submissions_frozen evidence
→ parse Literal-constrained request keys
→ validate every MCP-boundary join-key representation
→ inspect legacy broker_account_id=None scope (lock + empty required)
→ inspect concrete four-axis scope (lock + empty required)
→ create_proposal_in_watch_to_order_scope on that concrete inspection
→ caller-owned commit or rollback
```

All selected scopes are inspected before the first companion create. The
consumer does not commit or roll back, so the service instance and transaction
that acquired each advisory lock remain in force through its create.

## Broker-account representation contract

For automatic reserve-net creation, `broker_account_id` is an opaque canonical
identifier from the account-evidence source. It must be a non-empty string in
its exact supplied representation, with no leading or trailing whitespace.
The consumer does not invent an alias, substitute a default account, or rewrite
case or punctuation. Candidate and cash evidence must use that same canonical
value; otherwise the candidate is rejected before a seam inspection and the
result is zero proposals.

The MCP boundary applies the same contract to candidates, cash snapshots,
reserve-net attributions, and self-unfilled orders before it opens a DB session.
It accepts the exact opaque value only. It never trims, case-folds, rewrites
punctuation, maps an alias, or fills a missing value. If a candidate and cash
snapshot use different exact values, cash evidence does not join and the result
is zero proposals.

`None` is never a canonical automatic-creation identity. It is a distinct
legacy ledger scope, not a wildcard. The consumer locks and checks that legacy
scope first. An active legacy proposal blocks the concrete-id create with
`legacy_unscoped_active_proposal_exists`; this closes the NULL-ledger probe.

## Join-key representation contract

The complete consumer join/filter-key census and its primary protection are:

| Key | Protection |
|---|---|
| `broker_account_id` | MCP exact-opaque gate; a nonjoining account/cash record also rejects the candidate |
| `beneficial_owner_id` | MCP exact-opaque ambiguity gate across all four owner-bearing record groups |
| `side` | MCP Pydantic `Literal["buy", "sell"]` adapter |
| `strategy` | MCP exact-opaque gate plus exact reserve-net strategy vocabulary |
| `sector_cluster` | MCP exact-opaque ambiguity gate within owner×market |
| `market`, `state`, `intent` | request Pydantic `Literal` types |
| `normalized_symbol` | both blocker records and candidates use the consumer's shared symbol normalization; candidates must already equal that canonical form |
| `account_mode`, `currency` | a mismatch cannot find usable cash and rejects the candidate |

Every `beneficial_owner_id` in `candidates`, `reserve_net_attributions`,
`self_unfilled_orders`, and `sector_exposures` must be an exact non-empty opaque
string without surrounding whitespace. Multiple records with the same exact ID
and records for distinct owners may coexist. Two different raw IDs that collapse
under the reject-only case/Unicode/separator fingerprint are ambiguous and stop
the request. The fingerprint is never substituted into consumer evidence or
used as a join value.

Every `self_unfilled_orders[].side` must be exactly `buy` or `sell`. Upper-case,
mixed-case, surrounding-whitespace, and other values are rejected; the boundary
does not silently lower-case them. Every
`reserve_net_attributions[].strategy` must be a non-empty exact string. If its
reject-only fingerprint could mean `buy.support_reserve_net`, the raw value must
equal that canonical strategy exactly. A genuinely different exact strategy is
allowed and remains non-attributable to reserve-net.

Every candidate and sector-exposure `sector_cluster` must be an exact non-empty
string without surrounding whitespace. Within one beneficial-owner×market
scope, different raw sectors with the same reject-only fingerprint are
ambiguous and stop the request. Distinct sectors and independently consistent
sector spellings for different owners may coexist.

All five MCP-boundary guards run before consumer construction, DB-session
opening, or seam inspection. A violation creates zero proposals. Without these
contracts, evidence can silently miss the consumer joins and evade all three of
the following invariants:

1. same-symbol dedupe, including self-unfilled and active reserve-net checks;
2. `max_active_orders_per_symbol`;
3. `max_symbols_per_sector_cluster`; projected sector-cap excess is retained as
   an advisory, not used as an admission gate.

This is fail-closed ambiguity detection, not convenience normalization. An
assembler that cannot prove its exact representation must not call this create
surface.

## Residual race with the manual create path

`app/mcp_server/tooling/order_proposal_tools.py::order_proposal_create` remains
outside the watcher-scope lock. Therefore a manual human/agent create can still
commit in the interval after this consumer has inspected a scope and before its
companion create. The seam serializes its own callers, not that manual path.

This is an explicit operating constraint, accepted only while the manual path
remains separately reviewed and the existing proposal approval/dispatch gates
remain in place: do not manually create a matching account×market×symbol scope
while this consumer is running. The consumer never interprets its seam snapshot
as proof that an out-of-seam manual create cannot intervene. Migrating every
automatic caller to the seam does not remove this manual-path window.

The explicit-session surface accepts this residual window because it adds no
unattended timer, the manual path retains its separate proposal/approval gates,
and the operator can avoid overlapping matching-scope calls. This is an
operating constraint, not a serializability claim.

## Failure outcomes and residue

| Scenario | What happens | What the code prevents | Remaining limitation / evidence |
|---|---|---|---|
| Same candidate is consumed twice in succession | The first call commits an active concrete-account proposal. The second checks the legacy scope and concrete scope and returns `watch_to_order_scope_active_groups_present`. | The seam and active-scope check prevent a second companion create among seam users. | An out-of-seam manual create can still enter the residual window documented above. |
| Cash or account evidence is stale | `is_fresh=false`, missing cash, incomplete pending-cash accounting, or mismatched exact account IDs rejects the candidate and creates zero proposals. | Selection stops before seam creation. | The tool receives evidence rather than fetching it. It cannot independently detect a caller that incorrectly labels old evidence fresh; the invoking session owns timestamps and truthfulness. |
| The seam is unavailable | `consume()` returns `atomic_self_open_order_read_seam_unavailable`; the caller rolls back to release transaction locks and creates zero proposals. | There is no fallback to ordinary create or cached open-order state. | The structured result and server log remain; an operator/session must fix capability wiring before retrying. |
| One selected candidate fails while proposal rows are being created | Every selected scope was inspected first; any pre-commit exception rolls back the single DB transaction, so none of that batch's proposal rows remain. | DB persistence is all-or-none for the selected batch. | A commit acknowledgement failure is reported as `proposal_commit_outcome_unknown` with maybe-committed IDs; reconcile before retry. After a successful commit, approval/submit dispatch is per proposal and cannot be atomic across brokers: an earlier item may submit before a later dispatch fails. Durable proposal and dispatch evidence remains for each item. |

`submissions_frozen` must be explicitly present in every MCP request; omission
is rejected before consumer construction, DB-session opening, or seam
inspection and yields zero proposals. `submissions_frozen=true` is a fail-closed
input and also yields zero proposals. This manual/session-triggered surface does
not implement unattended fill-driven rearm. The fill-triage durable-lock
prerequisite remains binding for any future scheduler or automatic rearm
registration.

## Candidate controls

- Inputs must carry fresh account/currency cash evidence and explicitly account
  for same-account/currency local pending cash. Missing, stale, ambiguous, or
  incomplete self-open-order/sector evidence yields no candidate.
- `max_owned_or_open_symbols_per_market` counts only reserve-net-attributable
  `armed`, `open`, and `filled` symbols. It does not count another strategy's
  holdings. Same-beneficial-owner self unfilled buys still block a matching
  candidate across accounts.
- The pool order is `[eligible_new, eligible_add]`: all eligible new candidates
  are considered before at most one add fallback per market. Add exact ties end
  with `normalized_symbol ASC`.
- Add ranking is exactly support strength, independent source count, honest
  upside, post-fill sector increase, and required cash. Deep-loss depth is never
  a positive ranking input.
- An add needs R-931 `PASS` (≤7 days), a policy table ≤36 hours, recomputed
  `A_limit(10%)` at the proposed limit, full lot-rounded funding, no partial
  fill, and at most one add symbol per market. US adds are disabled pending a
  thesis-review contract.
- The caller must prove a non-negative post-fill sector increment. A missing
  sector or incomplete sector exposure is not treated as zero exposure. A
  projected post-fill concentration above the policy's 10% value is selected
  only if every other gate passes, and is returned in
  `plan.sector_cluster_cap_advisories` and persisted under
  `source_asof.sector_cluster_cap_advisories`; it is never silently dropped or
  used as an admission block.
- A KR new candidate requires net available cash of at least `400,000 KRW`; a
  lower amount yields zero KR new arms. The all-buy 90% and reserve-net armed
  50% cash caps remain fail-closed.
- Anchor math is `tick_floor(support × (1 - discount))`; the 5–10% support
  discount and final -15% to -5% current-price band are both checked. An
  out-of-band anchor is excluded, never clamped.

## Verification and operating rule

Run the consumer and seam contract suites before changing this consumer:

```bash
uv run pytest tests/services/test_support_reserve_net_consumer.py \
  tests/services/order_proposals/test_watch_to_order_scope.py \
  tests/mcp_server/tooling/test_support_reserve_net_consumer_tool.py -q
```

Passing these suites does not authorize a broker probe, environment arm,
deployment, or direct broker order. This consumer only persists a proposal via
the existing service-layer seam; all later execution gates remain independent.
