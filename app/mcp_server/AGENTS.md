# MCP SERVER KNOWLEDGE BASE

## OVERVIEW
`app/mcp_server/` contains MCP process bootstrap, auth/env helpers, and strategy/scoring modules used by public MCP tools.

## WHERE TO LOOK
| Task | Location | Notes |
|------|----------|-------|
| MCP process startup | `app/mcp_server/main.py` | Sentry init order, transport mode selection, process entrypoint |
| Tool registration orchestration | `app/mcp_server/tooling/registry.py` | Binds market/portfolio/order/fundamentals/analysis tool groups |
| MCP auth provider | `app/mcp_server/auth.py` | Bearer token verifier construction |
| Environment parsing helpers | `app/mcp_server/env_utils.py` | MCP env defaults and typed env parsing |
| Recommendation strategy and scoring | `app/mcp_server/strategies.py`, `app/mcp_server/scoring.py` | Strategy metadata and factor scoring utilities |
| KR tick-size helpers | `app/mcp_server/tick_size.py` | Tick-size table and order price adjustment |
| Public MCP contract reference | `app/mcp_server/README.md` | User-facing tool parameters and market-specific behavior |

## CONVENTIONS
- Initialize Sentry before creating `FastMCP` instances in process bootstrap.
- Register tools through `app/mcp_server/tooling/registry.py` domain registration functions.
- Keep strategy/scoring helpers deterministic and side-effect free.
- Keep env parsing and defaults in `env_utils.py`; avoid scattered env parsing.
- Keep MCP docs (`app/mcp_server/README.md`) synchronized with behavior changes.

## ANTI-PATTERNS
- Do not bypass registration modules by attaching handlers directly in unrelated files.
- Do not silently change public tool names, defaults, or response field contracts.
- Do not add new usage of deprecated `_get_tick_size` helper; use `get_tick_size_kr`/`adjust_tick_size_kr`.
- Do not move market-specific constraints into docs-only changes without code updates.

## NOTES
- `app/mcp_server/tooling/` has its own AGENTS file with handler-level conventions.
- Process transport is controlled by `MCP_TYPE` (`stdio`, `sse`, `streamable-http`) and related env vars.
