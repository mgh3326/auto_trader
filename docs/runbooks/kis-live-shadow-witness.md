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
