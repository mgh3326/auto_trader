# MCP TOOLING KNOWLEDGE BASE

## OVERVIEW
`app/mcp_server/tooling/` implements tool handlers and registrations exposed by the MCP server for quotes, screening, fundamentals, portfolio, and order workflows.

## WHERE TO LOOK
| Task | Location | Notes |
|------|----------|-------|
| Tool registry bootstrap | `app/mcp_server/tooling/registry.py` | Registers tool groups and shared dependencies |
| Shared helper utilities | `app/mcp_server/tooling/shared.py` | Cross-tool response shaping and utility helpers |
| Market-data tool registration | `app/mcp_server/tooling/market_data_registration.py` | Public tool names and handler binding |
| Market-data handlers | `app/mcp_server/tooling/market_data_quotes.py`, `app/mcp_server/tooling/market_data_indicators.py` | Quote/indicator behavior |
| Screening/recommend tools | `app/mcp_server/tooling/analysis_screening.py`, `app/mcp_server/tooling/analysis_recommend.py` | Candidate filtering and scoring flow |
| Fundamental data pipelines | `app/mcp_server/tooling/fundamentals_handlers.py`, `app/mcp_server/tooling/fundamentals_sources_*.py` | KR/US/crypto enrichment sources |
| Order tools | `app/mcp_server/tooling/order_execution.py`, `app/mcp_server/tooling/orders_*.py` | Place/modify/cancel/history contracts |
| Portfolio tools | `app/mcp_server/tooling/portfolio_holdings.py`, `app/mcp_server/tooling/portfolio_cash.py`, `app/mcp_server/tooling/portfolio_dca_*.py` | Holdings and DCA tool behavior |
| API contract reference | `app/mcp_server/README.md` | User-facing tool parameters and market-specific rules |

## CONVENTIONS
- Keep tool name, parameter defaults, and response fields stable once public.
- Maintain registration/handler split (`*_registration.py` vs `*_handlers.py`) for new tool domains.
- Align market-specific behavior with `app/mcp_server/README.md` whenever logic changes.
- Use explicit warnings/errors for unsupported market/filter combinations.
- Reuse shared utilities before adding new ad hoc response formatting.

## ANTI-PATTERNS
- Do not silently change tool argument defaults or rename response keys.
- Do not implement market-specific exceptions only in docs; code and docs must move together.
- Do not bypass existing registration modules by hardwiring handlers in unrelated files.
- Do not add new usage of deprecated tick-size helpers (`app/mcp_server/tick_size.py`).

## NOTES
- This folder is one of the highest-change-surface areas; prioritize backward-compatible edits.
- Screening/recommendation behavior includes market-specific subset limits and warning semantics; preserve contract behavior in tests.
- After MCP tool behavior changes, update tests in `tests/test_mcp_*.py` and `tests/test_upbit_order_tools.py` as needed.
