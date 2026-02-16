# JOBS KNOWLEDGE BASE

## OVERVIEW
`app/jobs/` contains async orchestration units used by routers and scheduled tasks for analysis and trading workflows.

## WHERE TO LOOK
| Task | Location | Notes |
|------|----------|-------|
| Crypto/stock analysis orchestration | `app/jobs/analyze.py` | Analyzer-driven buy/sell and analysis entrypoints |
| KIS trading orchestration | `app/jobs/kis_trading.py` | Domestic/overseas KIS analysis and order flows |
| Scheduled scanner implementation | `app/jobs/daily_scan.py` | Strategy and crash-detection scanner runtime |
| KOSPI200 sync/update jobs | `app/jobs/kospi200.py` | Index constituent update and sync routines |
| Screener job module | `app/jobs/screener.py` | Specialized screening path |
| Scheduled task declarations | `app/tasks/daily_scan_tasks.py` | Only `@broker.task(...)` schedule definitions |
| Worker/scheduler wiring | `app/core/taskiq_broker.py`, `app/core/scheduler.py` | TaskIQ broker and scheduler entrypoints |
| Router callsites | `app/routers/upbit_trading.py`, `app/routers/kis_*trading.py`, `app/routers/stock_latest.py` | Direct async job invocation paths |

## CONVENTIONS
- Keep job modules as orchestration layers over analyzers/services.
- Define periodic schedules in `app/tasks/` and keep `app/jobs/` schedule-agnostic.
- Preserve async-first patterns and explicit resource cleanup in long-running operations.
- Keep broker/scheduler entrypoint paths stable for Make/compose commands.
- Use domain-specific job modules instead of broad cross-domain utility scripts.

## ANTI-PATTERNS
- Do not add `@broker.task(...)` directly in `app/jobs/`; task declarations belong in `app/tasks/`.
- Do not duplicate broker/scheduler construction outside `app/core/taskiq_broker.py` and `app/core/scheduler.py`.
- Do not mix HTTP request/response shaping logic into job modules.
- Do not hide long-running side effects without explicit notification/error handling paths.

## NOTES
- Current schedule cron/timezone values are hardcoded in `app/tasks/daily_scan_tasks.py` (`cron_offset: Asia/Seoul`).
- Router-triggered direct async job calls and TaskIQ-scheduled flows coexist; keep boundary explicit when adding new jobs.
