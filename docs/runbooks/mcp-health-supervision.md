# Runbook: MCP server health & supervision

MCP production service ownership moved from Mac launchd to NCP. Use
[the NCP MCP runbook](ncp-mcp.md) for deployment, health probes, blue/green
state, fixed profiles, and watchdog recovery.

The Mac native deployment does not run an MCP listener, watchdog, profile
wrapper, or MCP launchd plist. Do not use a Mac `launchctl` command to restart
MCP or create a local MCP heartbeat file.
