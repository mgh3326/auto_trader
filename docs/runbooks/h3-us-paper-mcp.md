# h3-us-paper MCP profile (#1257, #1245) — deployed by the deploy script

The H3-US managed-envelope pilot (auto_trader-operator
`runners/h3_pilot_runner.py --market us`, account `alpaca_paper`) fails
closed on a missing `mock/.mcp.h3-us.json` (desk 10-05): no MCP server served
a least-privilege surface for it. The Alpaca paper order tools were only on
`us-paper` (which also carries the automated submit, the reconcile writer and
the lab/crypto paper accounts) and, gated, on DEFAULT (every live order tool).

This is the US twin of `h3-crypto-paper` (#1171 profile, #1189 deploy).
**Nothing registers a schedule, and no code generates the token or deploys.**
Those are operator steps below.

## What the profile serves

`MCP_PROFILE=h3-us-paper` is a closed world
(`app/mcp_server/tooling/h3_us_paper_registration.py`). It registers exactly
the 20 names the runner lists in `registered_tools("us")`:

| Phase | Tools |
| --- | --- |
| plan bootstrap | get_operating_briefing, route_request |
| plan research | get_trading_policy, get_quote, get_ohlcv, get_top_stocks, screen_stocks, screen_stocks_snapshot, discover_buy_candidates_fanout, analyze_stock, analyze_stock_batch, session_context_get_recent |
| plan account reads | alpaca_paper_list_orders, alpaca_paper_get_order, alpaca_paper_list_positions |
| execute | market_quote_snapshot_ensure, alpaca_paper_submit_order, alpaca_paper_cancel_order |
| record | analysis_artifact_save, session_context_append |

- The registered set must equal that list or the boot fails. It is a strict
  subset of `us-paper`.
- No live order tool (place/cancel/modify/reconcile for KIS, Toss, Upbit,
  Kiwoom), no other Alpaca surface (automated submit, preview, reconcile
  writer, account/cash reads), no live account read (`get_holdings`, cash,
  positions, order history), no proposal, watch, settings, policy or
  paper-account lifecycle tool, and no `session_bootstrap_pack`.
- The only mutations are the manual Alpaca paper submit/cancel pair (paper
  endpoint only, `confirm=True` required, server-derived idempotency, the
  existing qty 5 / $1000 caps), the quote-snapshot row the submit requires,
  and the two record writes.
- Argument pins:
  - `get_operating_briefing`, `get_quote`, `get_ohlcv`, `get_top_stocks`,
    `screen_stocks`, `screen_stocks_snapshot`,
    `discover_buy_candidates_fanout`, `analyze_stock`, `analyze_stock_batch`
    and `market_quote_snapshot_ensure` are pinned to `market="us"` (omitted
    becomes us, except `market_quote_snapshot_ensure` where it stays a
    required argument; any other value is refused before the body runs).
  - The five Alpaca tools are pinned to `account_mode="alpaca_paper"`; the
    lab and crypto paper accounts are refused.
  - `alpaca_paper_submit_order` is pinned to `asset_class="us_equity"`.
  - `get_operating_briefing` is pinned to `account_scope="db_simulated"`:
    the default US scope (`kis_live`) would read KIS live holdings and KIS
    overseas pending orders. The runner reads the Alpaca paper account
    through the three listed reads instead.
  - `route_request`, `get_trading_policy`, `session_context_get_recent` and
    `analysis_artifact_save` take a market but touch only the DB or the
    policy file; they are reviewed and left unpinned.
- **Credential firewall.** US market data normally uses live broker
  credentials: `get_quote` and the analysis/fan-out path read KIS overseas
  quotes and fill US daily candles through the live KIS app key, `get_ohlcv`
  falls back to Toss, and `screen_stocks` reads USD/KRW from Toss first.
  Every listed tool body here runs inside
  `app/services/brokers/credential_firewall.broker_credentials_blocked`:
  constructing a KIS or Toss client, ensuring a KIS token, dispatching a KIS
  request, sending a Toss request or an authenticated Upbit request raises
  `BrokerCredentialsBlocked` before any token lookup, breaker lease or send.
  The bodies degrade as on a broker outage: US quotes fall back to Yahoo, US
  daily candles to cached rows, USD/KRW to open.er-api; `get_ohlcv` day with
  Yahoo down returns an error instead of Toss candles. Alpaca paper is the
  H3-US account itself and is not blocked. The firewall follows the awaiting
  task and tasks created from it; a thread started with
  `loop.run_in_executor` does not inherit it.
- The runner's guard hook still enforces exact runner intents per call; the
  profile does not replace it.

A network boot without `MCP_AUTH_TOKEN` is refused (same rule as live-* and
h3-crypto-paper).

### route_request on this profile (#1244 r3)

This registration selects the Alpaca paper route surface
(`execution_surface="alpaca_paper"`), so `route_request(intent="buy_analysis"|"profit_taking", market="us")`
answers the `paper-execution-v1` contract with `success=true` /
`degraded=false`: execution tools `alpaca_paper_submit_order` and
`alpaca_paper_cancel_order`, required also `alpaca_paper_list_orders`,
`alpaca_paper_get_order`, `alpaca_paper_list_positions` and
`market_quote_snapshot_ensure`, approval channel runner intent guard, no
proposal tool. Any proposal, live, mock or crypto paper order tool registered
beside it degrades the contract; every reconcile writer is hidden on this
route. The broker-only line "accepted/resting is not a fill; broker evidence
reconcile is required" is replaced by confirming fills from the Alpaca paper
account's own order read. Before #1244 r3 these two routes were degraded and
the H3-US runner's bootstrap check stopped the run, exactly as h3-crypto-paper
did on 2026-10-02.

## Unit

| Unit | MCP_PROFILE | Container port (loopback) | HAProxy frontend (tailnet only) | Token env name |
| --- | --- | ---: | --- | --- |
| at-mcp-h3-us-paper | h3-us-paper | 127.0.0.1:8777 | 100.122.100.56:8777 | MCP_H3_US_PAPER_AUTH_TOKEN |

Client URL: `http://100.122.100.56:8777/mcp` with
`Authorization: Bearer ${MCP_H3_US_PAPER_AUTH_TOKEN}`. The
auto_trader-operator renderer target for `mock/.mcp.h3-us.json` points here.

Port 8777 has no other user in this repo's port map (8000 API, 8765-8767
DEFAULT, 8768-8772 fixed units, 8773-8775 live-*, 8776 h3-crypto-paper). The
live host listener table was not inspected.

`scripts/deploy-ncp-pull.sh` lists this unit in its `MCP_NAMES` /
`MCP_PROFILES` / `MCP_PORTS` / `MCP_TOKENS` / `APP_CONTAINERS` arrays and in
the tailnet route probe, appended after h3-crypto-paper: digest-pinned,
replacement-logged, rolled back on any failure (an introduced unit that fails
is removed again), restored by `--rollback`, kept by the #934 image prune
while it runs, and skippable with `MCP_UNITS_SKIP=h3-us-paper`.
`tests/scripts/test_deploy_ncp_pull_h3_us_paper.py` and
`tests/mcp_server/test_h3_us_paper_profile.py` pin the profile, port and token
name by equality.

The deploy refuses to start before any pull or container change when
`MCP_H3_US_PAPER_AUTH_TOKEN` is missing from both env files (unless the unit
is skipped). The server refuses a blank or missing `MCP_PROFILE` (#1189).

## Enabling (operator only)

1. Environment: the fixed units, this one included, run with both deploy env
   files (`AT_RUNTIME_ENV_FILE`, `AT_SECRETS_ENV_FILE`). The Alpaca paper
   tools need `ALPACA_PAPER_API_KEY` / `ALPACA_PAPER_API_SECRET` there; the
   credential firewall does not depend on which broker keys the env holds.
2. Confirm 8777 is free on the host listener table.
3. Create `MCP_H3_US_PAPER_AUTH_TOKEN` in one of the two deploy env files
   (for example the runtime file the host passes as
   `AT_RUNTIME_ENV_FILE=/root/at-run/.env.api`) and in the operator session
   env, same value, never printed. Check presence by name only:

       grep -c '^MCP_H3_US_PAPER_AUTH_TOKEN=.' /root/at-run/.env.api

4. Deploy with the normal pull script after this PR is merged and its image
   is built; a `--dry-run` first lists `at-mcp-h3-us-paper` as planned.
5. Smoke with a read-only `tools/list` through the tailnet frontend: exactly
   the 20 names above. `/health` on `http://100.122.100.56:8777/health`
   answers 200.
6. Merge the operator renderer PR only after steps 3-5; then render the
   operator config and the runner preflight probes the endpoint.

To hold the unit back on a deploy, run with `MCP_UNITS_SKIP=h3-us-paper` (no
token needed, container untouched). To take it out entirely after a deploy,
`docker rm -f at-mcp-h3-us-paper`; the next deploy starts it again unless
skipped.
