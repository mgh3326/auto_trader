# `config/mcp_lane_allowlists/` — reviewed lane manifests

Promoted verbatim from `lane-allowlists.draft/` (MCP tool usage audit,
`docs/mcp-tool-usage-audit-20260903.md`). One file per consumer lane; each
non-comment line is `tool<TAB>basis`, where `basis` is `prompt`, `sentry`, or
`both` — the evidence that the lane actually calls that tool.

These files are **the set that must stay registered**. The contract test
`tests/mcp_server/test_lane_allowlist_contract.py` asserts, for every lane,
that its allowlist is a subset of the tools registered by the MCP profiles
assigned to that lane (audit "Lane allowlist draft" table). Dropping a tool
that a lane still names turns that test red.

Lane → profile assignment is owned by `scripts/mcp_tool_usage_audit.LANE_SPECS`
and re-asserted against a literal in the contract test, so the two cannot drift
apart silently.

Editing rule: a line is removed only after the lane's prompt/runner stops
naming the tool. Adding a line requires the tool to be registered in every
profile assigned to that lane.
