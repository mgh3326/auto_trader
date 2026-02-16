# ROUTERS KNOWLEDGE BASE

## OVERVIEW
`app/routers/` defines HTTP and websocket endpoint surfaces; it should remain a thin layer over `app/services/` and auth dependencies.

## WHERE TO LOOK
| Task | Location | Notes |
|------|----------|-------|
| App include order and global wiring | `app/main.py` | Router registration, middleware, exception handling |
| Health and smoke checks | `app/routers/health.py`, `app/routers/test.py` | Lightweight operational endpoints |
| Trading endpoints (crypto) | `app/routers/upbit_trading.py`, `app/routers/trading.py` | Buy/sell API contracts and request validation |
| Trading endpoints (KIS) | `app/routers/kis_domestic_trading.py`, `app/routers/kis_overseas_trading.py` | Domestic/overseas order APIs |
| Portfolio and holdings | `app/routers/portfolio.py`, `app/routers/manual_holdings.py` | Position read/update routes |
| Dashboard and analysis views | `app/routers/dashboard.py`, `app/routers/analysis_json.py`, `app/routers/stock_latest.py` | Server-rendered pages and analysis responses |
| News and symbols | `app/routers/news_analysis.py`, `app/routers/symbol_settings.py`, `app/routers/kospi200.py` | Enrichment and configuration routes |
| Websocket endpoint adapter | `app/routers/websocket.py` | Runtime streaming endpoint and client management |
| Auth-specific endpoints | `app/auth/router.py`, `app/auth/web_router.py`, `app/auth/admin_router.py` | Auth/admin web flows (outside `app/routers/`) |

## CONVENTIONS
- Declare one `APIRouter` per module with explicit `prefix` and `tags`.
- Keep endpoint handlers thin: validate input, call services, shape response.
- Route registration happens in `app/main.py`; new router modules must be included there.
- Shared dependency wiring belongs in `app/routers/dependencies.py` or `app/auth/*`.
- Reuse existing response/error shape patterns before adding new response contracts.

## ANTI-PATTERNS
- Do not embed heavy trading/business logic directly in router functions.
- Do not call external providers directly from routes when service abstractions already exist.
- Do not introduce auth-sensitive endpoints without dependency checks from `app/auth/` utilities.
- Do not add standalone app entrypoints in router modules; startup stays in `app/main.py`.

## NOTES
- Current router surface is broad (20+ modules), so naming/prefix consistency matters for discoverability.
- If a route changes MCP-visible behavior indirectly, verify corresponding tooling/tests as follow-up.
