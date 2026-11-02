# KIS websocket replacement coverage, hk 2676

The operator requires coverage fixes before retiring `at-kis-ws`. This change
fixes domestic fillwire notification decoding in AT. It keeps `at-kis-ws`,
`websocket_monitor.py`, and `scripts/deploy-ncp-pull.sh` unchanged.

## Evidence and limits

The audit compared AT baseline `35d18093224492eb19e06ddcf4affe626acee230` with
[fillwire main f78adc936760abd982118e9127085b8353fe9d0e](https://github.com/mgh3326/fillwire/tree/f78adc936760abd982118e9127085b8353fe9d0e).
The local fillwire checkout was stale, so the audit used a GitHub archive pinned
to that SHA. No production container, configuration, or notification delivery
was inspected. The brief's statement that `at-kis-ws` is stopped is operator
context, not a live observation from this audit.

In the table, AT paths refer to that baseline unless marked as fixed here.
FW paths refer to the pinned fillwire SHA.

## Duties and replacement coverage

| Duty | Existing AT path | Replacement or gap |
|---|---|---|
| Domestic live and mock fills | `app/services/kis_websocket_internal/client.py:305-317`, `websocket_monitor.py:315-382` | FW `internal/reader/reader.go:227-231` selects the domestic live or mock TR. `internal/decode/notice.go:57-98` rejects non-fill notices before positive quantity and price checks in `decode.go:104-191`. Source coverage exists. Runtime cutover is unverified. |
| Overseas live and mock fills | `protocol.py:1-4`, `client.py:317`, `normalize_kis_fill` handle `H0GSCNI0` and `H0GSCNI9` | **Gap G1.** FW subscribes only to domestic TRs and emits `equity_kr` with `KRW`. Its `internal/decode/notice_test.go:209` classifies an overseas TR as unknown. The AT HTTP route can receive canonical US fills, but cannot replace an absent producer. Reconcile does not provide the same immediate websocket feed. |
| Durable ledger upsert | `websocket_monitor.py:440-520` uses the configured ledger sink | FW `internal/sink/sink.go:172-176` posts to AT. `app/routers/execution_ledger_ingest.py:160-239` uses the same repository key, per-item savepoints, and commit-before-downstream rule. No Go DB writer is needed. |
| Telegram and Discord fill notifications | `websocket_monitor.py:340-347,522-543`, shared `app/services/execution_ledger/fill_ingest.py` | **Gap G2, fixed here.** FW `decode.go:57-63,162-190` sends `{tr,fields,received_at}`. The old AT normalizer expected a parsed dict and returned `UNKNOWN`, zero quantity, and zero price without raising. That zero amount suppressed notification. The strict decoder now reads the domestic fields or raises. The shared threshold, enrichment, position link, and duplicate suppression remain in use. Actual message delivery is unverified. |
| Open-orders cache invalidation | Shared `fill_ingest.py:226-236,298-320`, including the Redis generation bump | The HTTP route calls the same downstream function after commit. Invalidation precedes notification suppression. Failures remain best-effort. |
| Redis execution pub/sub | The deployed command is `websocket_monitor.py --mode kis`, from `scripts/deploy-ncp-pull.sh:431,523` | This command does **not** publish `execution:{market}`. `kis_websocket_monitor.py:105-121` is a separate standalone publisher. FW's `fills:kis` Stream is not that pub/sub channel. Retiring the named container would not remove a pub/sub duty, but removing the standalone publisher would. |
| Approval-key coordination | Shared Python `app/services/kis_websocket_internal/approval_keys.py` | FW `internal/reader/approval.go` owns its session's approval-key behavior. Python cache and client modules still have smoke and mock-scalping consumers and cannot be deleted as dead code. |
| Heartbeat and liveness | Monitor heartbeat JSON every 5 seconds, periodic health log every 300 seconds, connection state, message and execution counters, last frame/fill/ping, notifier success, and sink counters | **Gap G3.** FW `README.md:30` explicitly excludes heartbeat reporting and an HTTP metrics endpoint. `internal/decode/counters.go:7-65` has pipeline counters, and the reader logs connections. Those do not provide the monitor's full liveness contract. |
| Error reporting | Monitor Sentry identity `auto-trader-kis-ws`, fatal capture, and error logs | **Gap G4.** FW has structured logs and optional Telegram operational alerts, described at `README.md:114-117`. No equivalent Sentry identity or verified alert configuration was established. Pipeline ACK success also does not prove Telegram or Discord fill delivery. |
| Shutdown and retry durability | Monitor sink flush and socket shutdown at `websocket_monitor.py:783-814` | FW's Stream pending reclaim and retry path (`internal/sink/sink.go:250-400`) keeps records until accepted statuses can be acknowledged. This covers domestic delivery at source level. It is not proof of runtime recovery or equivalence to the Python sink's DB fallback. |

Neither the existing unified monitor's KIS supervisor nor fillwire calls the
HTTP reconcile trigger on reconnect. That omission is not a duty lost by this
retirement. The separate standalone Redis publisher and shared Python KIS
modules remain outside the proposed removal.

## AT domestic notification fix

`normalize_kis_fill` recognizes the presence of `tr` or `fields` as a fillwire
frame. `app/services/fill_notification_kis_frame.py` reads the official domestic
indices for `H0STCNI0` and `H0STCNI9`. It requires a proven fill, an order ID,
a six-digit symbol, a known side, positive finite price and integral quantity,
and a valid clock with a timezone-aware `received_at`.

The clock uses the received KST date and fillwire's previous-day rule for
midnight arrivals. Duplicate-hint metadata does not change fill identity.
The existing parsed-dict and canonical-only paths remain available.

Malformed, unsupported, and non-fill frames raise `KisFillwireFrameError` with
no raw values in the error. `_rebuild_fill_order` does not hide these errors
with canonical fallback. The existing route catches downstream failure after
the ledger commit and logs an error. The ledger remains accepted and durable;
this change does not quarantine, reject, or roll back an already committed row.
Replay still suppresses duplicate notifications, so this fix does not resend
historical alerts that were missed before the fix.

`tests/routers/test_execution_ledger_ingest_fillwire.py` contains the original
offline reproduction as a regression test. Its synthetic official 26-field
fixture matches fillwire's wire shape and contains no production account data.
Before the fix, the two positive controls passed and the fillwire case failed:

```text
AssertionError: assert ('UNKNOWN', 0.0, 0.0, False) == ('005930', 10.0, 70000.0, True)
49 failed, 2 passed
```

The regression checks both normalization and HTTP reconstruction, the actual
shared notifier call with a mocked transport, non-fill rejection, malformed
frames, and KST date handling. It is registered in `ci_shards/shard-4.txt`.
The external handoff report records the full test commands and current results.

Local validation passed 233 affected unit tests, including the 57 new regression
cases and the existing monitor, sink, notification, ingest-auth, and shard-plan
tests. Twenty DB integration cases were deselected. `make lint` passed Ruff,
format checks, and ty for the full configured paths. These checks do not prove
production message delivery or DB integration behavior.
The full non-live collection and shard check passed for 2,054 test files with
zero missing, duplicate, or unexpected files, using an explicit localhost test
DSN for the collection guards.

## Separate fillwire task, hk 2680

The operator assigned G1 and G3 to a separate fillwire task. Retirement remains
blocked until that work and the remaining operational checks are resolved.
The required work is:

1. Subscribe to `H0GSCNI0` for live or `H0GSCNI9` for mock alongside the matching
   domestic TR on the **same** KIS websocket session. There must be no second
   session for the same app key.
2. Decode the overseas layout separately. Cover filled and partial executions,
   reject accept/cancel/reject notices, preserve order identity and deterministic
   fill sequence, and emit canonical US symbol, venue, USD price, quantity, and
   timestamps. Tests must compare real-shaped synthetic frames with the Python
   overseas parser, including the back-office KST clock and malformed frames.
3. Verify AT notification reconstruction for the new overseas raw shape. AT's
   decoder in this PR explicitly rejects overseas TRs. A coordinated AT change
   must add that support, or the producer must use the supported canonical-only
   notification path. Domestic field indices cannot be reused for overseas.
4. Publish observable liveness without secrets or raw frames. The record must
   distinguish connected, disconnected, stopped, and stale states and expose
   last frame, last execution, last ping, reconnect state, receive counters,
   pending delivery, successful ingest, and ingest failure. Persistence or an
   endpoint, stale thresholds, and the consumer must be explicit and tested.
5. State how notifier-success observations and G4 error alerts are monitored.
   An ingest ACK proves ledger acceptance, not notification delivery. Any metric
   without a replacement remains a named gap for the operator to resolve.
6. Test reconnect, shutdown, stale liveness, unsupported frames, retry, and
   duplicate suppression. Pin the deployed image and verify one active session,
   KR and US ingest, and the required observation window before any retirement.

This report authorizes no deploy, migration, scheduler, broker order, or
container change. It records the coverage decision and the remaining work.
