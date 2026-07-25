# ROB-867 Kiwoom Mock US Lifecycle Design

**Issue:** ROB-867

**Date:** 2026-07-13

**Status:** Approved for implementation planning

## Goal

Add a US-equity lifecycle for Kiwoom mock investment under the dedicated
`kiwoom_mock_us` account mode. The feature must support US account reads and
limit/market order lifecycle operations without weakening the existing KR
`kiwoom_mock` safety boundary or reusing its credentials.

The guiding rule is evidence-first capability exposure: the low-level client
may represent the documented Kiwoom request shape, but MCP exposes only order
types proven necessary for current consumers and safe to support now.

## Context and Evidence

- Kiwoom US mock uses `https://mockapi.kiwoom.com`, the same host as KR mock,
  but it has a separate app key, app secret, and account number.
- Operator read-only smoke on 2026-07-13 proved OAuth, `ust21070`, `ust21050`,
  `ust21110`, and `ust21160` return successful mock responses.
- The documented `ust31490` orderable-quantity TR returned `return_code=20`
  with `RC9000: 모의투자에서는 해당업무가 제공되지 않습니다.` Therefore a
  documented TR is not considered supported until mock evidence exists.
- Current champion-challenger consumers need only limit and market orders.
- The existing KR implementation in ROB-97/ROB-319 already provides the
  mock-host guard, confirm/dry-run mutation pattern, fail-closed broker response
  shaping, and cancel-in-finally smoke workflow to mirror.

## Scope

### Included

- US-only config and credential validation.
- A US-specific mock client factory with a distinct auth instance.
- US order operations for buy, sell, modify, and cancel.
- US account reads for open orders, today's orders/fills, positions, and USD
  deposit cash.
- Seven MCP tools under `account_mode="kiwoom_mock_us"`:
  - `kiwoom_mock_us_preview_order`
  - `kiwoom_mock_us_place_order`
  - `kiwoom_mock_us_modify_order`
  - `kiwoom_mock_us_cancel_order`
  - `kiwoom_mock_us_get_order_history`
  - `kiwoom_mock_us_get_positions`
  - `kiwoom_mock_us_get_orderable_cash`
- Operator smoke CLI with preflight, preview, full lifecycle, and explicitly
  confirmed advanced-order-type probing.
- MCP/profile/governance registration, README updates, and a dedicated runbook.
- Unit tests and opt-in live mock smoke. No database migration.

### Excluded

- Kiwoom live trading.
- Exchanges other than NYSE, NASDAQ, and AMEX.
- Broker-backed orderable quantity or margin estimates from `ust31490`.
- MCP exposure of VWAP, TWAP, LOC, MOC, STOP, or STOP LIMIT.
- Automated or scheduled order-type probes.
- Ledger/reconciliation persistence beyond raw broker lifecycle reads.
- A claim that class-share symbols such as `BRK.B` work before smoke evidence.

## Architecture

Use a separate US vertical slice instead of adding market branches to the KR
module:

- `app/services/brokers/kiwoom/us_orders.py`: documented US order payloads and
  client-side input validation.
- `app/services/brokers/kiwoom/us_account.py`: US account TR requests and raw
  response transport.
- `app/mcp_server/tooling/orders_kiwoom_us_variants.py`: US MCP guards,
  response envelopes, and tool registration.
- `scripts/kiwoom_mock_us_smoke.py`: operator-safe smoke workflow.
- `docs/runbooks/kiwoom-mock-us-smoke.md`: preconditions, safety boundaries,
  evidence table, and cleanup procedure.

The existing `KiwoomMockClient` transport remains the single mock-host-guarded
HTTP implementation. A sibling `KiwoomMockUsClient` factory constructs it with
US-only settings and its own `KiwoomAuthClient` instance. It never reads or
falls back to `KIWOOM_MOCK_APP_KEY`, `KIWOOM_MOCK_APP_SECRET`, or
`KIWOOM_MOCK_ACCOUNT_NO`.

Small broker-response helpers shared by KR and US MCP modules may move to a
focused `orders_kiwoom_shared.py` module. Public KR tool names, defaults, and
response fields must remain unchanged.

There is no external Redis token cache in the current Kiwoom implementation.
Credential separation is therefore enforced by distinct client/auth instances.
If a shared token cache is introduced later, its key must include the explicit
credential namespace (`kr_mock` versus `us_mock`).

## Configuration and Registration

Add default-disabled settings:

- `KIWOOM_MOCK_US_ENABLED=false`
- `KIWOOM_MOCK_US_APP_KEY`
- `KIWOOM_MOCK_US_APP_SECRET`
- `KIWOOM_MOCK_US_ACCOUNT_NO`

The base URL remains `KIWOOM_MOCK_BASE_URL`, but construction and every
resolved request continue to require exactly `https://mockapi.kiwoom.com`.

Registration behavior:

- DEFAULT profile: register US tools only when `KIWOOM_MOCK_US_ENABLED=true`.
- KIWOOM profile: register both KR and US namespaces; each namespace still
  fail-closes at call time when its own config is incomplete.
- Read-only/account profile forbidden sets include the new namespace.
- New mutation/preview tools enter the readonly deny-list and mutation
  classification.
- If present in DEFAULT, every new tool is classified in
  `route_request_lanes` so the registry-diff guard remains exhaustive.

## Order Model

### Low-Level Client

`KiwoomUsOrderClient` represents the documented request shape:

- Buy: `ust20000`, `/api/us/ordr`
- Sell: `ust20001`, `/api/us/ordr`
- Modify: `ust20002`, `/api/us/ordr`
- Cancel: `ust20003`, `/api/us/ordr`

It accepts `trde_tp` and optional `stop_pric` so future evidence-backed
capabilities and the explicit probe workflow do not require a transport
redesign. It validates symbol/order id/positive quantity/price shapes before
transport and formats USD prices without binary-float artifacts.

For the initial MCP surface:

| MCP order type | `trde_tp` | Price rule |
|---|---:|---|
| `limit` | `00` | Positive price required; formatted as a decimal string |
| `market` | `03` | Price omitted by the caller and sent as an empty string |

All other codes are rejected before client construction or network I/O with a
stable error envelope containing:

- `error_code="unsupported_trde_tp"`
- the rejected code
- `supported_trde_tp=["00", "03"]`

This allowlist lives in one constant so a later evidence-backed issue can
expand it without changing the public dispatch structure.

Modify exposes the documented US operation: original order number, symbol,
exchange, and new price. Cancel exposes original order number, symbol, and
exchange. Unlike the KR API, the captured US modify/cancel bodies do not invent
quantity fields absent from the documentation.

### Exchange and Symbol Mapping

MCP resolves active symbols through `us_symbol_universe` before any network
call and maps:

| Universe exchange | Kiwoom `stex_tp` |
|---|---|
| `NASDAQ` or `NASD` | `ND` |
| `NYSE` | `NY` |
| `AMEX` | `NA` |

Missing, inactive, or unsupported exchanges fail closed. The DB-standard dot
symbol is passed to Kiwoom unchanged initially. Class-share symbols are marked
unverified in the runbook until the smoke workflow records broker evidence.

## Account Reads and Cash Semantics

`KiwoomUsAccountClient` provides:

- `get_open_orders`: `ust21050`
- `get_positions`: `ust21070`
- `get_today_orders`: `ust21510`
- `get_us_deposit_detail`: `ust21160`
- an optional raw foreign-deposit method for `ust21110` when needed by smoke
  diagnostics, not as a required MCP dependency

`kiwoom_mock_us_get_order_history` accepts
`scope: Literal["open", "today"] = "open"`. The default supports the full
smoke's submit/pending/cancel lifecycle; `today` exposes execution history.

`kiwoom_mock_us_get_orderable_cash` does not call unsupported `ust31490`.
It parses `ust21160.d0_usd_fx_entr` as a decimal USD deposit when present and
returns:

- `cash`: normalized decimal value, or `null` when parsing is not proven
- `currency="USD"`
- `cash_source="ust21160.d0_usd_fx_entr"` or an explicit `*_unparsed` source
- `cash_semantics="deposit_not_broker_orderable"`
- `orderable_quantity_supported=false`
- a warning that the value is deposit evidence, not a broker-calculated
  per-symbol orderable amount

Preview calculates only requested notional. It does not synthesize margin,
orderable quantity, or a success claim based on the deposit value.

## Error Handling and Safety

- Success requires an explicit numeric `return_code == 0`.
- Missing, null, nonnumeric, or nonzero return codes are failures.
- Raw broker payload and `return_msg` remain available as evidence.
- Known mock capability refusal such as `return_code=20` plus `RC9000` is
  additionally classified as `error_code="capability_unsupported"`; it is not
  converted to success.
- Every mutation defaults to `dry_run=true`. Broker I/O requires both
  `dry_run=false` and `confirm=true`.
- Unsupported market, exchange, order type, unsafe order number, missing
  price, and invalid quantity are rejected before network I/O.
- Logs and CLI output never include app keys, secrets, access tokens, or account
  numbers. Preflight reports missing environment key names only.

## Smoke Workflow

The CLI has three normal modes:

1. `preflight`: validate env key presence and call proven read-only TRs.
2. `preview`: resolve symbol/exchange and render limit or market payload without
   broker mutation.
3. `full`: submit a conservative, operator-supplied distant limit order, verify
   it through `ust21050`, cancel it, and verify no open order remains.

`full` requires explicit confirmation. It runs a dry-run first, records the
returned nine-digit order number, and always attempts cancellation in a
`finally` block. Unparsed order ids, nonzero cancel results, or an order still
present after cancellation return exit code 2 with manual cleanup guidance.

Advanced order-type discovery is an optional preflight substep and is disabled
unless both `--probe-order-types` and `--confirm-probes` are supplied. The probe
calls the low-level client rather than the MCP surface, records each attempted
code and exact broker result, and immediately cancels every accepted order.
Sell-only types are skipped with an explicit prerequisite reason unless the
operator supplies a confirmed existing mock position. Market orders are not
part of automated full/probe defaults because immediate fill would defeat the
cancel-before-submit safety goal.

Probe evidence updates the runbook; expanding the MCP allowlist requires a
separate reviewed change.

## Testing Strategy

Implementation follows red-green-refactor TDD. Unit coverage includes:

- default-disabled config and exact missing-key reporting
- absolute non-fallback from US settings to KR credentials
- separate US auth/client instances and mock-host enforcement
- each US order/account TR id, path, and request body
- precise decimal price formatting and zero-padded response parsing
- DB exchange mapping and pre-network rejection paths
- MCP `00`/`03` behavior and advanced-code `unsupported_trde_tp` envelopes
- `dry_run`/`confirm` enforcement for every mutation
- fail-closed response shaping and `RC9000` capability classification
- honest deposit cash parsing and null/unparsed behavior
- DEFAULT/KIWOOM profile registration and governance drift guards
- CLI preflight, preview, full cleanup, and double-confirmed probe gates
- secret-redaction assertions

Focused Kiwoom tests, registry/governance tests, Ruff, and ty run before the
full non-live test gate. Live mock smoke remains operator opt-in and is never
run by default CI.

## Documentation and Operational Evidence

Update `app/mcp_server/README.md` with public tool contracts and the distinction
between KR `kiwoom_mock` and US `kiwoom_mock_us`. The runbook includes:

- required env key names
- supported MCP order types (`00`, `03`)
- known unsupported `ust31490` evidence
- preflight/full/probe commands
- cleanup and manual cancellation steps
- a per-TR and per-order-type evidence table
- a class-share symbol evidence row

No capability is described as supported solely because it appears in Kiwoom
documentation.

## Acceptance Criteria

1. US tools cannot read KR credentials or contact the Kiwoom live host.
2. Only `00` and `03` are exposed through MCP; all other codes fail before
   network I/O with the stable supported-list envelope.
3. Proven US account reads work through the dedicated client and preserve raw
   broker evidence.
4. `get_orderable_cash` never calls `ust31490` and does not mislabel deposit
   cash as a broker-calculated orderable amount.
5. Every mutation requires dry-run/confirm gates and the smoke lifecycle cannot
   silently strand an accepted order.
6. New tools are fully classified in registration, route, and readonly
   governance guards.
7. Unit, lint, type, and non-live regression gates pass; live mock results are
   recorded only when explicitly run by an operator.
