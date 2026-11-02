# Shadow-replay retirement (ROB-1488 R1a)

The unused `shadow-replay` MCP profile and its seven supplied-artifact tools are
retired. `MCP_PROFILE=shadow-replay` is rejected as unknown. The dedicated CLI,
probe, configuration and service package are removed with their tests.

The [historical ROB-697 procedure](../archive/shadow-replay-rob697.md) is retained
for evidence. Do not launch its old commands. Existing replay artifacts remain
untouched; this change does not delete data or run a migration.

The separate `fill-watch-context` profile and
`scripts/fill_watch_context_shadow_replay.py` remain supported. Other MCP profile
inventories and retained lane allowlists are unchanged.
