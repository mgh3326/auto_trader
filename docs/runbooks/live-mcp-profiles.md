# Retired: live-kr / live-us / live-crypto MCP profiles (#891, config/mcp_profiles/live.yaml)

ROB-1436 (operator decision, 2026-11-01) retired this surface. No caller config
pointed at it and Sentry recorded at most one sampled `tools/call` for it in 30
days; live sessions use the DEFAULT surface (prefect
`KR_LIVE_MCP_MODE=shared`).

- Evidence, desk stop order, verification and rollback:
  [mcp-unit-retirement-rob1436.md](mcp-unit-retirement-rob1436.md)
- The previous text of this runbook is in git history:
  `git show dc3c0a7bc:docs/runbooks/live-mcp-profiles.md`

Do not recreate the profile or reuse its port without a new review.
