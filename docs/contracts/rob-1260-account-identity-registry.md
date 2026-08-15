# ROB-1260 — account identity and lane registry contract

Status: **inert and fail-closed**. This contract defines no broker transport,
database, scheduler, deployment, signing, or credential-value I/O. Its helpers
may invoke opaque caller callbacks after static validation. It adds no schema or
migration.

Authority:

- Linear ROB-1260 description
- operator addendum SHA-256
  `5bf99507f7e1c7ccfa0255dfd784b716f72ed63364e68bdad0c45a5a9f3c5d04`
- J2 preflight SHA-256
  `71513c149a265c94f286c401fa5e56667e52a9888007a9f224d9ff6de2bdeab7`
- J1 merge base `5d63d60239ea98ed83459071892f6304323b1742`
- J2B merge `094ab2d59d6f2bf5fc3df4efa43bb5d412221ffd`

The implementation is `app/services/mock_lane_registry.py`. It intentionally
does not modify J2B's intent, plan, attempt, ID, canonical-JSON, or hash
surfaces.

## Canonical registry snapshot

Every physical identity is currently `physical_account_id=null` and
`identity_status=UNKNOWN`; consequently every row has `writer=false` and
`auto=false`. Missing policy, cap, physical owner, and canary bindings remain on
the row as reasons. They are never used as a reason to omit the lane.

| `lane_id` | quote | role | lane status | activation | credential namespace | exact host allowlist |
|---|---|---|---|---|---|---|
| `kr.kis.mock` | KRW | `AUTO_MIRROR` | `OBSERVATION_TEMPORARY` | `BLOCKED` | `KIS_MOCK_*` | `openapivts.koreainvestment.com:29443` |
| `kr.kiwoom.mock` | KRW | `PRIMARY_AUTO` | `NOT_READY` | `BLOCKED` | `KIWOOM_MOCK_*` | `mockapi.kiwoom.com` |
| `us.kis.mock` | USD | `AUTO_MIRROR` | `NOT_READY` | `BLOCKED` | `KIS_MOCK_*` | `openapivts.koreainvestment.com:29443` |
| `us.kiwoom.mock` | USD | `BROKER_REGRESSION` | `NOT_READY` | `BLOCKED` | `KIWOOM_MOCK_US_*` | `mockapi.kiwoom.com` |
| `us.alpaca.paper.default` | USD | `PRIMARY_AUTO` | `AUTO_READY_BLOCKED_BY_POLICY` | `BLOCKED` | `ALPACA_PAPER_*` | `paper-api.alpaca.markets` |
| `us.alpaca.paper.lab` | USD | `null` (`policy_absent`; future reference `AUTO_CHALLENGER`) | `AUTO_READY_BLOCKED_BY_LIFECYCLE` | `BLOCKED` | `ALPACA_PAPER_LAB_*` | `paper-api.alpaca.markets` |
| `crypto.binance.spot_demo.canonical` | USDT | `PRIMARY_AUTO` | `NOT_READY` | `BLOCKED` | `BINANCE_SPOT_DEMO_API_*` | `demo-api.binance.com` |
| `crypto.binance.spot_demo.b0x_sidecar` | USDT | `SHADOW_ONLY` | `OBSERVATION_TEMPORARY` | `DISABLED` | `BINANCE_SPOT_DEMO_API_*` | `demo-api.binance.com` |
| `crypto.alpaca.paper.default` | USD | `AUTO_MIRROR` | `NOT_READY` | `DISABLED` | `ALPACA_PAPER_*` | `paper-api.alpaca.markets` |
| `crypto.alpaca.paper.clean` | USD | `AUTO_MIRROR` | `NOT_READY` | `DISABLED` | `ALPACA_PAPER_CRYPTO_*` | `paper-api.alpaca.markets` |
| `crypto.upbit.shadow` | KRW | `SHADOW_ONLY` | `SHADOW_ONLY` | `DISABLED` | none | none; the declared callback path is rejected |
| `crypto.binance.futures_demo` | USDT | `null` | `DISABLED_NO_STRATEGY` | `DISABLED` | `BINANCE_FUTURES_DEMO_API_*` | `demo-fapi.binance.com` |

The host and namespace columns record repository literals only. They do not
prove which physical account a credential keyset reaches. That proof requires a
separate broker fingerprint and evidence reference.

## Canonical startup and lineage binding

`assert_registry_startup` verifies the ordered 12-row registry, the exact
13-field identity subset, non-live modes, strict scheduler/timing owner types,
preserved blocked rows, safe unknown identity, signed lane restrictions, and
exact host/namespace bindings. `assert_single_writer` groups opaque physical
account IDs internally and fails startup if more than one lane claims writer
ownership. Errors identify lanes but render the account as `[MASKED]`. Both
guarded helpers run this full canonical/global validation; a caller-supplied
mapping cannot replace or weaken the signed identity facts.

`PolicyBinding(policy_version, policy_version_hash)` is frozen, nonblank, and
in-memory only. No model, database column, or migration is added. A real J2B
`LineageEnvelope` is compared exactly before any opaque callback or factory:

1. validate the full canonical registry and global writer cardinality;
2. resolve the exact registered `lane_id`;
3. compare quote currency, broker, account profile, and exact
   `mock|paper|demo|shadow` account mode;
4. compare the intent policy version/hash to the registry `PolicyBinding`;
5. enforce immutable signed lane and recurring rules;
6. validate the caller-declared host and symbolic credential namespace;
7. require complete known-identity execution evidence;
8. only then invoke the supplied opaque callback or factory.

Current canonical rows cannot reach step 8. Tests use counter-only callbacks and
perform no network operation. KIS and Kiwoom attempt envelopes with J2B's
`lane_prefix=None` and `broker_client_id_target=None` pass only when their plan
binds to a fully evidenced registered lane. An unregistered broker may receive
internal J2B IDs, but J2A stops it at the existing `unknown_lane` lookup. J2A
does not add broker targets or reproduce J2B's confirmed-target rejection.

## Static declared-string helper limitation

`guarded_broker_io` and `guarded_client_factory` validate caller-declared
endpoint and namespace strings. Their callbacks are opaque and zero-argument;
these helpers do not prove that the actual callback transport consumes those
declarations. Real host/profile validation at each broker send boundary is a
J3+ transport responsibility.

The registry contains symbolic Binance host/namespace facts, URL parsing,
live-host rejection, and pre-I/O declared-string checks. It contains no Binance
HTTP/WS transport, signing, credential-value loading, or actual endpoint call.

## Activation guards

The activation enum is exactly:

`DISABLED | BLOCKED | READY | ENABLED | RUNTIME_ACCEPTANCE_PENDING |
READY_FOR_MOCK_DEPLOYMENT`.

`transition_activation` enforces all three signed rules:

- G1: `ENABLED` requires directly proven preservation of an existing cadence;
  a new recurring schedule ends in `AUTO_READY_BLOCKED_BY_SCHEDULER`.
- G2: `READY_FOR_MOCK_DEPLOYMENT` is terminal. A shared production release or
  live restart stops there with no automatic promotion.
- G3: J8 canary success can move only
  `RUNTIME_ACCEPTANCE_PENDING -> READY`, never directly to `ENABLED`.

In addition, signed writer-false, `SHADOW_ONLY`, role-null,
`BROKER_REGRESSION`, and lane-status ceilings remain enforced after a physical
identity and every dynamic binding become known. Recurring authorization
requires both `lane_status=AUTO_ENABLED` and `activation_status=ENABLED`.
Satisfying only one state fails with `lane_recurring_not_authorized`; bounded
canary evidence is a separate path and never supplies recurring authority.

## Lane independence

`record_mirror_divergence` creates an immutable lane-scoped record whose peer
rollback and peer cancellation fields are fixed to `false`. It performs no
broker or ledger operation.
