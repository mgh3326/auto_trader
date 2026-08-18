# ROB-1266 — US lane readiness and evidence contract

## Scope and authority

This is a contract-only artifact (J5A).  It consumes the signed registry
without changing it and authorizes no broker, network, database, scheduler,
deployment, credential, or account operation.  It creates no policy, assigns
no writer, proposes no cadence, and grants no canary.

The controlling registry is
`app/services/mock_lane_registry.py::CANONICAL_LANE_REGISTRY`.  The four US
rows recorded below are a literal consumption of that registry.  Where this
document and the registry disagree, **the registry is authoritative** and this
document is the defect.

`role` is a purpose-only registry value.  It is not execution authority, and
it is independent of `writer`, `auto_order_enabled`, `activation_status`, and
`scheduler_owner`.  In particular `us.alpaca.paper.default` carries
`PRIMARY_AUTO` *together with* `AUTO_READY_BLOCKED_BY_POLICY`, `writer=False`,
`auto_order_enabled=False`, and an absent scheduler owner; those five facts are
fixed as one unit and may not be quoted apart from each other.

## 1. Document-level fixed tokens

```text
SIGNED_SOURCE = app/services/mock_lane_registry.py::CANONICAL_LANE_REGISTRY @ e057941425d2ea7d35a36ebf6074a6c70eba3013
SEPARATION_PROVEN = NO (physical_account_fingerprint missing)
```

Each token appears exactly once in this document.  `SEPARATION_PROVEN` is the
machine-checkable form of the standing override: **physical account separation
is not claimed while every US row carries `physical_account_id` absent.**  See
§6.

## 2. Record shape

Each US lane is recorded as a `### LANE <lane_id>` heading followed by exactly
two fenced blocks, in this order:

1. a **REGISTRY** block whose first line is `# REGISTRY`, and
2. a **DERIVED** block whose first line is `# DERIVED`.

The two are separate because they have different provenance.  REGISTRY holds
registry dataclass fields only; DERIVED holds values computed from those
fields.  Mixing them would make neither key set closed.

- The REGISTRY block's key set equals the **full** field-name set of
  `LaneRegistryEntry` — every field, no extras, no omissions.  It is not a
  subset.  As of 2026-08-18 that is 34 fields, but the field list itself is the
  contract, not the count.
- The DERIVED block's key set is exactly `{CAP_STATUS}` — nothing else.  No
  optional keys are permitted, because an optional key cannot be held to a
  closed-equality check.

Each body line is `<key> = <rendered value>`.  Keys do not repeat within a
block.

## 3. Value rendering rule

A recorded value is `render(v)` of the registry attribute, where `render`
dispatches on type in exactly this order:

1. `v is None` → `None`
2. `bool` → `False` / `True` — **before `int`**, because `bool` is a subclass
   of `int`
3. `Enum` → `v.value` — **before `str`**, because the registry uses `StrEnum`
   members (`AUTO_MIRROR`, `NOT_READY`, `BLOCKED`, `mock`, `paper`, `USD`, …)
4. `str` → the string as-is
5. `tuple` → `()` when empty, otherwise `"(" + ", ".join(render(x)) + ")"`
6. dataclass → `ClassName(f1=render(v1), f2=render(v2))`, field names and order
   taken from `dataclasses.fields(v)`, separator `", "`.  This rule exists for
   `policy_binding: PolicyBinding | None`; all four US rows currently record it
   as absent, and no non-absent spelling may be invented here.
7. `int` / `Decimal` → `str(v)`
8. anything else → **no fallback**.  The checker raises rather than inventing a
   representation.

Reordering steps 2 and 3 against steps 4 and 7 silently corrupts the record, so
the order above is normative.

## 4. Cap status rule

`CAP_STATUS` is derived from the registry alone, from exactly three fields:
`max_order_notional`, `max_orders_per_session`, `max_open_orders`.

- All three absent → `MISSING`.
- Any one of them bound → not `MISSING`.

This is an if-and-only-if.  Policy-specific daily, concurrent, or loss caps are
**not** recorded here: no approved policy exists for any US lane, so no such
literal exists to quote.  Inventing one is forbidden; the honest record is
`MISSING`.  All four US rows are `MISSING` today, and each additionally carries
`cap` inside `missing_bindings`.

## 5. US capability gap evidence

Linear ROB-1266 ① asks what is *impossible* today, with evidence.  The
following gaps are read from merged code at
`994b60c90abb7b88fa253acb8b986c532b3967c3`.  Each is a readiness blocker in its
own right, independent of the policy and cap blockers above.

### 5.1 `us.kis.mock` — no open-order truth in mock mode

`KISOverseasOrders.inquire_overseas_orders`
(`app/services/brokers/kis/overseas_orders.py:393`) fails closed for the mock
lane at `app/services/brokers/kis/overseas_orders.py:427-431`:

> `KIS overseas pending-orders inquiry (TTTS3018R) is not available in mock mode.`

The overseas pending-order (미체결) inquiry is the primary source of
open-order truth for this lane, and it raises before any transport when
`is_mock=True`.  Any downstream rule of the form "stop unless open order count
is known" therefore cannot be satisfied from this endpoint at all — it is an
absent capability, not a transient error.  This is recorded as UNKNOWN; it is
not evidence that the count is zero.

### 5.2 `us.kis.mock` — USD buying power is not available from integrated margin

`KISAccount.inquire_integrated_margin`
(`app/services/brokers/kis/account.py:809`) raises for the mock lane at
`app/services/brokers/kis/account.py:836-840`:

> `KIS integrated margin is not supported in mock mode; use inquire_domestic_cash_balance(is_mock=True) instead.`

That endpoint's documented response is where `usd_ord_psbl_amt` (달러
주문가능금액) and `usd_balance` come from, so the integrated-margin USD surface
is unavailable to this lane.  A mock-scoped alternative does exist —
`KISAccount.inquire_mock_overseas_buyable_amount`
(`app/services/brokers/kis/account.py:761`) — so the honest statement is
**"the integrated-margin USD surface is unavailable; a separate mock-only
buyable-amount endpoint exists"**, not "USD buying power is unobtainable".
Which of the two a future lane consumes is not decided here.

### 5.3 `us.kiwoom.mock` — US equity is not a declared broker market

`BROKER_CAPABILITIES[Broker.KIWOOM]`
(`app/services/brokers/capabilities.py:74-79`) declares
`markets=frozenset({Market.KR_EQUITY})` and `supports_live=False`.
`Market.US_EQUITY` is **absent** from that set.

A US order/account surface nevertheless exists in the tree
(`app/services/brokers/kiwoom/us_client.py`, `us_orders.py`, `us_account.py`,
plus `app/mcp_server/tooling/orders_kiwoom_us_variants.py`).  The gap is
therefore a *disagreement* between the capability registry and the shipped
surface, not a simple absence.  This contract records the capability registry
as it stands and does not reconcile the two; reconciliation would be a
production change and is out of scope for J5A.

Consistent with the standing decision, `us.kiwoom.mock` is
`BROKER_REGRESSION` for this epoch.  Promotion to `AUTO_MIRROR` requires a
separate approved autonomous policy and is not authorized here.

### 5.4 All four US lanes — no executable US-equity paper capability row

`PAPER_BROKER_CAPABILITIES` (`app/services/brokers/capabilities.py:91-136`)
contains exactly two entries: `Broker.BINANCE` (`market=Market.CRYPTO`) and
`Broker.ALPACA` (`market=Market.CRYPTO`, `symbols={"BTC/USD", "ETH/USD"}`).

There is **no `Broker.KIS` entry, no `Broker.KIWOOM` entry, and no
`Market.US_EQUITY` entry at all**.  `get_paper_capabilities(Broker.ALPACA)`
returns the crypto row, which is not a US equity capability and must not be
read as one.  Consequently none of the four US lanes has an executable
capability description covering products, symbols, sides, order types, time in
force, or sizing modes.  Every such value is UNKNOWN, and the correct
per-lane conclusion is the registry's own `allowed_order_types = ()` and
`allowed_time_in_force = ()` — empty, meaning *nothing is authorized*, not
*anything is allowed*.

### 5.5 Rate-limit fact carried forward, not a readiness claim

The official KIS mock (VTS) host enforces one admitted REST request per second
across every call for a given account/app-key scope, enforced at the dispatch
boundary by `app/services/brokers/kis/vts_distributed_gate.py`.  This is
recorded because it bounds any future evidence-gathering cadence on
`us.kis.mock`.  It is not a readiness signal in either direction.

## 6. Why separation is not proven

All four US rows record `physical_account_id` as absent,
`identity_status = UNKNOWN`, `fingerprint_evidence_ref` absent, and carry
`physical_account_fingerprint` in `missing_bindings`.

What the registry does establish is that the four lanes are declared under
**distinct credential namespaces** — `KIS_MOCK_*`, `KIWOOM_MOCK_US_*`,
`ALPACA_PAPER_*`, `ALPACA_PAPER_LAB_*` — and, for the two Alpaca rows, distinct
`profile_variant` values (`default`, `lab`) over a shared
`paper-api.alpaca.markets` host allowlist.

Namespace and profile distinctness is a **declaration**, not a measurement.  No
broker-observed account fingerprint has been recorded for any of the four rows,
so it remains unproven that two declared namespaces do not resolve to the same
physical account.  The two Alpaca rows are the sharpest case: they differ only
by credential namespace and profile variant on the same host.

Therefore the token in §1 is fixed at `NO`, and the strongest supportable claim
is: *the profiles and credential namespaces are declared separately.*  Any
stronger claim requires masked physical-account fingerprint evidence that does
not exist today.  Until then all four rows stay `writer=False`,
`auto_order_enabled=False`, and activation-blocked.

## 7. What this document does not do

- It does not modify `app/services/mock_lane_registry.py` or any production
  module.  If a registry row disagrees with the signed values, the fix is a
  serial J2A follow-up, not an edit from here.
- It does not create, approve, or imply a policy; `policy` remains in
  `missing_bindings` for all four rows.
- It does not assign a scheduler owner.  An absent `scheduler_owner` means
  **owner absent; mutation-ineligible; no authority to bind one downstream**.
  It is not an alternative spelling of `DISABLED`, which is a distinct explicit
  enum member used by other lanes.
- It does not authorize a canary; `canary` remains in `missing_bindings`.
- It records no FX rate, parity, or currency conversion.  All four rows are
  `quote_currency = USD`, and USD is never treated as interchangeable with KRW
  or USDT.

## 8. Lane records

The four blocks below are the contract surface.  Values are `render()` of the
signed registry entry as defined in §3.

### LANE us.kis.mock

```text
# REGISTRY
lane_id = us.kis.mock
market = us
broker = kis
account_profile = mock
profile_variant = None
account_mode = mock
lane_type = mock
quote_currency = USD
role = AUTO_MIRROR
role_pending_reason = None
role_on_policy_approval = None
lane_status = NOT_READY
activation_status = BLOCKED
activation_reason = NOT_READY
policy_binding = None
execution_mode = None
scheduler_owner = None
timing_owner = None
writer = False
auto_order_enabled = False
max_order_notional = None
max_orders_per_session = None
max_open_orders = None
allowed_order_types = ()
allowed_time_in_force = ()
endpoint_class = mock
reconcile_required = None
credential_namespace = KIS_MOCK_*
allowed_hosts = (openapivts.koreainvestment.com:29443)
physical_account_id = None
identity_status = UNKNOWN
fingerprint_evidence_ref = None
canary_binding = None
missing_bindings = (physical_account_fingerprint, policy, cap, owner, canary)
```

```text
# DERIVED
CAP_STATUS = MISSING
```

### LANE us.kiwoom.mock

```text
# REGISTRY
lane_id = us.kiwoom.mock
market = us
broker = kiwoom
account_profile = mock
profile_variant = None
account_mode = mock
lane_type = mock
quote_currency = USD
role = BROKER_REGRESSION
role_pending_reason = None
role_on_policy_approval = None
lane_status = NOT_READY
activation_status = BLOCKED
activation_reason = NOT_READY
policy_binding = None
execution_mode = None
scheduler_owner = None
timing_owner = None
writer = False
auto_order_enabled = False
max_order_notional = None
max_orders_per_session = None
max_open_orders = None
allowed_order_types = ()
allowed_time_in_force = ()
endpoint_class = mock
reconcile_required = None
credential_namespace = KIWOOM_MOCK_US_*
allowed_hosts = (mockapi.kiwoom.com)
physical_account_id = None
identity_status = UNKNOWN
fingerprint_evidence_ref = None
canary_binding = None
missing_bindings = (physical_account_fingerprint, policy, cap, owner, canary)
```

```text
# DERIVED
CAP_STATUS = MISSING
```

### LANE us.alpaca.paper.default

```text
# REGISTRY
lane_id = us.alpaca.paper.default
market = us
broker = alpaca
account_profile = paper
profile_variant = default
account_mode = paper
lane_type = paper
quote_currency = USD
role = PRIMARY_AUTO
role_pending_reason = None
role_on_policy_approval = None
lane_status = AUTO_READY_BLOCKED_BY_POLICY
activation_status = BLOCKED
activation_reason = AUTO_READY_BLOCKED_BY_POLICY
policy_binding = None
execution_mode = None
scheduler_owner = None
timing_owner = None
writer = False
auto_order_enabled = False
max_order_notional = None
max_orders_per_session = None
max_open_orders = None
allowed_order_types = ()
allowed_time_in_force = ()
endpoint_class = paper
reconcile_required = None
credential_namespace = ALPACA_PAPER_*
allowed_hosts = (paper-api.alpaca.markets)
physical_account_id = None
identity_status = UNKNOWN
fingerprint_evidence_ref = None
canary_binding = None
missing_bindings = (physical_account_fingerprint, policy, cap, owner, canary)
```

```text
# DERIVED
CAP_STATUS = MISSING
```

### LANE us.alpaca.paper.lab

```text
# REGISTRY
lane_id = us.alpaca.paper.lab
market = us
broker = alpaca
account_profile = paper
profile_variant = lab
account_mode = paper
lane_type = paper
quote_currency = USD
role = None
role_pending_reason = policy_absent
role_on_policy_approval = AUTO_CHALLENGER
lane_status = AUTO_READY_BLOCKED_BY_LIFECYCLE
activation_status = BLOCKED
activation_reason = AUTO_READY_BLOCKED_BY_LIFECYCLE
policy_binding = None
execution_mode = None
scheduler_owner = None
timing_owner = None
writer = False
auto_order_enabled = False
max_order_notional = None
max_orders_per_session = None
max_open_orders = None
allowed_order_types = ()
allowed_time_in_force = ()
endpoint_class = paper
reconcile_required = None
credential_namespace = ALPACA_PAPER_LAB_*
allowed_hosts = (paper-api.alpaca.markets)
physical_account_id = None
identity_status = UNKNOWN
fingerprint_evidence_ref = None
canary_binding = None
missing_bindings = (physical_account_fingerprint, policy, cap, owner, canary)
```

```text
# DERIVED
CAP_STATUS = MISSING
```
