# h3-crypto-paper MCP profile (#1171) — deployed by the deploy script (#1189)

Operator decision hk 1135 = A. The H3-CRYPTO managed-envelope pilot
(auto_trader-operator `runners/h3_pilot_runner.py --market crypto`) failed
closed on 2026-09-30 (rc 78, hk:doc ops/2026-09-30/h3-crypto-first-run)
because no MCP server served a least-privilege surface for it: the crypto
paper tools were only on DEFAULT, which also carries every live order tool.

#1171 added the profile and declared its unit. #1189 (part C) wires the unit
into `scripts/deploy-ncp-pull.sh` and the HAProxy template, so the next
operator deploy starts it. **Nothing registers a schedule, and no code
generates the token or deploys.** Those are operator steps below.

## What the profile serves

`MCP_PROFILE=h3-crypto-paper` is a closed world
(`app/mcp_server/tooling/h3_crypto_paper_registration.py`). It registers
exactly the 20 names the runner lists in `registered_tools("crypto")` (the
same list the operator contract registers as the prompt's `allowed_tools`):

| Phase | Tools |
| --- | --- |
| plan bootstrap | get_operating_briefing, route_request |
| plan research | get_trading_policy, get_quote, get_ohlcv, get_support_resistance, get_indicators, get_momentum_candidates, screen_stocks, screen_stocks_snapshot, analyze_stock, analyze_stock_batch, session_context_get_recent |
| plan account reads | paper_reconcile_orders, paper_list_pending_orders, get_holdings (paper-pinned) |
| execute | paper_place_limit_order, paper_cancel_pending_order |
| record | analysis_artifact_save, session_context_append |

- The registered set must equal that list or the boot fails.
- No live order tool (place/cancel/modify/reconcile for KIS, Toss, Upbit,
  Kiwoom, Alpaca), no live account read (cash, positions, order history), no
  proposal, watch, settings, policy or paper-account lifecycle tool, and no
  `session_bootstrap_pack`.
- The only mutations are the ROB-703 paper simulator tools (writes only the
  `paper.*` tables) and the two record writes.
- Argument pins keep every listed tool off live broker credentials:
  - `get_quote`, `get_ohlcv`, `get_indicators`, `get_support_resistance`,
    `get_momentum_candidates`, `screen_stocks`, `screen_stocks_snapshot`,
    `analyze_stock`, `analyze_stock_batch`, `get_holdings` and
    `get_operating_briefing` are pinned to `market="crypto"` (omitted becomes
    crypto; any other value is refused before the tool body runs). The crypto
    paths read public Upbit market data and the DB; the KIS quote, candle,
    indicator, screen-enrichment and equity-analysis paths are unreachable.
  - `get_operating_briefing` is pinned to `account_scope="db_simulated"`:
    holdings come from DB paper accounts and the broker pending-order
    collector is not built (the default crypto scope `upbit_live` would read
    the live Upbit account and its open orders).
  - `get_holdings` is pinned to DB paper accounts: `account` must be `paper`
    or `paper:<name>`, `account_mode` is forced to `db_simulated`, and
    `account_type`, `fresh_sellable` and `include_ledger_lots` are refused.
  - `route_request`, `get_trading_policy`, `session_context_get_recent` and
    `analysis_artifact_save` take a market but touch only the DB or the
    policy file; they are reviewed and left unpinned.
- The runner's guard hook still enforces the account (`account_id=2`) and
  exact runner intents per call; the profile does not replace it.
- `route_request` (#1244): this registration selects the paper-simulator
  route surface, so crypto `buy_analysis` / `profit_taking` answer the
  `paper-execution-v1` contract (execution tools `paper_place_limit_order`,
  `paper_cancel_pending_order`; required also `paper_list_pending_orders`,
  `paper_reconcile_orders`) with `success: true`, `degraded: false`. Before
  #1244 they answered the proposal-led contract, which needs the absent
  `order_proposal_create`, and the runner's bootstrap check halted every run.
  Any proposal, live or mock order tool registered beside it degrades the
  paper contract. The broker-only constraint "accepted/resting is not a fill;
  broker evidence reconcile is required" is replaced on this route by
  confirming fills from the paper account's own order read (#1244 r3). Every
  other profile's route_request output is unchanged.

The unit env should still carry no broker credentials it does not need
(defense in depth, decided at enablement below), but the code pins above no
longer depend on it.

A network boot without `MCP_AUTH_TOKEN` is refused (same rule as live-*).

## Unit

| Unit | MCP_PROFILE | Container port (loopback) | HAProxy frontend (tailnet only) | Token env name |
| --- | --- | ---: | --- | --- |
| at-mcp-h3-crypto-paper | h3-crypto-paper | 127.0.0.1:8776 | 100.122.100.56:8776 | MCP_H3_CRYPTO_PAPER_AUTH_TOKEN |

Client URL: `http://100.122.100.56:8776/mcp` with
`Authorization: Bearer ${MCP_H3_CRYPTO_PAPER_AUTH_TOKEN}`. The
auto_trader-operator renderer target for `mock/.mcp.h3-crypto.json` points
here.

Port 8776 had no user repo-wide (auto_trader, auto_trader-operator,
robin-prefect-automations, fillwire, go-kis, handoffkeep, herdr). The live
host listener table was not inspected.

`scripts/deploy-ncp-pull.sh` lists this unit in its `MCP_NAMES` /
`MCP_PROFILES` / `MCP_PORTS` / `MCP_TOKENS` / `APP_CONTAINERS` arrays and in
the tailnet route probe, exactly like the live-* units: digest-pinned,
replacement-logged, rolled back on any failure (an introduced unit that fails
is removed again), restored by `--rollback`, kept by the #934 image prune
while it runs, and skippable with `MCP_UNITS_SKIP=h3-crypto-paper`.
`tests/scripts/test_deploy_ncp_pull_h3_crypto_paper.py` and
`tests/mcp_server/test_h3_crypto_paper_profile.py` pin the profile, port and
token name by equality.

The deploy refuses to start before any pull or container change when
`MCP_H3_CRYPTO_PAPER_AUTH_TOKEN` is missing from both env files (unless the
unit is skipped). The server itself refuses a blank or missing `MCP_PROFILE`
(#1189), so this unit can never come up as DEFAULT.

## Enabling (operator only)

1. Environment: the fixed units, this one included, run with both deploy env
   files (`AT_RUNTIME_ENV_FILE`, `AT_SECRETS_ENV_FILE`). A narrower env file
   would be a separate change.
2. Confirm 8776 is free on the host listener table (desk: confirmed free).
3. Create `MCP_H3_CRYPTO_PAPER_AUTH_TOKEN` in one of the two deploy env files
   (for example the runtime file the host passes as
   `AT_RUNTIME_ENV_FILE=/root/at-run/.env.api`) and in the operator session
   env, same value, never printed. Check presence by name only:

       grep -c '^MCP_H3_CRYPTO_PAPER_AUTH_TOKEN=.' /root/at-run/.env.api

4. Deploy with the normal pull script after this PR is merged and its image
   is built; a `--dry-run` first lists `at-mcp-h3-crypto-paper` as planned.
5. Smoke with a read-only `tools/list` through the tailnet frontend: exactly
   the 20 names above. `/health` on `http://100.122.100.56:8776/health`
   answers 200.
6. Render the operator configs; the runner preflight then probes the endpoint.

To hold the unit back on a deploy, run with `MCP_UNITS_SKIP=h3-crypto-paper`
(no token needed, container untouched). To take it out entirely after a
deploy, `docker rm -f at-mcp-h3-crypto-paper`; the next deploy starts it
again unless skipped.
