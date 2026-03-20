# Upbit Symbol Universe Runtime Guide

## Overview

Upbit symbol/market resolution now uses DB table `upbit_symbol_universe` as the
single runtime source of truth.

- Sync source endpoint: `GET /v1/market/all?isDetails=true`
- Runtime lookup source: `upbit_symbol_universe` table only
- Runtime path does **not** call `/v1/market/all`

## Sync Commands

Run after migrations:

```bash
make sync-upbit-symbol-universe
# or
uv run python scripts/sync_upbit_symbol_universe.py
```

## Runtime API

Use `app/services/upbit_symbol_universe_service.py`:

- `prime_upbit_constants()`
- `get_or_refresh_maps(force=False)`
- `get_active_upbit_markets(fiat=None)`
- `get_upbit_warning_markets(fiat=None)`
- `get_upbit_symbol_by_name(name)`
- `search_upbit_symbols(query, limit)`

Legacy map-style access is still available from the service module:

- `NAME_TO_PAIR_KR`
- `PAIR_TO_NAME_KR`
- `COIN_TO_PAIR`
- `COIN_TO_NAME_KR`
- `COIN_TO_NAME_EN`
- `KRW_TRADABLE_COINS`

## Failure Policy

If `upbit_symbol_universe` is empty or unavailable, runtime lookup fails fast
with sync guidance:

```text
Sync required: uv run python scripts/sync_upbit_symbol_universe.py
```
