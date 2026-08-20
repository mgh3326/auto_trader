# D2 one-shot remediation — `d2_remediation_single`

`operator_contract.yaml` names a writer for the
`binance-demo-remediation-20260820` exception. This runbook covers the code that
answers to that name.

**Scope of this document.** It describes the writer and how to rehearse it. It
does **not** authorize execution. Execution requires a separate operator
re-sign, and the runbook cannot supply one.

## 1. What it is

| | |
|---|---|
| Writer | `app/services/brokers/binance/spot_demo/d2_remediation_single.py` |
| CLI | `scripts/binance_spot_demo_d2_remediation.py` |
| Contract | `~/work/herdr-inbox/contract-d2-binance-remediation-v2.2-20260820.md` |
| Seal | r7 attempt-2, `pre_snapshot_hash=sha256:5ba70a81…3e6b898` |
| Surface | `binance_spot_demo_remediation_only` |
| Canary/strategy use | forbidden |

It can express exactly three orders:

```text
BTCUSDT  SELL LIMIT 0.00015000    @ 69266.01000000
ETHUSDT  SELL LIMIT 0.00520000    @  2248.56000000
USDCUSDT SELL LIMIT 5000.00000000 @     1.00072000
```

There is no fourth, no BUY, no MARKET, and no flag that selects a symbol,
quantity, or price. The set lives in `D2_BOUND_ORDERS`; the sealed payload is
re-read at runtime and must match it exactly.

## 2. Why a second entry point exists

`scripts/binance_spot_demo_smoke.py` is the ROB-298 BUY round-trip and does not
wire a SELL `--confirm`, so it cannot express these operations. The 2026-08-20
execution attempt correctly refused to reach around it with an ad-hoc script,
because that would have created an unreviewed execution surface. This module is
the reviewed alternative.

`tests/services/brokers/binance/spot_demo/test_spot_demo_submit_callers.py`
enumerates every caller of `BinanceSpotDemoExecutionClient.submit_order` and
fails on an unlisted one. Adding an execution surface is a reviewed change.

## 3. Inherited boundaries — none relaxed

| Boundary | Owner | State after this change |
|---|---|---|
| Host pinned to `demo-api.binance.com` | ROB-298 transport | unchanged; the writer re-asserts it before composing |
| `submit_order(..., confirm=True)` per-call gate | ROB-298 execution client | unchanged; the writer's own default is `confirm=False` |
| `BINANCE_SPOT_DEMO_ENABLED` | ROB-298 | unchanged, still default-off |
| Ledger writes via `BinanceDemoLedgerService` | ROB-298 | unchanged; the repository is never imported here |
| Lifecycle state machine | ROB-298 | unchanged; the writer only uses legal transitions |

Added on top (all tightening):

* `D2_REMEDIATION_SINGLE_ENABLED` — a second, independent, default-off gate.
* A closed three-order set bound to one `pre_snapshot_hash`.
* A J3A lease precondition, re-attested immediately before every submit.
* One dispatch per `client_order_id`, enforced by a claim set.
* Two independent post-dispatch proof epochs.

## 4. Rehearsal

`--plan-only` is pure: it binds the seal and prints the three request payloads
with no HTTP, no DB, no lease, and no signing. It needs no env gate because it
touches nothing.

```bash
uv run python -m scripts.binance_spot_demo_d2_remediation \
  --plan-only \
  --sealed-payload <path>/r7-snapshot/attempt-2/binding-payload-proposed.json
```

`--dry-run` walks the whole path — both env gates, host re-assertion, seal
binding, J3A lease acquisition and attestation, request composition, and the
non-mutating `POST /api/v3/order/test` shape check — and stops immediately
before the signed POST. It needs both gates armed and a reachable database.

```bash
BINANCE_SPOT_DEMO_ENABLED=true D2_REMEDIATION_SINGLE_ENABLED=true \
uv run python -m scripts.binance_spot_demo_d2_remediation \
  --dry-run --sealed-payload <path>/binding-payload-proposed.json
```

## 5. Execution — not authorized by this document

`--confirm` is the only mode that reaches `submit_order(..., confirm=True)`.
Before it may be run, all of the following must hold, and none of them is
established here:

1. a fresh operator re-sign bound to the exact r7 `pre_snapshot_hash`;
2. `operator_authorization` no longer `null` in the sealed object;
3. both env gates armed by the operator, deliberately, for this run;
4. the account-wide writer freeze in force per contract v2.1 §6.

## 6. Failure behaviour

* **Ambiguous submit** (timeout, reset, unclear response): one bounded
  readback of the order by its client id. Never a re-send — a duplicate order
  is the worst available outcome, so an unproven result is recorded as an
  anomaly and the run halts before the next order.
* **Broker echo mismatch**: a response describing a different symbol, side,
  type, or quantity is a failure, not cosmetic drift.
* **Lease lost**: refusal before the transport. Acquisition-time success is
  never carried forward.

## 7. Known limits

* **The J3A lease is not broker-enforced fencing.** Binance never sees it. An
  operator at a console or an out-of-repo process reaches the same shared Demo
  account without contending for it. That is why the writer also re-attests per
  submit and reads the *account-wide* open-order book in both proof epochs —
  and why neither of those closes the hole, only narrows it.
* **A resting SELL LIMIT is not a fill.** The writer records `submitted` and
  proves the resting state; it does not wait for or assert a fill.
* **No scheduler registration.** CLI-only, operator-driven, one shot.
