# KIS Token Cache Implementation Plan

## Goal
Reduce Redis `GET kis:access_token` bursts in high-concurrency KIS paths (especially `get_holdings`) while preserving token correctness and existing API contracts.

## Execution Order

1. **Add local cache fields** in `RedisTokenManager`.
2. **Implement local-first read path** with lock-protected double-check.
3. **Add Redis miss cooldown** for true misses only.
4. **Add periodic Redis revalidation interval** to avoid indefinite local staleness.
5. **Update refresh polling path** to bypass cooldown with forced Redis checks.
6. **Synchronize save/clear operations** with local cache state.
7. **Extend tests** for local hit, concurrent miss fanout, cooldown behavior, `_ensure_token` fanout, and holdings-path fanout.
8. **Run diagnostics + tests + lint**.

## Files
- Modify: `app/services/redis_token_manager.py`
- Modify: `tests/test_redis_token_manager.py`
- Add: `docs/plans/2026-02-25-kis-token-cache-design.md`

## Verification Commands

```bash
uv run pytest --no-cov tests/test_redis_token_manager.py -q
```

```bash
make lint
```

```bash
uv run pytest --no-cov tests/test_redis_token_manager.py -q
```

## Acceptance Criteria
- Local cache hit path avoids Redis `GET`.
- Concurrent in-process token reads collapse to a single Redis `GET` on first miss.
- Refresh polling still observes tokens created by other workers.
- Redis read errors do not break valid-local-token usage.
- Existing refresh lock behavior remains intact.

## Rollback Procedure

1. Revert `app/services/redis_token_manager.py` changes.
2. Re-run test command for `tests/test_redis_token_manager.py`.
3. Confirm no public API behavior change in KIS client call sites.

## Notes
- Deployment mode: direct default-on.
- No DB migration required.
