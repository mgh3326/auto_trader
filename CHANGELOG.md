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
- Breaking: Require Python 3.13+ and drop support for Python 3.11 and 3.12.
