# ROB-845 — paper execution platform design

- **Issue:** ROB-845
- **Date:** 2026-07-13
- **Base:** `origin/main` `0b56669a`
- **Status:** approved for implementation

## Goal

Provide one default-off `paper_execution` MCP profile and one canonical paper-order
application boundary for the two proven V1 venues: Binance Spot Demo and Alpaca Crypto
Paper. An experiment caller must not select a broker-native mutation path, origin, or
idempotency key. It supplies a claimed intent; a trusted provenance verifier exact-binds
that intent before any adapter, native ledger mutation, or broker call.

This change deliberately preserves each broker-native ledger as the sole lifecycle,
fill, and P&L source. There is no common order table, copied lifecycle state, migration,
or live mutation surface.

## Current state and confirmed gaps

- `app/services/brokers/capabilities.py` is the single capability registry, but currently
  knows only KIS, Kiwoom, and Upbit.
- Existing MCP profiles expose venue-specific tools. Reusing `US_PAPER`, Binance demo,
  or broad execution registrars would expose more surface than the cohort runner needs.
- Binance Spot Demo has server-derived market-condition fail-close and a native ledger,
  but its executor is a complete BUY-open/SELL-close round trip, not a generic single-leg
  order port. Its root/close client IDs are currently random.
- Alpaca Paper has approval-packet, atomic claim, send-time freshness, live-position,
  sell-reservation, cancel, and broker-truth reconciliation boundaries. Packet creation
  remains in MCP tooling, and automated sell is intentionally disabled because no exact
  source-buy authority is currently enforced.
- ROB-849 will own cohort storage and canonical snapshot production. ROB-845 must expose
  a model-independent verifier Protocol and fail closed until a concrete verifier is
  injected; importing ROB-849 persistence here would invert ownership.

## Alternatives considered

### A. Canonical façade plus venue adapters — selected

The common application verifies provenance, resolves capabilities and an adapter, then
passes a frozen verified intent to the venue application boundary. It owns no order
state. This makes the caller-visible contract uniform while retaining the mature
venue-specific safety and ledger boundaries.

### B. Register existing MCP handlers in one profile — rejected

The handlers combine DTO mapping, profile policy, packet construction, and business
logic. A common profile over them would preserve caller-selectable venue details and
would make ROB-849 depend on tooling internals.

### C. Add paper flags only to the coarse capability registry — rejected

Capabilities alone cannot enforce provenance-before-side-effect ordering, server-owned
idempotency, or the native application boundaries. This would be a descriptive rather
than executable safety contract.

## Ownership and dependency direction

```text
paper_execution MCP tools
        |
        v
PaperExecutionApplication
  1. validate claimed intent
  2. require and call ExperimentProvenanceVerifier
  3. exact-bind returned evidence
  4. resolve capability + adapter
        |
        +--------------------------+
        |                          |
        v                          v
BinanceSpotDemoPaperAdapter   AlpacaCryptoPaperAdapter
        |                          |
guarded DemoScalpingExecutor  AlpacaPaperOrderApplication
BinanceDemoLedgerService      approval packet/coordinator
        |                          |
Binance native ledger         Alpaca native ledger
```

Allowed dependency direction is `tooling -> common application -> adapter -> existing
venue application/native ledger`. Adapters must not import MCP tooling. The common layer
must not import venue models or repositories.

## Common contracts

### Capability source of truth

Extend `app/services/brokers/capabilities.py`; do not create a second mapping under
`brokers/paper`.

- Add `Broker.BINANCE` and `Broker.ALPACA`.
- Add frozen `PaperBrokerCapabilities`, including venue, account mode, products,
  symbols, order types, time-in-force values, sizing modes, quote source, session/rate
  notes, `fill_model_known`, and support flags for every port operation.
- Define exactly two V1 entries:
  - Binance Spot Demo: `BTCUSDT`, `ETHUSDT`; BUY MARKET; notional sizing; preview,
    submit, get-order, and native-link supported; cancel and external reconcile
    unsupported. A submit is an internally reconciled open/close round trip and consumes
    two native ledger rows.
  - Alpaca Crypto Paper: `BTC/USD`, `ETH/USD`; BUY/SELL LIMIT; qty sizing; GTC/IOC;
    preview, submit, cancel, and get-order supported. Native lifecycle remains in
    `AlpacaPaperLedgerService`.
- Do not invent numeric provider rate limits. Record only limits actually enforced by
  the existing application path.

`app/services/brokers/paper/__init__.py` may re-export these types but must not redefine
or mirror the registry.

### Claimed and verified intent

The caller-facing `PaperOrderRequest` contains:

- intent ID;
- experiment, run, cohort, and strategy-version IDs;
- strategy, config, and policy hashes;
- venue, account mode, product, symbol, side, order type, time in force, qty/notional,
  and price;
- canonical market snapshot ID, hash, as-of, and source;
- for an experiment sell, an opaque claimed source-buy reference.

It intentionally does **not** contain `origin` or `idempotency_key`.

`ExperimentProvenanceVerifier.verify(request)` returns a frozen
`VerifiedExperimentProvenance`, not a boolean. The return exact-binds every required ID
and hash, canonical snapshot identity/as-of/source, reference price, a server-derived
decision identity, and (for a sell) the exact native source-buy client order ID.

The application compares every returned field with the request. Missing verifier,
missing fields, mismatch, stale/unusable evidence, or verifier failure returns a stable
fail-closed result before adapter resolution. Only then does it construct a frozen
`VerifiedPaperOrderIntent` with `origin="experiment"` and a deterministic server-derived
idempotency key.

The existing manual-smoke entrypoints remain separate and keep their current narrow
contracts. They are not routed through this experiment façade and callers cannot convert
an experiment request into manual origin.

### Risk snapshot and result

`PaperRiskSnapshot` is immutable evidence returned by preview: open exposure, reserved
notional, daily realized loss, quote/spread/data age/source/as-of, and policy
version/hash. It is descriptive evidence only; the common layer never persists it.

Every operation returns a typed `PaperOperationResult` with a stable status/reason,
venue-native identifiers/evidence, and `replayed` where relevant. Unsupported methods
always return `unsupported_capability` without constructing a client or touching a
native ledger.

### Port and registry

`PaperBrokerPort` declares `preview`, `submit`, `cancel`, `get_order`, `reconcile`, and
`link_native_order`. `PaperAdapterRegistry` contains adapters only and rejects duplicate
keys. Capability lookup stays in the shared capability module.

The application checks the advertised support bit before invoking a port method. A
capability and adapter method have a one-to-one contract test.

## Profile and MCP boundary

- Add `McpProfile.PAPER_EXECUTION = "paper_execution"`.
- Add `PAPER_EXECUTION_ENABLED: bool = False`.
- Selecting the profile with the flag off fails during startup before FastMCP tool
  registration. Authentication remains mandatory.
- `register_all_tools` handles this profile before the always-on block, registers only
  the paper façade registrar, and returns immediately. Direct registry calls with the
  flag off register nothing.
- The profile exposes capability/read plus typed preview/submit/cancel/get-order/
  reconcile tools. It does not expose generic/legacy/native Binance, Alpaca, KIS, Toss,
  Upbit, or Kiwoom mutation tools and does not expose caller-controlled native-link.
- With no production provenance verifier installed, capability reads work and all
  experiment mutation calls return `provenance_verifier_unavailable` with zero downstream
  calls. ROB-849 supplies the composition-root implementation later.

## Binance Spot Demo adapter

The current executor is retained as a round-trip strategy application, so the adapter
advertises only the behavior it can truthfully provide.

1. Accept only BTCUSDT/ETHUSDT, BUY, MARKET, and notional sizing.
2. Build Spot-demo market/reference clients and a dedicated risk limit allowlist; do not
   change the global scalping symbol defaults.
3. Derive root and close client IDs from the verified decision identity, within Binance's
   existing length constraints. Existing manual callers retain random IDs by default.
4. Extend the existing root reservation, under its PostgreSQL advisory lock, to look up
   the deterministic root ID before cap checks:
   - exact immutable metadata match + terminal native result -> replay without market
     fetch or POST;
   - exact match + in-flight lifecycle -> `idempotency_in_progress`;
   - same ID + different immutable metadata -> `idempotency_collision`;
   - absent -> existing cap checks and planned insert.
5. Persist the complete verified identity and deterministic close ID in native ledger
   metadata. The common layer stores no link row.
6. Call only the guarded `DemoScalpingExecutor`; adapter code may not call raw signed
   endpoints.

Cancel and external reconcile are stable unsupported operations. `get_order` and
`link_native_order` resolve deterministic native ledger evidence without mutation.

## Alpaca Crypto Paper adapter

Extract packet/application behavior from MCP tooling into
`AlpacaPaperOrderApplication`; existing handlers and the new adapter both consume it.
The extracted service owns packet creation/re-read, server-derived key, preview
persistence, token submission through `AlpacaPaperSubmitCoordinator`, broker-truth
cancel synchronization, and native read mapping. MCP files retain only registration,
input DTOs, feature gates, and response mapping.

The adapter narrows the existing crypto surface to BTC/USD and ETH/USD. It never imports
tooling or calls raw `submit_order`.

### Source-bound automated sell

Removing `automated_sell_disabled` alone is unsafe. A façade-origin sell is enabled only
with all of these checks:

1. Verified provenance supplies an exact native source-buy client order ID.
2. The application reloads that execution from `AlpacaPaperLedgerService` and verifies
   execution kind, BUY side, Alpaca paper account, exact symbol/product, reconciled filled
   lifecycle, and finite positive filled quantity.
3. Requested sell quantity is no greater than the source filled quantity. The existing
   USD 50 notional ceiling remains independently enforced.
4. The source identity and server decision hash are embedded in the persisted packet.
5. The coordinator validates the source-bound packet after terminal replay but before
   freshness/claim. Legacy source-less automated-sell tokens remain fail-closed.
6. Existing live-position read, broker-status reconciliation, PostgreSQL advisory lock,
   open-sell reservation, and send-time freshness checks remain authoritative.

Existing `US_PAPER` automated sell stays disabled unless it carries the same trusted
source authority; ROB-845 does not weaken legacy callers.

### Canonical signal venue

Add `binance_public_spot` to the existing signal-venue mapping with only
`BTCUSDT -> BTC/USD` and `ETHUSDT -> ETH/USD`. Existing Upbit mapping is unchanged. SOL
and unrecognized mappings fail closed. This is an additive evidence vocabulary change,
not a quote-provider implementation; ROB-849 owns production canonical snapshots.

## Failure ordering

For experiment mutation operations the required order is:

1. parse and validate the request;
2. require the verifier and exact-bind all provenance;
3. derive the immutable intent and server idempotency key;
4. resolve capability and reject unsupported operations;
5. resolve the adapter;
6. enter the venue application/native ledger boundary;
7. perform broker mutation only after all venue gates pass.

This ordering is tested with counters. Any failure in steps 1–4 must produce zero adapter,
client, and native-ledger calls.

## Tests

- Contract validation for claimed/verified intent, deterministic keys, risk evidence,
  duplicate adapter registration, and the unsupported matrix.
- Startup/profile tests for flag off/on, mandatory auth, exact allowlist, and zero live or
  venue-native mutation tools.
- Production adapter contract tests for every advertised method.
- Binance integration tests for deterministic sequential/concurrent replay, collision,
  in-flight behavior, terminal replay after stale data, native root/close links, symbol
  limits, and legacy random-ID behavior.
- Alpaca integration tests for valid source-bound sell and every malformed source case,
  independent quantity/notional ceilings, concurrent oversell, cancel reservation, and
  asynchronous-fill reconciliation.
- AST/import guards for raw submit calls, live imports, MCP-tooling imports from adapters,
  common ledger/model/migration additions, and duplicate profile/capability definitions.
- Existing ROB-841/842/844 and profile regression suites.

## Documentation and operations

Update `app/mcp_server/README.md` with the profile flag, auth, exact tool surface,
supported venues, and the deliberate verifier-unavailable behavior before ROB-849. The
rollback is to disable `PAPER_EXECUTION_ENABLED`; startup then fails and no façade tool is
registered. Existing native records and profiles are unchanged.

## Out of scope

- A unified order/fill/P&L ledger or any schema migration.
- Live broker mutations, futures, leverage, KIS/Kiwoom conformance, strategy evaluation,
  cohort scheduling, or promotion state.
- ROB-849 snapshot/cohort persistence or a fabricated production verifier.
- Broadening existing manual-smoke or `US_PAPER` contracts.

