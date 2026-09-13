# Fill/watch context-only consumption (Phase 0)

## Status and boundary

This runbook describes the deliberately narrow Phase 0 boundary for #137: a
default-off, scheduleless consumer records non-economic context outcomes for
supplied `lane.event` envelopes and supplied context artifacts. It does not
create a proposal, mutate a watch or report, read an account, contact a broker
or gateway, issue/refresh credentials, or run a scheduler.

The only persistent write is one `review.fill_watch_context_outcomes` row per
canonical transport UUID, made through `FillWatchContextOutcomeService`. A
delivery/persist acknowledgement is not a business-consumption decision. The
former says whether the UUID row was durably accepted; the latter records only
one of the closed context statuses below.

This is an orderless artifact boundary, not an activation procedure. The
operator-hub route and any operator lane registration are absent pending
director review.

## Access matrix

The matrix was compared before implementation against the read-only operator
sources `CLAUDE.md`, `live/CLAUDE.md`, `mock/CLAUDE.md`, and
`operator_contract.yaml`. The YAML's existing-profile and mutation-policy
entries cover its own named lanes; neither those documents nor this Phase 0
scope authorizes a new context-consumer route. The YAML's prohibition on a new
database schema applies to records in the operator repository, whereas this
repository's approved additive table is owned here and is restricted by this
runbook and its service layer. This was a source/configuration comparison, not
a connection to an operator runtime.

| Surface | Phase 0 result | Basis |
|---|---|---|
| Existing `lane.event` envelope (`kind`, `lane`, `event_id`, `text`, optional routing metadata) | Allowed input only | `app/services/lane_events.py:98-161`; producer transport shape |
| Supplied market/position/watch/route snapshots in a context artifact | Allowed input only; never refreshed here | Artifact contract in `app.services.fill_watch_context.contracts` |
| UUID context outcome | Allowed write only, through `FillWatchContextOutcomeService` and its repository | `app.services.fill_watch_context.service` |
| Account reads/writes, positions, cash, and order history | Excluded | No account tool or client is registered in the closed profile |
| Proposal, report, or watch create/lifecycle mutation | Excluded | No proposal/report/watch tool is registered |
| Preview/place/modify/cancel/replace/reconcile | Excluded | No broker/order/reconcile tool is registered |
| Broker or gateway network call | Excluded | Consumer imports no broker/gateway client and has no network transport |
| Credentials, OAuth issue/refresh, approval hashes, signatures, or capabilities | Excluded | Read-only token seam only; no issuer is imported |
| Shell escape | Excluded | The entrypoint reads one supplied JSON artifact; it starts no subprocess |
| Scheduler, activation, deployment, or operator lane registration | Excluded | One-shot entrypoint, independent default-false flag, no TaskIQ/cron wiring |

The MCP profile is closed-world: its complete served tool set is
`fill_watch_context_consume_artifact` and `fill_watch_context_outcome_get`.
The consumer can never gain a forbidden capability by a prompt instruction;
the server does not register the name.

## Artifact and outcome contract

The incoming object is deliberately two-part:

```json
{
  "lane_event": {
    "kind": "lane.event",
    "lane": "context-fill",
    "event_id": "canonical UUID",
    "text": "producer-facing event text"
  },
  "context": {
    "event_kind": "fill or watch",
    "input_as_of": "timezone-aware timestamp",
    "economic_root_ref": "stable non-economic root reference",
    "order_refs": ["opaque order reference"],
    "market_snapshot": {"as_of": "timestamp", "freshness": "fresh or stale"},
    "position_snapshot": {"as_of": "timestamp", "freshness": "fresh or stale"},
    "route": {"availability": "available or unavailable"}
  }
}
```

`lane_event` follows the existing transport shape; `context` is supplied beside
it and is not claimed to be a field emitted by the current transport. The
consumer requires the event id to be a canonical UUID. The older fill-handoff
producer currently uses an `execution_ledger:<id>` event key
(`app/services/fill_event_handoff/service.py:391-437`), so a UUID artifact
adapter is a future integration, not a claim that this Phase 0 process is
already subscribed to that producer.

The closed contextual-status vocabulary is:

| Status | Meaning | Next action |
|---|---|---|
| `context_only_no_action` | Supplied context is recorded; no economic action exists here | `none` |
| `stale_input` | A supplied market or position snapshot is stale | `refresh_context` |
| `failed_processing` | The supplied artifact declares itself incomplete/failed | `inspect_artifact` |
| `needs_human` | A route is unavailable or the same UUID conflicts semantically | `await_route` or `operator_review` |

There is intentionally no proposal-created, approval, execution, reservation,
or plan/rung status. A unique transport UUID is the dedupe key. An economic
root is indexed but never unique: a partial and a full fill with different UUIDs
therefore retain two queryable outcome rows while being grouped by the same
root. A replay with the same semantic digest increments delivery accounting; a
different semantic digest for the same UUID converges to the existing row and
marks it `needs_human` without creating a second logical outcome.

## Toss issuer ownership and evidence separation

### Repository/config facts

The one ownership contract for any later shared Python/Go deployment is:
**gatewayd is the sole Toss OAuth issuer; every non-owner consumes a
read-only cached token provider and never issues or force-reissues.**

This Phase 0 consumer has no Toss client at all. Its local token seam has only
the cached-read method, and its test supplies an issuer-shaped stub whose issue
and force-reissue methods fail if touched.

The current repository supports a selectable issuance mode:

- `app/services/brokers/token_issuance.py:27-41` treats `gatewayd` as the
  owner and has Python re-read the established cache after a token-ensure
  acknowledgement.
- `app/services/brokers/toss/auth.py:138-220` shows that the pre-existing
  self-owned path may issue/force-reissue, while the `gatewayd` branch is
  selected before that path.
- `app/services/brokers/toss/client.py:145-200` is deliberately not used by
  this consumer because its existing retry path can ask its token manager to
  force reissue.
- The read-only Go comparison has `OAuthClient.Issue` at
  `go-toss/toss/auth.go:21-37`, while `TokenProvider` at
  `go-toss/toss/client.go:39-45` owns token supply and the client only calls
  it (`:187`). Its `TestTokenProviderOwnsExpiryAndRefresh` is at
  `go-toss/toss/client_test.go:257-275`.

Enforcing gatewayd ownership across both repositories would require a separate
cross-repository deployment/configuration change. It is explicitly not made by
this Phase 0 change.

The existing Toss booking rule remains separate: a future first-stage Toss
websocket may only wake a targeted REST reconcile. The existing REST
cumulative-delta booking kernel is the only path that writes a Toss ledger.
It must not be routed through the fill-sequence HTTP ingest path, whose
idempotency identity includes `fill_seq`; the models are not unified. The
current repository runbook documents REST reconcile as the booking path at
`docs/runbooks/toss-fill-reconcile-poller.md:5-24`.

### Prior runtime observed

The runtime-baseline packet records bounded observations made earlier on
2026-09-07, including an observed environment configuration and the absence of
Toss fill-poller logs in one inspected container. Those are prior observations,
not current deployment facts and not an authorization to alter any setting.
The Toss assessment similarly records a bounded prior source review and a
possible websocket follow-up; it is not a runtime verification.

### Not observed today

No production environment, runtime configuration, OAuth exchange, gateway,
broker, websocket, scheduler, account, or live/shadow deployment was inspected
or contacted today. No claim is made about current ownership, token freshness,
route availability, fill latency, or real trade volume.

## Single-owner takeover plan

This plan covers both the existing `watch-alert-relay` lane (listed in the
MCP audit as an existing lane) and fill triage (documented in
`docs/runbooks/fill-event-claude-triage.md`). It does not register either route.
Any loaded-state statement from those materials is dated prior evidence only.

1. **Pending.** A producer leaves the original lane delivery untouched. A
   context artifact is eligible only after a canonical UUID is supplied.
2. **Claim/consume.** The context consumer writes the UUID outcome in one
   short transaction. The unique UUID is the single-owner boundary; an
   existing row is replayed, never independently consumed.
3. **Drain.** Before cutover, drain pending artifacts by UUID in deterministic
   order and query every UUID outcome. Existing watch-alert-relay and fill
   triage keep their current owners until their respective pending set is
   accounted for.
4. **Recovery and late register.** On restart, re-submit the same artifact.
   It reads the durable UUID outcome. A late registration maps to that same
   UUID; a semantic conflict becomes `needs_human`, not a second outcome.
5. **Failover/reorder.** A successor uses the same UUID table, so a reordered
   partial/full sequence retains each transport outcome and root grouping.
   There is no free-running retry loop or broker retry.
6. **Cutover.** Only after director review: freeze new admission to the old
   owner, drain and account all old UUIDs, then arm the new route in a
   separately approved change. This Phase 0 PR performs none of those steps.
7. **Rollback.** Disable the independent event-loop flag, stop new artifact
   admission, retain outcomes for inspection, and return lane ownership to the
   previously approved route. Never delete outcomes to make a replay look new.

## Local shadow harness

The local replay harness is deterministic and scheduleless. It reports noise,
transport duplicates, root coalescing, latency, and unconsumed outcomes. Its
readiness predicate is exactly:

```
elapsed >= 48 hours AND distinct economic roots >= 20
```

Synthetic fixtures exercise logic only and never count as a real shadow or
trade sample. The required real shadow remains `NOT_STARTED`; no 23:50 job,
scheduler, lane, or deployment is registered here.

For a local, explicitly armed replay, use the one-shot
`scripts.fill_watch_context_shadow_replay` entrypoint with a supplied JSON
array, fixed `--finished-at`, and fixed `--elapsed-seconds`. It checks the
independent gate before reading an artifact or opening a database session and
always reports `real_shadow: NOT_STARTED`; it is not an activation command.

The later online-isolation delta is intentionally precise: director approval
would be needed for an operator-hub route that starts a separate process with
`MCP_PROFILE=fill-watch-context`, a scoped environment whose only arming
control is the independent context flag, and a served-tool attestation before
route registration. That change must retain the two-tool closed world and may
not reuse a proposal or execution profile. It is outside this Phase 0 PR.

## Explicit follow-ups

- A real at-least-48-hour, at-least-20-root shadow measurement.
- A separately authorized server-side T3 plan/authority/budget boundary.
- A cross-repository gatewayd issuer enforcement rollout, if the Go consumer
  is introduced.
- Director review, operator-hub route/profile, and any live activation.
