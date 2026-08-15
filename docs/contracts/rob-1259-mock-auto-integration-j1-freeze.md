# ROB-1259 J1 — mock/paper/demo integration contract freeze

This is a vocabulary-and-type contract only. It is implemented by the
side-effect-free definitions in `app/schemas/execution_contracts.py`; it does
not choose a physical account, register a scheduler, call a broker, or change
an existing ledger.

Authority used for this freeze:

- Master Contract §3 (`contract-mock-auto-integration-20260815.md`)
- J0 verified audit SHA-256
  `7200bcb43af423ce9d1df3a593bc42330c9e3074003a01c1cabb4e7cd90cb69e`
  (§C lane matrix and §E2 split)
- Signed operator addendum SHA-256
  `5bf99507f7e1c7ccfa0255dfd784b716f72ed63364e68bdad0c45a5a9f3c5d04`
  (D5 currency contract)
- Repository base `fc990658547ba18196bd186e6ff4b06ff84e7f85`

## Operator common header

J1 does not select a profile, writer, cadence, cap, canary, or currency-rate
behavior. When an exact literal or evidence is absent, the later owner must end
in its applicable blocked or pending state rather than create a value. Only
`orch-mock` may merge a verification-passing PR.

## Three-layer records

The records are strict, frozen Pydantic definitions. Every listed field is
required; a nullable field must still be supplied explicitly as `null` when it
is not yet known.

### `DecisionIntent`

| Field | Type / invariant |
|---|---|
| `decision_intent_id` | non-blank opaque identifier |
| `policy_version` | non-blank policy version |
| `policy_version_hash` | non-blank existing policy hash representation; J1 does not rehash or impose a digest length |
| `decision_timestamp` | timezone-aware timestamp |
| `market_data_cutoff` | timezone-aware timestamp, not after `decision_timestamp` |
| `symbol` | non-blank decision symbol; where a DB symbol conversion applies, its canonical form remains `.`-separated |
| `side` | `buy` or `sell` |
| `target_notional` | finite, positive `Decimal` |
| `target_notional_currency` | required `Literal["KRW", "USD", "USDT"]`; the unit of `target_notional` |
| `limit_policy` | required opaque JSON object; detailed policy vocabulary belongs to a later owner |
| `expiry_policy` | required opaque JSON object; detailed policy vocabulary belongs to a later owner |
| `rationale` | non-blank text |

### `ExecutionPlan`

| Field | Type / invariant |
|---|---|
| `execution_plan_id` | non-blank opaque identifier |
| `decision_intent_id` | non-blank parent identifier |
| `lane_id` | non-blank lane identifier; its registry is a J2A responsibility |
| `broker` | non-blank broker identifier |
| `account_profile` | non-blank logical profile identifier |
| `account_mode` | non-blank existing account-mode value |
| `normalized_symbol` | non-blank broker-ready symbol; conversion remains owned by `app/core/symbol.py` where applicable |
| `quantity` | finite, positive `Decimal`; a share/base-asset quantity, not a currency amount |
| `limit_price` | `null` or finite, positive `Decimal` |
| `quote_currency` | required `Literal["KRW", "USD", "USDT"]`; the unit of `limit_price`, `quantity * price`, plan min-notional, and plan monetary caps without their own unit |
| `tick_rounding` | required opaque JSON object |
| `session` | non-blank string or `null` |
| `time_in_force` | non-blank string or `null` |
| `min_order_validation` | required opaque JSON object |
| `risk_caps` | required opaque JSON object |

### `OrderAttempt`

| Field | Type / invariant |
|---|---|
| `order_attempt_id` | non-blank opaque identifier |
| `execution_plan_id` | non-blank parent identifier |
| `cycle_id` | non-blank existing cycle identity, with its current lane-owned derivation retained |
| `idempotency_key` | non-blank existing deduplication identity, with its current scope retained |
| `broker_client_order_id` | non-blank broker client ID or `null` before it is available |
| `broker_order_id` | non-blank broker order ID or `null` before acknowledgement/readback |

The containment relation is fixed: one stable `DecisionIntent` may produce one
or more account-specific `ExecutionPlan` records, and each `OrderAttempt`
references exactly one plan. This contract does not prescribe persistence,
fan-out, claim, submit, terminal-state, or recovery behavior.

Each plan is independently identified and carries its own `lane_id`; J1 has no
rollback or cancellation operation. A later mirror implementation must keep
one lane's outcome separate from the others and record any divergence without
using this vocabulary to cancel another plan.

## Currency alignment and strict JSON

`create_execution_plan(decision_intent, **plan_values)` is the pure J1
creation boundary for an intent-derived plan. It checks
`target_notional_currency` against the supplied `quote_currency` before
constructing `ExecutionPlan`, so a mismatch creates no plan and raises exactly
`currency_conversion_not_authorized`. The companion
`validate_plan_currency_alignment(decision_intent, execution_plan)` protects
an already materialized decision/plan pair. Neither function performs a
conversion, rate lookup, broker call, or persistence operation. `KRW`, `USD`,
and `USDT` remain distinct literals.

`OrderAttempt` does not duplicate a currency; it inherits the plan context via
`execution_plan_id`. J2A owns its later registry-specific quote-currency check
and its distinct `lane_quote_currency_mismatch` result; the
`CURRENCY_ALIGNMENT_ERROR_CODES` vocabulary contains both exact error strings,
but no registry is added by J1.

These frozen models are strict for Python inputs. For JSON serialized by
`model_dump_json()`, the required deserialization path is
`DecisionIntent.model_validate_json(payload)` or
`ExecutionPlan.model_validate_json(payload)`. Callers must not pass a
`json.loads()` dictionary to `model_validate()` as a substitute, because that
would reject JSON timestamp/decimal representations under this strict contract.

## Common control vocabulary

`LaneStatus` is the canonical `lane_status` type. `LaneState` remains a
backward-compatible alias, and `LANE_STATES` remains its historical set name.
`ActivationStatus` is a separate type and currently has only the signed
`READY_FOR_MOCK_DEPLOYMENT` value. Thus
`AUTO_READY_BLOCKED_BY_SCHEDULER` remains a lane status, while
`READY_FOR_MOCK_DEPLOYMENT` is not merged into it.

`LaneRole` is a single `StrEnum` value, never a combined role string. Its signed
vocabulary is `PRIMARY_AUTO`, `AUTO_MIRROR`, `BROKER_REGRESSION`, and
`EXECUTION_AUTO`. `SchedulerOwner` separately uses exactly
`taskiq|prefect|orch|manual|disabled`; `TimingOwner` is a distinct opaque owner
type because J1 has no signed timing-owner value set. These are vocabulary types
only: J1 does not add a registry or attach the fields to the three frozen
records. J2A owns the registry surface that will use them.

## Lane state allowlist

`LaneStatus` is the only J1 lane-status vocabulary:

```text
AUTO_ENABLED
AUTO_READY
AUTO_READY_BLOCKED_BY_POLICY
AUTO_READY_BLOCKED_BY_LIFECYCLE
AUTO_READY_BLOCKED_BY_ACCOUNT_STATE
AUTO_READY_BLOCKED_BY_SCHEDULER
OBSERVATION_TEMPORARY
SHADOW_ONLY
DISABLED_NO_STRATEGY
NOT_READY
UNKNOWN
```

J0 §C fit is exact. All 12 rows use an allowlisted value, and the snapshot has
zero `AUTO_ENABLED` rows.

| J0 lane/profile | Frozen state |
|---|---|
| KR/KIS mock | `OBSERVATION_TEMPORARY` |
| KR/Kiwoom mock | `NOT_READY` |
| US/KIS mock | `NOT_READY` |
| US/Kiwoom mock | `NOT_READY` |
| US/Alpaca paper default | `AUTO_READY_BLOCKED_BY_POLICY` |
| US/Alpaca paper lab | `AUTO_READY_BLOCKED_BY_LIFECYCLE` |
| Crypto/Binance Spot Demo — canonical paper cohort | `NOT_READY` |
| Crypto/Binance Spot Demo — B0-X sidecar | `OBSERVATION_TEMPORARY` |
| Crypto/Alpaca paper — canonical cohort/default account | `NOT_READY` |
| Crypto/Alpaca paper — clean account profile | `NOT_READY` |
| Crypto/Upbit synthetic shadow | `SHADOW_ONLY` |
| Crypto/Binance Futures Demo | `DISABLED_NO_STRATEGY` |

The matrix's account/profile split is preserved, particularly the two Spot
Demo and the two Alpaca crypto rows. J1 does not merge them or change their
state rationale.

## Signed addendum expressibility check

| Addendum constraint | J1 vocabulary / record support | Boundary retained |
|---|---|---|
| D1 Alpaca crypto remains unassigned and stops `NOT_READY` | `LaneStatus.NOT_READY` | J2A later records registry facts; J1 does not select a profile. |
| D2 sidecar remains observation-only and recurring owner is disabled | `LaneStatus.OBSERVATION_TEMPORARY` and `SchedulerOwner.DISABLED` | No writer, cadence, or scheduler is attached by J1. |
| D3 US/Kiwoom is `BROKER_REGRESSION`, not `AUTO_MIRROR` | distinct single `LaneRole` values express both states | The lane-specific allowed role is a later registry policy, not a J1 runtime check. |
| D4 has no new recurring schedule | `LaneStatus.AUTO_READY_BLOCKED_BY_SCHEDULER` is separate from `ActivationStatus.READY_FOR_MOCK_DEPLOYMENT` | J1 adds neither scheduler code nor a cadence. |
| D6 uses a limit plan with preserved sizing evidence | `target_notional`, `market_data_cutoff`, `quantity`, `limit_price`, `tick_rounding`, and `min_order_validation` hold the required values and opaque evidence | Exact venue step-size/source key grammar remains the later owner’s signed contract; J1 adds no speculative fields. |

## Evidence tier vocabulary

`EvidenceTier` uses exactly these values:

| Value | Meaning |
|---|---|
| `FACT` | directly observed source evidence |
| `INFERENCE` | conclusion drawn by combining direct facts |
| `UNVERIFIED` | not established from the permitted evidence surface |

J0 §G records its middle category as `INFERENCE`; that spelling is canonical
here. The illustrative adjective `INFERRED` is not a second wire value, so a
claim cannot split across two synonymous categories.

`research_contracts/strategy_ownership_manifest.py:47-130` already has an
`EvidenceStatus`/`EvidenceFact` pair, but it models a broader ownership-manifest
authority lifecycle (`accepted`, `missing`, `draft`, `stale`, and more). It is
not a J0 claim-tier type and importing it would break this shared schema leaf.
The narrowly scoped `EvidenceTier` therefore adds the missing three-value
audit vocabulary without replacing that manifest contract.

## Reuse census and additive boundary

The census scanned `app`, `scripts`, `tests`, `research_contracts`, and `docs`
at the frozen base with whole-tree `rg` queries. Hit counts are file counts,
not a claim that every occurrence has one shared meaning.

| Existing vocabulary / object | Census result and anchors | J1 decision |
|---|---|---|
| Shared execution schema leaf | `app/schemas/execution_contracts.py:1-16`, existing `ExecutionReadiness` at `:128-146` and `OrderPreviewLine` at `:148-191` | Extend this leaf; do not create a competing generic schema module. |
| `correlation_id` | 295 matching files. Existing lane-specific derivations include `app/services/paper_correlation.py:17-35`, `app/services/live_correlation.py:15-34`, `app/services/kis_mock_runner/correlation.py:8-25`, and `app/services/brokers/binance/demo_strategy_loop/correlation.py:14-33`; native ledger columns include `app/models/review.py:206,278,716,845`. | Preserve every native derivation and ledger column. No new J1 `correlation_id` field or derivation is introduced; later work may carry the existing value through additive metadata. |
| `cycle_id` | 19 matching files. B0-X owns deterministic derivation in `scripts/b0x/derivation.py:158-170,230-280`; Kiwoom batch ownership uses it at `scripts/b0x/kr/kiwoom.py:875-1162`. | Reuse the name only on `OrderAttempt`; J1 deliberately does not impose a new cross-lane format. |
| `idempotency_key` | 231 matching files. KIS's durable pre-send reservation is `app/models/review.py:592-613` and `app/services/order_send_intent_service.py:30-95`; paper intent has its own exact provenance-derived key at `app/services/brokers/paper/contracts.py:154-206`. | Reuse the field name on `OrderAttempt`, retaining current scope and uniqueness behavior. J1 does not claim global uniqueness or replace existing reservations. |
| `approval_*` | 194 matching files. Existing approval material is ledger metadata in `app/models/review.py:454,551,709-712` and dedicated preview/confirmation behavior in `app/mcp_server/tooling/orders_kis_variants.py:487-496` and `app/mcp_server/tooling/orders_toss_variants.py:965-1118`. | Keep approval artifacts separate from decision/plan/attempt identity. No new approval field, token, or gate is created. |
| Policy version/hash | `app/services/trading_policy_service.py:111-134` returns the current policy version with its existing short `content_hash`; paper validation also carries full hashes at `app/services/paper_validation/contracts.py:71-90`. | Retain `policy_version_hash` as an opaque, non-blank contract field so both existing representations remain usable. No new hash function or policy storage is added. |
| `quote_currency` | Existing B0-X envelope/state vocabulary includes `scripts/b0x/envelope.py:28,48,85` and `scripts/b0x/state.py:73,82`; `scripts/b0x/kill_switch.py:163-184` rejects missing or mismatched values before comparing monetary quantities. | Reuse `quote_currency` exactly on `ExecutionPlan`; add the signed `target_notional_currency` only where it denotes the decision notional's unit. No second plan-currency abstraction is introduced. |
| Existing decision/plan records | 16 matching files. `PaperCohortDecision` and `PaperCohortVenueIntent` are cohort-specific at `app/models/paper_cohort.py:287-467`; `PaperOrderRequest` and `VerifiedPaperOrderIntent` are paper-experiment boundary records at `app/services/brokers/paper/contracts.py:124-206`. Exact general classes named `DecisionIntent`, `ExecutionPlan`, and `OrderAttempt` were absent from the scan. | Add only the contract-minimum, broker-neutral types in the existing shared leaf. Existing specialized records remain untouched. |

The additive boundary is explicit:

- No ORM model, migration, ledger column, JSONL format, repository, service,
  router, task, scheduler registration, broker adapter, or configuration value
  is changed by J1.
- Existing correlation, idempotency, and approval data stay in their current
  native storage. J1 supplies names and type shape for later consumers only.
- No direct string symbol conversion is added. Existing `to_kis_symbol`,
  `to_yahoo_symbol`, and `to_db_symbol` remain the conversion authority.
- A broker identifier is never treated as fill evidence. It is nullable at the
  attempt layer until the appropriate later acknowledgement/readback path has
  evidence.

## J0 §E2 hand-off boundary

J1 supplies the state, three record shapes, evidence vocabulary, and the
additive compatibility rule. It intentionally leaves J2A's physical
account/profile registry and J2B's durable correlation/idempotency/capability
work unimplemented. The later J3+ lane work remains responsible for its own
claim, mutation, and recovery paths.
