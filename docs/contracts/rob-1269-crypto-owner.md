# ROB-1269 — crypto ownership and conflict contract

## Scope and authority

This is a contract-only artifact.  It consumes the signed registry without
changing it and authorizes no broker, network, database, scheduler, deployment,
or account operation.

The controlling registry is
`app/services/mock_lane_registry.py::CANONICAL_LANE_REGISTRY`.  The six crypto
rows below are a literal consumption of that registry.  `role` is a
purpose-only registry value; it is not execution authority.

## Signed crypto lane matrix

| lane_id | role (purpose only) | lane_status | activation_status | scheduler_owner | writer | auto_order_enabled | quote_currency |
| --- | --- | --- | --- | --- | ---: | ---: | --- |
| crypto.binance.spot_demo.canonical | PRIMARY_AUTO | NOT_READY | BLOCKED | DISABLED | false | false | USDT |
| crypto.binance.spot_demo.b0x_sidecar | SHADOW_ONLY | OBSERVATION_TEMPORARY | DISABLED | DISABLED | false | false | USDT |
| crypto.alpaca.paper.default | AUTO_MIRROR | NOT_READY | DISABLED | None (owner absent; mutation-ineligible; downstream bind authority 없음) | false | false | USD |
| crypto.alpaca.paper.clean | AUTO_MIRROR | NOT_READY | DISABLED | None (owner absent; mutation-ineligible; downstream bind authority 없음) | false | false | USD |
| crypto.upbit.shadow | SHADOW_ONLY | SHADOW_ONLY | DISABLED | None (owner absent; mutation-ineligible; downstream bind authority 없음) | false | false | KRW |
| crypto.binance.futures_demo | None | DISABLED_NO_STRATEGY | DISABLED | DISABLED | false | false | USDT |

`DISABLED` is the explicit `SchedulerOwner.DISABLED` enum member.  The three
`None` values above mean exactly **owner absent; mutation-ineligible;
downstream bind authority 없음**.  They are not an alternative spelling of
`DISABLED`, and they do not grant authority to invent an owner.

All twelve registry rows initialize `writer=False` and
`auto_order_enabled=False` in `_canonical_entry`
(`app/services/mock_lane_registry.py:319-368`).  The two Alpaca crypto
`AUTO_MIRROR` rows therefore remain purpose-only, `NOT_READY`, disabled for
activation, with no mutation profile, mutation path, or canary authorization.

## Identity and ownership safety

The registry's unknown-identity rule is the controlling safety terminal:

> broker fingerprint 증거 부재 시 physical_account_id=null,
> identity_status=UNKNOWN, writer=false, auto=false. 행은 삭제하지 않는다.

It is implemented at `app/services/mock_lane_registry.py:114-117` and checked
by `tests/test_mock_lane_registry.py:548-556`.  Consequently, the Binance
canonical and B0-X sidecar relationship is an `UNKNOWN` physical conflict
domain unless masked fingerprint evidence is supplied.  This artifact chooses
no writer.  Its current safe state is both rows with `writer=false`; B0-X stays
observation-only and has explicit recurring owner `DISABLED`.

If a future evidence-backed registry snapshot assigns a physical account,
`assert_single_writer` rejects more than one writer for the same physical
account (`app/services/mock_lane_registry.py:877-900`).  Its diagnostic masks
the account identifier, as exercised by
`tests/test_mock_lane_registry.py:679-696`.

No Binance recurring scheduler is enabled here.  No Alpaca crypto mutation
profile is selected here.  No submit/cancel wiring is introduced here.  Upbit
remains synthetic `SHADOW_ONLY`; Futures remains `DISABLED_NO_STRATEGY`.

## Single-currency DecisionIntent contract

The following authority is reproduced verbatim:

```text
A DecisionIntent is single-currency.

ExecutionPlan fan-out from one DecisionIntent is permitted only to lanes
whose registry quote_currency exactly equals the intent's
target_notional_currency.

USD and USDT are distinct currencies. No parity, FX lookup, or implicit
conversion is authorized.

When the same policy is evaluated on USD and USDT venues, the system must
materialize separate sibling DecisionIntents, one per currency, and bind
them through an immutable common comparison or policy-decision correlation.

For crypto.alpaca.paper.*, AUTO_MIRROR means policy mirror, not
same-DecisionIntent currency conversion.
```

### C2-1 — single-currency construction

`DecisionIntent` is a frozen, strict contract
(`app/schemas/execution_contracts.py:295-315`) with exactly one
`target_notional_currency: Literal["KRW", "USD", "USDT"]` field.  The
factory derives a server-owned immutable `decision_intent_id` from the frozen
intent projection (`app/services/mock_integration/lineage.py:377-405` and
`650-653`).  A DecisionIntent consequently has one, rather than a converted
or multi-currency, target-notional currency.

### C2-2 — exact fan-out guard

Two pre-I/O equality guards compose the permitted relationship:

1. `MockLineageFactory.create_execution_plan` rejects when
   `decision_intent.target_notional_currency != draft.quote_currency`
   (`app/services/mock_integration/lineage.py:520-526`).
2. `assert_lane_quote_currency` rejects when
   `plan.quote_currency != entry.quote_currency` before the broker boundary
   (`app/services/mock_lane_registry.py:1085-1097`).

Thus a plan can bind only when the intent currency, plan quote currency, and
the selected registry lane's quote currency are exact-equal.  A mismatch raises
`currency_conversion_not_authorized` or `lane_quote_currency_mismatch`; neither
guard performs conversion or I/O.

### C2-3 — no FX, parity, or implicit conversion path

The exhaustive search scope for this contract is the J1/J2B decision/plan
schema, its factory, the J2A registry guard, and their direct tests.  The
following command returned no matches (exit 1 is `grep`'s no-match result):

```text
$ grep -R -n -E -i --include='*.py' '(fx|parity|exchange[_-]?rate|usd[[:space:]_:/-]*to[[:space:]_:/-]*usdt|usdt[[:space:]_:/-]*to[[:space:]_:/-]*usd)' app/schemas/execution_contracts.py app/services/mock_integration/lineage.py app/services/mock_lane_registry.py tests/test_execution_contracts.py tests/services/mock_integration/test_lineage.py tests/test_mock_lane_registry.py
[exit 1; stdout empty]
```

That zero-result search is paired with the two explicit reject guards in
C2-2.  It is evidence for the stated enforcement surface only; unrelated
market-data FX readers are not a DecisionIntent-to-ExecutionPlan conversion
authority.

### C2-4 — USD/USDT sibling binding

The verbatim authority above defines the required immutable common comparison
or policy-decision correlation, but it supplies no field name, type, storage
location, or factory literal for that key.  The current frozen
`DecisionIntent` fields are listed at
`app/schemas/execution_contracts.py:301-315`; neither it nor
`LineageEnvelope` (`app/services/mock_integration/lineage.py:188-230`) defines
such a common sibling key.  The generic preview/lifecycle `correlation_id`
fields at `app/schemas/execution_contracts.py:143-206` are not a frozen
DecisionIntent sibling-binding definition.

`SIBLING_BINDING_FOR_EXECUTION = PENDING`.  Until an exact immutable key
contract is supplied, this artifact does not name, synthesize, or persist one;
it also does not permit a USD intent to fan out to a USDT lane (or the reverse).

### C2-5 — crypto Alpaca AUTO_MIRROR

For `crypto.alpaca.paper.default` and `crypto.alpaca.paper.clean`,
`AUTO_MIRROR` means the policy mirror stated verbatim above.  It cannot mean
same-DecisionIntent currency conversion: both rows are USD in the signed
matrix, both are `NOT_READY` with activation `DISABLED`, both have
`writer=false` and `auto_order_enabled=false`, and both have
`scheduler_owner=None` in the exact absent-owner sense defined above.

## Negative guarantees

This artifact performs and authorizes all of the following at zero:

- broker or network mutation;
- database migration, schema operation, or DML probe;
- TaskIQ, Prefect, cron, launchd, or systemd registration;
- deployment, service restart, canary, or account cleanup;
- any live path, default, environment, host allowlist, cap, or confirm-gate
  change; and
- any change to existing recurring or manual ownership semantics.

The contract changes no production `app/**` file and does not alter the
registry, test suite, models, migrations, broker adapters, or scheduler files.

## Verification anchors

- Registry values: `app/services/mock_lane_registry.py:442-510`.
- Unknown identity and duplicate-writer guards:
  `app/services/mock_lane_registry.py:773-784` and `877-900`.
- Currency contract and factory equality guards:
  `app/schemas/execution_contracts.py:295-376` and
  `app/services/mock_integration/lineage.py:510-526`.
- Signed registry regression test:
  `tests/test_mock_lane_registry.py`.

No execution, profile, writer, cadence, cap, canary, or FX value is selected by
this document.
