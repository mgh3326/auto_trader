# ROB-1260 — account identity and lane registry contract

Status: **inert and fail-closed**. This contract performs no broker, database,
scheduler, deployment, or credential I/O. It adds no schema or migration.

Authority:

- Linear ROB-1260 description
- operator addendum SHA-256
  `5bf99507f7e1c7ccfa0255dfd784b716f72ed63364e68bdad0c45a5a9f3c5d04`
- J2 preflight SHA-256
  `71513c149a265c94f286c401fa5e56667e52a9888007a9f224d9ff6de2bdeab7`
- J1 merge base `5d63d60239ea98ed83459071892f6304323b1742`

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
| `crypto.upbit.shadow` | KRW | `SHADOW_ONLY` | `SHADOW_ONLY` | `DISABLED` | none | none; broker I/O is structurally rejected |
| `crypto.binance.futures_demo` | USDT | `null` | `DISABLED_NO_STRATEGY` | `DISABLED` | `BINANCE_FUTURES_DEMO_API_*` | `demo-fapi.binance.com` |

The host and namespace columns record repository literals only. They do not
prove which physical account a credential keyset reaches. That proof requires a
separate broker fingerprint and evidence reference.

## Startup and pre-I/O assertions

`assert_registry_startup` verifies immutable lane identity, non-live modes,
separate lane/activation states, preserved blocked rows, safe unknown identity,
and exact host/namespace bindings. `assert_single_writer` groups opaque physical
account IDs internally and fails startup if more than one lane claims writer
ownership. Errors identify lanes but render the account as `[MASKED]`.

The guarded broker boundary applies checks in this order:

1. resolve the exact lane;
2. compare the plan and registry quote currency, raising exactly
   `lane_quote_currency_mismatch` on disagreement;
3. reject shadow, live, malformed, or non-allowlisted endpoints;
4. require the exact credential namespace;
5. require `ENABLED`, a single evidenced writer, `auto=true`, and every signed
   policy/cap/owner/canary/reconcile binding;
6. only then invoke the supplied broker callback.

Current canonical rows cannot reach step 6. The tests use callback spies and
perform no network operation.

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

## Lane independence

`record_mirror_divergence` creates an immutable lane-scoped record whose peer
rollback and peer cancellation fields are fixed to `false`. It performs no
broker or ledger operation.
