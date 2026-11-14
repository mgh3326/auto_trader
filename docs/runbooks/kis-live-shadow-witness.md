# KIS live shadow witness (Phase 1)

Phase 1 is an audit-only, no-broker-egress witness. It is disabled by default:
`KIS_LIVE_SHADOW_WITNESS_ENABLED=false`. When the operator separately deploys and arms
it, the client sends immutable KIS live limit-order observations only to the
loopback `EDGE_WITNESS_URL` (default `http://127.0.0.1:8080`). No credential or
authorization header is used. This PR does not deploy, activate, or contact it.

Witness I/O is background-only. Intent and echo failures, invalid configuration,
timeouts, and edge errors do not wait for or change the KIS broker call. A missing
echo means an observation was not recorded; it is not evidence that an order did
not execute.

A rejection the broker answered as the order's final answer is recorded too. The
domestic order path captures only the response that ends the call, so a
token-expiry or gateway-throttle rejection that the caller re-POSTs is never
echoed ahead of the final answer. When KIS answers with a non-`0` `rt_cd` plus
string `msg_cd` and `msg1` but no order number (rejections carry no
`output.ODNO`), the echo is sent with `ODNO: ""`. The v1 echo schema requires a
string and the edge stores the echo as a shadow witness only (its own
`kis_live_witnesses` row, never a command receipt), so such an echo stops counting
as `missing_echo` and cannot be read as an accepted order. An `rt_cd` of `0`
without an order number is still dropped. An absent or `null` `ODNO` is treated as
no order number, so the rejection above echoes `""`; any other non-string `ODNO`
is reported instead of being replaced. A rejection is echoed only when the send
outcome is `NOT_CREATED` (the broker proves no order was created); a 5xx body
keeps the outcome unknown and is not echoed, so its receipt stays `missing_echo`.
When an echo field is not a string, or the echo fails schema validation,
`kis_live_witness_echo_invalid` carries the offending comma-separated field names
and never their values.

Use the manual reconciler after the separate activation:

```sh
ENV_FILE=/dev/null uv run --no-env-file python -m scripts.kis_live_witness_reconcile
```

It queries `GET /v1/commands?scope=kis_live&missing_echo=true`, prints receipts,
and exits 0 when none are missing, 1 when any are missing, and 2 on query or
response errors. The actual empty response is `{"witnesses":null}`; a missing or
malformed `witnesses` field is an error, never a clean result.

Phase 1 evidence requires at least seven days with `missing_echo=0`. Disabled
collection, no observations, and reconcile failures are not success evidence.
Phase 2 requires separate approval; it is the only phase that could consider any
broker-egress design.
