# KIS Token Cache Design (Redis GET Pressure Reduction)

## Problem
- `get_holdings` can trigger many parallel KR quote calls.
- Each KIS call reaches `_ensure_token()`, which calls `RedisTokenManager.get_token()`.
- Previous behavior always did Redis `GET kis:access_token`, creating connection pressure.

## Goals
- Keep external API behavior unchanged.
- Reuse valid token in-process without Redis hit on every call.
- Preserve distributed refresh lock flow.
- Keep existing 60-second proactive expiration buffer.

## Scope
- Primary file: `app/services/redis_token_manager.py`
- Verified integration path: `app/services/brokers/kis/client.py`
- Verified high-concurrency caller path: `app/mcp_server/tooling/portfolio_holdings.py`

## Non-Goals
- Re-architecting all Redis hot paths.
- Introducing background refresh workers.
- Changing KIS client public method signatures.

## Chosen Design

### 1) Hybrid local + Redis cache in `RedisTokenManager`
Added process-local state:
- `_local_token: str | None`
- `_local_expires_at: float`
- `_local_last_redis_check_at: float`
- `_local_lock: asyncio.Lock`
- `_local_redis_miss_cooldown_until: float`

### 2) Local-first `get_token` with safety guards
`get_token(force_redis_check: bool = False)` now works as:
1. If local token is valid and last Redis revalidation is recent, return local token.
2. If local miss cooldown is active (and force flag is false), return `None`.
3. Enter `_local_lock` and double-check local state.
4. Redis `GET` only when needed.
5. On Redis hit+valid, update local cache and return token.
6. On Redis miss, set short cooldown (`200ms`) and return `None`.
7. On Redis error, if local token is still valid, return local token (degraded success).

### 3) Cross-process safety improvement
- Added periodic Redis revalidation interval (`5s`) so local cache cannot stay detached indefinitely from Redis state.

### 4) Poll loop compatibility
- `refresh_token_with_lock()` now uses `get_token(force_redis_check=True)` in polling and pre-check loops.
- This prevents local miss cooldown from suppressing lock-wait polling behavior.

### 5) Write/delete consistency
- `save_token()` updates local cache before Redis write attempt.
- `clear_token()` invalidates local cache immediately, then deletes Redis key.

## Expiration Policy
- Token validity remains `now < expires_at - 60`.
- Redis TTL remains `expires_in + 60`.

## Observability Expectations
- Reduced count of `GET 'kis:access_token'` spans in high-parallel holdings traces.
- Lower probability of Redis connection saturation under holdings fanout.

## Risks and Mitigations
- Risk: local stale token after cross-process refresh.
  - Mitigation: periodic Redis revalidation interval.
- Risk: Redis error causing unnecessary refresh storms.
  - Mitigation: if local token is valid, serve local token on Redis read failure.
- Risk: cooldown blocking lock poll path.
  - Mitigation: forced Redis check path in refresh polling.

## Rollback
- Revert `app/services/redis_token_manager.py` to previous Redis-only behavior.
- No schema/data migration required.
