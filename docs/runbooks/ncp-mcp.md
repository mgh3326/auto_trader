# NCP MCP blue/green deployment

This runbook deploys the seven NCP MCP server instances behind a private HAProxy
listener. It does not perform the client cutover: changing .mcp.json, restarting
consumer sessions, retiring the Mac services, and changing Cloudflare routes are
owned by the orchestrator.

## Private endpoint and units

HAProxy binds exactly 127.0.0.1:8765 and 100.122.100.56:8765; it must never
bind 0.0.0.0. Its backend is the active main MCP color.

| Unit | Port | MCP_PROFILE | required token environment name |
| --- | ---: | --- | --- |
| at-mcp-blue | 8766 | default | MCP_AUTH_TOKEN |
| at-mcp-green | 8767 | default | MCP_AUTH_TOKEN |
| at-mcp-analysis-readonly | 8768 | analysis_readonly | MCP_ANALYSIS_READONLY_AUTH_TOKEN |
| at-mcp-account-read | 8769 | account_read | MCP_ACCOUNT_READ_AUTH_TOKEN |
| at-mcp-tradingcodex-execution | 8770 | tradingcodex_execution | MCP_TRADINGCODEX_EXECUTION_AUTH_TOKEN |
| at-mcp-paper-001 | 8771 | hermes-paper-kis | MCP_PAPER_001_AUTH_TOKEN |
| at-mcp-kiwoom | 8772 | kiwoom | MCP_KIWOOM_AUTH_TOKEN |

All units run with host networking, but the Python server itself is explicitly
bound to MCP_HOST=127.0.0.1, MCP_TYPE=streamable-http, MCP_PATH=/mcp, and
MCP_USER_ID=1. The main profile is authenticated because it is exposed over
tailnet. The Kiwoom profile also requires a token for HTTP transports.

## First deployment

1. On NCP, install this branch's deploy script and HAProxy/systemd files. Keep
   both Docker env files outside git. If the host calls its runtime file
   /root/at-run/.env.api, invoke the script with
   AT_RUNTIME_ENV_FILE=/root/at-run/.env.api; the second established secret
   env file remains AT_SECRETS_ENV_FILE.
2. Confirm all seven token names in the table are non-empty across the two
   --env-file inputs. Do not print their values. MCP_PAPER_001_AUTH_TOKEN
   and MCP_KIWOOM_AUTH_TOKEN are required before normal deployment.
3. Remove the legacy default listener before HAProxy claims its port:

       docker rm -f at-mcp-readonly

4. Install and enable the watchdog after the first healthy MCP promotion:

       install -m 0644 ops/ncp/systemd/at-mcp-watchdog.{service,timer} /etc/systemd/system/
       systemctl daemon-reload
       systemctl enable --now at-mcp-watchdog.timer

5. Promote using the normal pull script. The process refuses to start a
   tokenless required profile before it replaces any existing core container.

       /root/at-run/deploy-ncp-pull.sh

For a temporary pre-token validation only, an operator may exclude precisely the
two unavailable units with MCP_UNITS_SKIP=paper-001,kiwoom. The default has no
skips; remove the flag for the final seven-profile promotion.

## Color promotion, drain, and rollback

The active color is the one-line durable file
/root/at-run/mcp-active-color. On an existing deployment, the script launches
only the other color at the resolved image digest, waits for its direct
/health 200, refreshes all fixed profiles, atomically changes the active file,
renders /root/at-run/haproxy.cfg, and sends HAProxy SIGHUP. It does not stop
the former color then: a captured-container-ID timer removes it after
MCP_DRAIN_SECONDS (default 3600) so existing streamable HTTP sessions can
finish. Inspect its timer evidence with:

       cat /root/at-run/mcp-active-color
       cat /root/at-run/mcp-drain-blue.pid /root/at-run/mcp-drain-green.pid 2>/dev/null || true
       docker ps --format 'table {{.Names}}\t{{.Image}}\t{{.Status}}' | grep '^at-mcp\|^at-haproxy'

/root/at-run/deploy-ncp-pull.sh --rollback promotes the previous recorded
digest through the same checks. A failed readiness path restores API, scheduler,
and worker to pinned prior digest references; investigate MCP logs and the
rendered config before retrying. Never substitute a movable :main reference.

The watchdog is a systemd timer, not a health probe: it restarts
at-mcp-active-color only when the active process is reported running and its
event-loop heartbeat is stale. Missing, stopped, inactive, or draining colors
are never restarted.

## Orchestrator cutover checklist

After all seven direct health checks and per-profile tools/list checks pass,
the orchestrator may cut over consumers.

| Consumer profile | URL |
| --- | --- |
| main | http://100.122.100.56:8765/mcp |
| analysis readonly | http://100.122.100.56:8768/mcp |
| account read | http://100.122.100.56:8769/mcp |
| tradingcodex execution | http://100.122.100.56:8770/mcp |
| paper 001 | http://100.122.100.56:8771/mcp |
| kiwoom | http://100.122.100.56:8772/mcp |

1. Update each .mcp.json entry to its table URL and
   headers.Authorization: Bearer $MCP_AUTH_TOKEN (use that profile's secret
   value; do not place literal secrets in source control).
2. End the crypto cycle cleanly, then restart consumers in order:
   orch-live, followed by orch-mock.
3. In each new session, call tools/list, record the tool count, and make one
   read-only tool call for that profile.
4. After the NCP sessions are confirmed, stop the Mac MCP services in this
   order: Mac HAProxy; Mac MCP blue/green; fixed Mac profiles; Mac watchdog.
5. Repoint the trader-mcp*.robinco.dev Cloudflare routes only after private
   tailnet validation and the consumer cutover are complete.
