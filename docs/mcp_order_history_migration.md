# MCP Order History Migration Guide (v0.2.0)

Version 0.2.0 introduces breaking changes to the order querying tools to unify order history and open orders functionality.

## Summary

- **Removed**: `get_open_orders` tool.
- **Updated**: `get_order_history` tool with new arguments and response format.

## Migration Paths

### 1. Replacing `get_open_orders`

The `get_open_orders` tool has been removed. Use `get_order_history` with `status="pending"` instead.

**Old Usage:**
```python
# Get all open orders
open_orders = await get_open_orders()

# Get open orders for a specific symbol
btc_orders = await get_open_orders(symbol="KRW-BTC")
```

**New Usage:**
```python
# Get all open orders (across all markets)
open_orders = await get_order_history(status="pending")

# Get open orders for a specific symbol
btc_orders = await get_order_history(symbol="KRW-BTC", status="pending")
```

### 2. Updating `get_order_history` Calls

The signature of `get_order_history` has changed.

**Old Signature:**
```python
get_order_history(symbol: str, market: str = None, days: int = 7, limit: int = 20)
```

**New Signature:**
```python
get_order_history(
    symbol: str | None = None,
    status: str = "all",  # "all", "pending", "filled", "cancelled"
    order_id: str | None = None,
    market: str | None = None,
    side: str | None = None,
    days: int | None = None,
    limit: int | None = 50
)
```

**Key Changes:**
- **`status` argument added**: Filter orders by status. Defaults to "all".
- **`limit` defaults to 50**: Previously 20. Set to 0 or -1 for unlimited.
- **`symbol` logic**:
  - Required if `status` depends on history (e.g. "all", "filled", "cancelled").
  - Optional if `status` is "pending".
  - Strictly required for non-pending queries (even if `order_id` is provided).
- **`market` argument**: Retained as a hint but optional.
- **`days` argument**: Optional. If not provided, no fixed time filter is applied (API defaults apply).

### 3. Response Format

The response dictionary has been enhanced.

```json
{
  "orders": [...],
  "market": "crypto",  // deduced from symbol
  "filters": {
    "symbol": "KRW-BTC",
    "status": "pending",
    "limit": 50
  },
  "truncated": false,  // True if total orders > limit
  "total_available": 5, // Total count of orders matching criteria before limit
  "summary": { ... }
}
```
