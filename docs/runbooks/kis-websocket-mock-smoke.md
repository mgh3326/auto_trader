# KIS WebSocket Mock Smoke

ROB-104. Bounded smoke check for the KIS mock websocket subscription path.

## Purpose

Verify that on a given host:
- `KIS_WS_HTS_ID` is present in env.
- The KIS mock approval key can be issued / cached.
- A mock TR subscription handshake (`H0STCNI9` + `H0GSCNI9`) returns ACK `rt_cd=0`.

This check **does not**:
- Place orders (mock or live).
- Listen for fills.
- Publish anything to Redis.
- Validate the live websocket path. Live is a separate runtime concern (see "Runtime separation").

## Run

```bash
uv run python -m scripts.kis_websocket_mock_smoke
```

Exit codes:
- `0` — smoke OK
- `1` — unexpected error
- `2` — subscription ACK failure (see logged `tr_id` / `msg_cd` / `msg1`)
- `3` — websocket connection failure
- `4` — `KIS_WS_HTS_ID` unset

## Interpreting failure

| Exit | Likely cause | Next check |
|---|---|---|
| 4 | Missing `KIS_WS_HTS_ID` in `.env` | Set HTS user id on this host |
| 3 | Network blocked to `ops.koreainvestment.com:31000` | Check egress / firewall |
| 2 | Approval key invalid / HTS id mismatch / KIS-side outage | Inspect `msg_cd`; reissue approval key; check KIS status page |
| 1 | Bug or transient — re-run with `LOG_LEVEL=DEBUG` | Capture traceback for triage |

## Runtime separation (MacBook server)

Live and mock KIS websocket monitors should run as **separate** processes/units, never sharing the same `KISExecutionWebSocket` instance:

- Live: `KIS_WS_IS_MOCK=false` → `account_mode=kis_live`, port 21000.
- Mock: `KIS_WS_IS_MOCK=true` → `account_mode=kis_mock`, port 31000.

Both processes emit events tagged with `broker="kis"`, `execution_source="websocket"`, and the appropriate `account_mode`, so downstream consumers (Redis subscribers, reconciler) can route by mode without re-deriving from `tr_code`.

This runbook covers code-readiness only. **launchd/systemd unit definitions for the MacBook server are out of scope for ROB-104** (see Linear issue "Non-goals").

## Related

- ROB-100 / PR #670 — `app.schemas.execution_contracts` foundation
- `app/services/kis_websocket_internal/events.py::build_lifecycle_event`
- `kis_websocket_monitor.py` — production runtime entry point
