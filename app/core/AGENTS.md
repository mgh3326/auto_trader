# CORE KNOWLEDGE BASE

## OVERVIEW
`app/core/` holds shared runtime infrastructure: configuration, database wiring, TaskIQ broker/scheduler, symbol helpers, and reusable runtime utilities.

## WHERE TO LOOK
| Task | Location | Notes |
|------|----------|-------|
| Environment/config settings | `app/core/config.py` | Typed settings and env loading behavior |
| Database session wiring | `app/core/db.py` | Async engine/session factory helpers |
| TaskIQ broker setup | `app/core/taskiq_broker.py` | Redis broker, result backend, worker middleware |
| TaskIQ scheduler setup | `app/core/scheduler.py` | Scheduler object and schedule source wiring |
| Model rate limiter | `app/core/model_rate_limiter.py` | Shared LLM model availability/rate-limiting state |
| Symbol conversion helpers | `app/core/symbol.py` | DB/API symbol normalization boundaries |
| Session/token blacklist | `app/core/session_blacklist.py` | Auth/session invalidation support |
| Async API rate limiting | `app/core/async_rate_limiter.py` | Shared async throttling utility |
| Timezone utilities | `app/core/timezone.py` | Time conversion helpers used across jobs/services |

## CONVENTIONS
- Keep shared infrastructure logic here; avoid domain-specific business logic in `app/core/`.
- Treat `config.py` as the source of truth for runtime settings and env keys.
- Keep broker/scheduler entrypoints stable for worker and compose command paths.
- Reuse symbol/time/rate-limit helpers instead of duplicating utility logic in services.
- Validate config and runtime helper changes against tests that rely on startup behavior.

## ANTI-PATTERNS
- Do not hardcode environment-dependent values when `settings` already exposes them.
- Do not duplicate broker/scheduler initialization in other modules.
- Do not bypass symbol normalization utilities in cross-market service paths.
- Do not add service-specific orchestration into core utility modules.

## NOTES
- Changes here can affect API startup, worker startup, and MCP/job behavior simultaneously.
- Keep root docs/config examples synchronized when adding or renaming settings.
