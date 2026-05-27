# KIS mock scalping quote WebSocket smoke (ROB-321 PR2)

Read-only verification that the KIS quote/orderbook WebSocket connects, issues
the correct (account-mode-aware) approval key, subscribes, and delivers parsed
real-time ticks/orderbook snapshots. **No orders, no mutation, no Redis publish.**

- **Script:** `scripts/kis_mock_scalping_ws_smoke.py`
- **Client:** `app/services/brokers/kis/mock_scalping_ws/market_stream.KISQuoteWebSocket` (read-only; no order method)
- **Gate:** `KIS_MOCK_SCALPING_WS_ENABLED=true` (default off → no-op, exit 0)

## Safety boundary

- The quote WS client has **no order surface** (enforced by `tests/brokers/kis/mock_scalping_ws/test_import_guard.py` — the package may not import any order/ledger/execution-mutation module).
- Host separation is fail-closed: the URL is built from `WEBSOCKET_ENDPOINT_HOSTS[account_mode]` (`ops.koreainvestment.com:21000` live / `:31000` mock) and asserted against that allowlist.
- The approval key is issued via the account-mode-aware path (`approval_keys.get_approval_key(account_mode)`): live uses `openapi.koreainvestment.com:9443` + `KIS_APP_KEY/SECRET`; mock uses `openapivts.koreainvestment.com:29443` + `KIS_MOCK_APP_KEY/SECRET` and fails closed (env-name-only error) when mock config is missing.

## Prerequisites

- Approval-key credentials for the chosen `--account-mode`:
  - `kis_mock`: `KIS_MOCK_ENABLED=true`, `KIS_MOCK_APP_KEY`, `KIS_MOCK_APP_SECRET`, `KIS_MOCK_ACCOUNT_NO`.
  - `kis_live`: `KIS_APP_KEY`, `KIS_APP_SECRET`.
- Redis reachable (approval-key cache).

## Run

```bash
# disabled by default → no-op, exit 0
uv run python -m scripts.kis_mock_scalping_ws_smoke

# enabled, mock environment
KIS_MOCK_SCALPING_WS_ENABLED=true uv run python -m scripts.kis_mock_scalping_ws_smoke \
    --account-mode kis_mock --symbols 005930,000660 --max-events 5 --max-seconds 30
```

### Exit codes

| code | meaning |
|------|---------|
| 0 | success (or disabled no-op) |
| 1 | unexpected exception |
| 2 | subscription ACK failure |
| 3 | connection not established |
| 4 | connected/subscribed but no quote events arrived in the window |

## OPEN QUESTION this smoke resolves

Does the KIS **mock** WS (`:31000`) serve real-time quotes, or must quotes come
from the **live** WS (`:21000`)? Run both and record the result here:

```bash
KIS_MOCK_SCALPING_WS_ENABLED=true uv run python -m scripts.kis_mock_scalping_ws_smoke --account-mode kis_mock --max-events 5 --max-seconds 30
KIS_MOCK_SCALPING_WS_ENABLED=true uv run python -m scripts.kis_mock_scalping_ws_smoke --account-mode kis_live --max-events 5 --max-seconds 30
```

- Exit 0 with `ticks>0`/`books>0` → that host serves quotes.
- Exit 4 (no events) → that host does not serve quotes during the window; use the other.

**Result (fill in after running):**

| account_mode | ticks | books | verdict |
|--------------|-------|-------|---------|
| kis_mock     | 2     | 2     | **OK** — 2026-05-27 10:44 KST, KRX regular session, `005930,000660`; mock WS (`:31000`) delivered both orderbook and trade frames. |
| kis_live     | _not run_ | _not run_ | Not required for ROB-321 domestic mock smoke once `kis_mock` quote host was confirmed. |

> Note: also confirm the parsed `last_price`/`bid`/`ask` look sane against a known
> quote. If a field is consistently wrong, the documented `H0STCNT0`/`H0STASP0`
> field indices in `quote_protocol.py` need a one-line adjustment.

## Caveats

- Run **during KRX market hours** — outside trading hours the stream is idle and the smoke will return exit 4 (no events) even on a healthy connection.
- This is the market-data half only. Orders remain a separate mock-only REST path (PR1 guard + PR4 executor) and are never reachable from this client.
