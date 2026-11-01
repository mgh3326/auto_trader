# Retired: tradingcodex_execution MCP profile (unit at-mcp-tradingcodex-execution, port 8770)

ROB-1436 (operator decision, 2026-11-01) retired this surface. No caller config
pointed at it and Sentry recorded at most one sampled `tools/call` for it in 30
days; live sessions use the DEFAULT surface (prefect
`KR_LIVE_MCP_MODE=shared`).

- Evidence, desk stop order, verification and rollback:
  [mcp-unit-retirement-rob1436.md](mcp-unit-retirement-rob1436.md)
- The previous text of this runbook is in git history:
  `git show 309718831:docs/runbooks/tradingcodex-execution-mcp.md`

Do not recreate the profile or reuse its port without a new review.
