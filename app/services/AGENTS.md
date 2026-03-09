# SERVICES KNOWLEDGE BASE

## OVERVIEW
`app/services/` is the business-logic and external-data integration layer for trading, market data, portfolio state, and notifications.

## WHERE TO LOOK
| Task | Location | Notes |
|------|----------|-------|
| Upbit REST + caching behavior | `app/services/upbit.py` | Price/OHLCV fetch paths and inflight/cache coordination |
| KIS API integration | `app/services/kis.py` | Large integration surface; treat as high-risk file |
| KIS order orchestration | `app/services/kis_trading_service.py` | Domestic/overseas buy/sell execution flow |
| KIS holdings normalization | `app/services/kis_holdings_service.py` | Account positions and symbol normalization boundaries |
| Portfolio merge logic | `app/services/merged_portfolio_service.py` | Multi-source holdings consolidation |
| KR fundamentals/news enrichment | `app/services/naver_finance.py` | Valuation/news/sentiment enrichment |
| Order history and execution events | `app/services/order_service.py`, `app/services/execution_event.py` | Order lifecycle and event payload handling |
| Notification fanout | `app/services/fill_notification.py`, `app/services/toss_notification_service.py` | Fill and external notifier integrations |
| Websocket service clients | `app/services/kis_websocket.py`, `app/services/upbit_websocket.py` | Stream-specific adapter logic |

## CONVENTIONS
- Keep transport/domain behavior here; routers should orchestrate request/response only.
- Preserve symbol normalization boundaries (`app/core/symbol.py`) when adding KR/US ticker logic.
- Prefer extending focused modules (`*_service.py`) before adding logic to `app/services/kis.py`.
- Keep async call sites and timeout/retry behavior explicit for external API interactions.
- For holdings/trading changes, verify side effects in both notification and order-history services.

## ANTI-PATTERNS
- Do not hardcode API credentials or account secrets in service code.
- Do not bypass service-level normalization by injecting raw external symbol formats into DB paths.
- Do not add new feature logic only inside debug scripts; production paths belong in `app/services/`.
- Do not introduce broad refactors in `app/services/kis.py` during bugfix work; keep fixes minimal.

## NOTES
- `app/services/kis.py` is intentionally large; use targeted reads and narrow edits.
- Service changes often require test updates in `tests/test_services_*.py` and domain-specific test files; use `make test-services-split` when you need the exact former `tests/test_services.py` scope.
- Some services have dedicated submodules (`app/services/disclosures/`, `app/services/kis/`) for narrower responsibilities.
