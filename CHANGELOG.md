# Changelog

## [0.2.0] - 2026-02-12
### Changed
- **Breaking**: `get_open_orders` tool removed. Replaced by `get_order_history(status="pending")`.
- **Breaking**: `get_order_history` updated to v2 spec.
  - Added `status`, `order_id`, `side` arguments.
  - Retained `market` argument as optional hint.
  - Default `limit` changed to 50.
  - `symbol` became optional if `status="pending"`.
  - `days` became optional (no longer defaults to 7).

### Added
- `truncated` boolean field in `get_order_history` response.
- `total_available` integer field in `get_order_history` response.

## Unreleased

### Fixed (ROB-302 — Binance Futures Demo smoke: credential alias, v2 preflight, XRPUSDT sizing)
- Futures Demo preflight now calls `GET /fapi/v2/account` instead of `/fapi/v1/account`. `demo-fapi.binance.com` returns `404` for v1; v2 returns the same redacted summary fields (`canTrade`, nonzero asset/position counts) so evidence shape is unchanged.
- `scripts/binance_futures_demo_smoke.py` now selects the requested symbol's row from the `exchangeInfo` response instead of `symbols[0]`. demo-fapi does not honor the `symbol=` query param and can lead the array with BTCUSDT, so XRPUSDT was being sized against BTCUSDT's step/precision/min-notional (cap-10 falsely blocked at `MIN_NOTIONAL=50`). The requested symbol is now matched and the helper fails closed if it is absent.
- The submitted MARKET quantity is quantized to the symbol's `quantityPrecision` on **both** the open leg and the reduceOnly close leg. A step-floored `Decimal` carried the `exchangeInfo` step string's trailing zeros (`"0.10000000"` → `"30.00000000"`) which `format(qty, "f")` emitted verbatim, triggering Binance `-1111 Precision is over the maximum`. The close leg sizes from `abs(positionAmt)` and is now quantized identically, so a confirmed open can no longer be left with a failing close.
- LIMIT confirm orders floor to `LOT_SIZE` while MARKET orders floor to `MARKET_LOT_SIZE`, so a coarser MARKET step no longer over-floors or blocks a LIMIT smoke order.

### Added (ROB-302)
- Canonical shared Demo credential resolution (`app/services/brokers/binance/demo/credentials.py`): set `BINANCE_DEMO_API_KEY` / `BINANCE_DEMO_API_SECRET` once and both the Spot Demo and Futures Demo lanes use it — no duplicate secret per lane. Per-product vars (`BINANCE_{SPOT,FUTURES}_DEMO_API_*`) remain optional overrides that win when set. Credential pairs resolve by source: a half-set override (key without secret, or vice versa) fails closed and is never completed from the canonical pair. Each lane's `*_ENABLED` flag still gates activation independently, and a Spot-specific override never resolves for Futures (crossing happens only through the explicit canonical pair).
- `--readiness` evidence now reports `credential_source` (`futures_demo_env` / `shared_demo_env`) and `credential_incomplete`, so operators can confirm which credential pair would be used without any value being printed.

### Added (ROB-299 — Binance Demo smoke hardening + Futures env readiness)
- Spot Demo `--confirm` close path is now fee-aware: the closing SELL sizes from the live free base-asset balance (step-floored, min-notional gated) instead of reusing the original BUY quantity, so a commission-reduced balance no longer triggers an insufficient-balance failure that needs manual remediation.
- New `--readiness` mode on `scripts/binance_futures_demo_smoke.py`: a no-secret, no-HTTP report of `BINANCE_FUTURES_DEMO_{ENABLED,API_KEY,API_SECRET,BASE_URL}` presence/truthiness and host-allowlist judgment, surfacing every missing var at once. Reads only the Futures Demo namespace — Spot Demo and legacy testnet env never leak in.
- New narrow `BinanceSpotDemoExecutionClient.get_asset_balance(asset)` signed read-side method returning only the requested asset's free/locked amounts; the full account payload never enters logs or evidence.
- Structured `spot_demo_smoke_report` evidence event summarizing deployed SHA, env readiness, buy/close quantities and status, open-order count, residual dust, reconciliation status, and blockers.

### Changed (ROB-299)
- Spot Demo close reconciliation now classifies sub-min-notional residue as benign **dust** (ledger row marked `reconciled` with a `residual_dust` note) instead of an anomaly. A dirty order book or a still-sellable remainder is recorded as an anomaly carrying an operator-readable remediation hint.

### Added (ROB-179 — /invest/api/feed/research)
- New `GET /invest/api/feed/research` endpoint on the existing `/invest/api` router. Exposes the ROB-178 `research_reports` table as a paginated, citation-shaped user feed with cursor pagination, 7 tabs (`top`, `latest`, `mine`, `watchlist`, `holdings`, `kr`, `us`), and filters (`source`, `symbol`, `analyst`, `category`, `query`, `fromDate`, `toDate`). Mirrors `/invest/api/feed/news` shape and conventions. Copyright guardrail tests (recursive scan for body fields) are the structural safety gate.

### Added (ROB-56 — KIS official mock hard-separation)
- `MCP_PROFILE` env var (`default` / `hermes-paper-kis`) gates which order tool surface is registered at startup.
- New `hermes-paper-kis` profile: only `kis_mock_*` typed order tools registered; live order surface (`kis_live_*`, legacy ambiguous tools) physically absent from the MCP tool list.
- Typed `kis_live_*` MCP order tools (`kis_live_place_order`, `kis_live_cancel_order`, `kis_live_modify_order`, `kis_live_get_order_history`) — hard-pin `is_mock=False`; additive in `default` profile.
- Typed `kis_mock_*` MCP order tools (`kis_mock_place_order`, `kis_mock_cancel_order`, `kis_mock_modify_order`, `kis_mock_get_order_history`) — hard-pin `is_mock=True`; fail closed on missing KIS mock config.
- Broker capability metadata registry (`app/services/brokers/capabilities.py`): KIS and Kiwoom declared as KR+US equity brokers; metadata only, no routing change.
- `_KISSettingsView` credential isolation regression tests (ROB-19 phase-2 carry).

### Changed (ROB-56)
- `register_all_tools` now accepts an optional `profile: McpProfile` parameter (default `McpProfile.DEFAULT`); existing deployments unaffected.

- Breaking: Require Python 3.13+ and drop support for Python 3.11 and 3.12.
