# MONITORING KNOWLEDGE BASE

## OVERVIEW
`app/monitoring/` holds observability and notification integrations, primarily Sentry setup/filtering and Telegram trade notifications.

## WHERE TO LOOK
| Task | Location | Notes |
|------|----------|-------|
| Shared Sentry initialization | `app/monitoring/sentry.py` | Integration flags, sensitive-data scrubbing, health-log suppression |
| Trade notification delivery | `app/monitoring/trade_notifier.py` | Singleton notifier, message formatting, Telegram send flow |
| yfinance request span tracing | `app/monitoring/yfinance_sentry.py` | HTTP client span wrapper and metadata tagging |
| API startup monitoring wiring | `app/main.py` | Calls `init_sentry` and notifier setup/cleanup |
| Worker startup monitoring wiring | `app/core/taskiq_broker.py` | Worker Sentry init and notifier configuration |

## CONVENTIONS
- Initialize Sentry only through `app/monitoring/sentry.py` helpers.
- Preserve sensitive-key filtering for events, logs, breadcrumbs, and exception context.
- Use `get_trade_notifier()` singleton accessor instead of direct notifier instantiation.
- Ensure notifier HTTP clients are closed on shutdown paths.
- Keep transaction/log filtering behavior stable for `/healthz` and MCP transaction naming.

## ANTI-PATTERNS
- Do not call `sentry_sdk.init(...)` directly from unrelated runtime modules.
- Do not log or expose bot tokens, chat IDs, authorization headers, or secret-bearing fields.
- Do not instantiate multiple notifier instances or bypass singleton lifecycle.
- Do not remove healthcheck noise suppression without replacement filtering.

## NOTES
- Monitoring logic is shared by API and worker runtimes; regressions can affect both.
- Trade notifier formatting is contract-like for operators; keep message shape changes intentional.
