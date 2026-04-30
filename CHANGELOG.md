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
