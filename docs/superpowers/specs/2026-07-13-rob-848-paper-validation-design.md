# ROB-848 — Paper Validation State and Authorization Design

- **Issue:** ROB-848
- **Date:** 2026-07-13
- **Base:** `origin/main` `6229a28ac8235d911b8f90c0960b89b87eb34b9c`
- **Status:** approved in Linear and the session prompt

## Goal

Provide a server-enforced, append-only paper-validation state and authorization
boundary. Researcher and reviewer identities may append narrative research records,
but only operator or system identities may register a validation, advance it, authorize
a paper order, or make a terminal promotion decision. No LLM-authored text can change
an experiment identity, deterministic gate result, or active strategy payload.

## Scope and ownership

ROB-848 owns validation transitions, immutable hypothesis/review audit, role resolution,
exact hash authorization, and a separate MCP validation registrar. It reuses the
ROB-846 strategy experiment registry and canonical hash authority without duplicating
their identity model or formula.

ROB-849 owns cohort storage, `CanonicalMarketSnapshot`, and the concrete production
`ExperimentProvenanceVerifier`. ROB-848 stores opaque cohort/evidence IDs and hashes and
exposes the role/state/hash authorization contract ROB-849 will consume. Tests use fake
providers/verifiers/adapters only. ROB-850 owns quantitative soak/performance policy;
ROB-848 can deterministically decide only whether its local state, role, evidence
stamps, and hashes satisfy the transition contract.

## Alternatives considered

1. **Append-only transition log with transaction locks — selected.** Current state is
   derived from ordered valid events. PostgreSQL advisory locking and unique constraints
   serialize validation mutations across processes.
2. **Mutable validation row plus audit table — rejected.** A cached `current_state`
   creates a second authority and permits drift from history.
3. **Reuse the broker façade registrar or add a profile — rejected.** ROB-845 owns the
   profile and its exact six-tool broker façade. Validation is composed beside it only
   in the existing `PAPER_EXECUTION` early-return branch.

## State graph

The only graph is:

```text
draft -> offline_eligible -> shadow_soak -> paper_active
      -> promotion_eligible -> promoted | rejected | aborted
```

The indentation above continues from `paper_active`: there are four prerequisite edges
ending at `promotion_eligible`, then three mutually exclusive terminal edges.

Registration appends sequence 1 with `prior_state = NULL` and `new_state = draft`.
Every later event must advance exactly one edge. Skips and reversals return
`invalid_transition`; any request after `promoted`, `rejected`, or `aborted` returns
`terminal_state`. Promotion eligibility is a distinct deterministic transition.
Promotion requires a later, explicit operator/system confirmation event.

## Persistence

All three tables live in the existing `research` schema:

- `paper_validation_state_transitions`: validation/experiment/version identity,
  monotonic sequence, idempotency key and canonical request hash, prior/new state,
  server-derived actor ID/role, stable reason code/text, exact experiment/cohort/
  strategy/config/policy/input hashes, evidence IDs, and server timestamp.
- `strategy_hypothesis_drafts`: immutable author record with mechanism, universe,
  horizon, entry, exit, invalidation, data requirements, expected cost hurdle,
  turnover/risk bounds, cited evidence, exact frozen hashes, and timestamp.
- `paper_validation_postmortem_reviews`: immutable evaluator record with review text,
  cited evidence, exact frozen hashes, and timestamp. Deterministic metrics and gate
  results remain transition evidence and are never accepted as review text fields.

`paper_validation_state_transitions.experiment_id` references
`research.strategy_experiments.experiment_id`. An insert trigger verifies the stored
ROB-846 experiment, strategy, frozen-config, and policy hashes exactly. DB triggers
reject UPDATE and DELETE on all three tables. There are no update/delete services.

Unique constraints cover `(validation_id, sequence)` and
`(validation_id, idempotency_key)`. A structural check permits NULL prior state only on
the initial draft and requires sequence 1 for it. State values, roles, hash lengths,
and positive sequences are closed by constraints.

The Alembic revision is additive and descends from the single head observed at start,
`20260713_rob866_manual_alerts`. Model metadata, migration DDL, and
`tests/_schema_bootstrap.py` move together. Downgrade removes only ROB-848 objects; the
operational rollback remains disabling tool registration, never deleting audit rows.

## Service contracts and authorization

`ActorRoleProvider` resolves the authenticated caller ID into exactly
`researcher | reviewer | operator | system`. MCP payloads contain no actor ID or role.
Missing caller identity, an unmapped production identity, or an ambiguous mapping fails
closed before a repository, adapter, ledger, client, or broker is called.

`FrozenInputHashProvider` and `PolicyHashProvider` are injected protocols. They return
verified stamps; no ROB-838/839 concrete implementation is imported. A missing provider,
exception, or unverified stamp returns `evidence_stamp_unavailable`. A verified but
nonmatching stamp returns `evidence_hash_mismatch`.

The role matrix is:

| Operation | researcher | reviewer | operator | system |
| --- | --- | --- | --- | --- |
| Read frozen audit/history | allow | allow | allow | allow |
| Append hypothesis | allow | deny | deny | deny |
| Append postmortem review | deny | allow | deny | deny |
| Register/advance/activate | deny | deny | allow | allow |
| Authorize paper order | deny | deny | allow | allow |
| Confirm promote/reject/abort | deny | deny | allow | allow |

An order authorization is an immutable return value, not a broker action. It is allowed
only in `paper_active` or `promotion_eligible`, exact-binds the current experiment,
cohort, strategy, config, policy, and input hashes, and performs no adapter, ledger,
client, or broker work. ROB-849 can combine this contract with its concrete provenance
verifier later.

## Transaction and idempotency behavior

Each registration/transition transaction obtains
`pg_advisory_xact_lock(hashtextextended(validation_id, 0))`, then re-reads the latest
transition, verifies the exact current state and hashes, and appends the next sequence.
There is no process-local lock.

The request is encoded with the existing ROB-846 canonical hash utility. Reusing the
same idempotency key with the same canonical payload returns the existing event. Reusing
the key with a different payload returns `idempotency_conflict`. Two different
concurrent requests from the same prior state serialize; exactly one appends and the
loser returns `concurrent_transition_conflict` without changing state.

## MCP composition

`PAPER_EXECUTION_TOOL_NAMES` and the six ROB-845 façade tools stay unchanged. A separate
`PAPER_VALIDATION_TOOL_NAMES` and `paper_validation_registration.py` own validation
reads and mutations. `registry.py` imports both registrars and, in the existing
`McpProfile.PAPER_EXECUTION` early-return branch, registers both only when the same
`PAPER_EXECUTION_ENABLED` default-off gate is enabled.

The exact profile allowlist is
`PAPER_EXECUTION_TOOL_NAMES | PAPER_VALIDATION_TOOL_NAMES`. Tests separately prove that
the broker façade itself remains exactly six tools. No new profile enum, global registry
branch, or base broker capability registry is introduced. The execution registrar does
not import validation implementation. Every new mutation tool is added to readonly deny
settings and classification tests.

## Stable failure reasons

The public failure vocabulary is closed around:

- `forbidden`
- `actor_identity_unavailable`
- `evidence_stamp_unavailable`
- `evidence_hash_mismatch`
- `validation_not_found`
- `experiment_identity_mismatch`
- `invalid_transition`
- `terminal_state`
- `idempotency_conflict`
- `concurrent_transition_conflict`
- `promotion_confirmation_mismatch`

Failures happen before side effects and never append an invalid transition.

## Safety and testing

Tests cover the full transition, role, provider, and hash matrices; actor payload
spoofing rejection; complete ordered history and author/evaluator/operator separation;
sequential and concurrent duplicate/conflict behavior; ROB-846 registry/hash regression;
exact MCP union/default-off/readonly behavior; and AST guards against live-order,
broker-native submit, and ROB-816 proposal mutation imports/calls.

Real PostgreSQL verification uses a disposable database and runs upgrade, downgrade,
upgrade. Two independent sessions synchronized by a barrier prove append-only triggers,
sequence/idempotency constraints, FK/hash mismatch rejection, duplicate replay, and
conflicting transition single-winner behavior. Relevant tests are repeated under xdist.

ROB-720 remains independently blocked until at least 30 resolved negative-class
forecasts. ROB-848 neither changes that status nor treats narrative review text as a
calibration result.

## Out of scope

- Cohort persistence or scheduling.
- `CanonicalMarketSnapshot` persistence or production snapshot adapters.
- A concrete production `ExperimentProvenanceVerifier`.
- Live orders, broker-native submit, proposal mutation, automatic promotion, or active
  strategy payload mutation.
- Quantitative 7-day shadow/60-day paper gate policy owned by ROB-850.
