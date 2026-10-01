# h3-crypto-paper MCP profile (#1171) — declared, not enabled

Operator decision hk 1135 = A. The H3-CRYPTO managed-envelope pilot
(auto_trader-operator `runners/h3_pilot_runner.py --market crypto`) failed
closed on 2026-09-30 (rc 78, hk:doc ops/2026-09-30/h3-crypto-first-run)
because no MCP server served a least-privilege surface for it: the crypto
paper tools were only on DEFAULT, which also carries every live order tool.

This change adds the profile and declares its unit. **Nothing here starts a
container, edits the deploy script, touches HAProxy or registers a
schedule.** Enabling the unit is a separate operator step.

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

The unit env should still carry no broker credentials it does not need
(defense in depth, decided at enablement below), but the code pins above no
longer depend on it.

A network boot without `MCP_AUTH_TOKEN` is refused (same rule as live-*).

## Declared unit

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

`scripts/deploy-ncp-pull.sh` deliberately does not list this unit
(`tests/mcp_server/test_h3_crypto_paper_profile.py` pins that). Until an
operator enables it, the operator runner keeps failing closed because its
endpoint probe does not answer.

## Enabling (operator only, separate approval)

1. Decide the unit's environment. The fixed units run with both deploy
   env files; that is also what this unit gets if added to the deploy arrays
   as-is. A narrower env file is possible but must still let the paper
   simulator reach the database.
2. Confirm 8776 is free on the host listener table.
3. Create `MCP_H3_CRYPTO_PAPER_AUTH_TOKEN` in the deploy secrets file and the
   operator session env (same value), never printed.
4. Add the unit to the `MCP_NAMES` / `MCP_PROFILES` / `MCP_PORTS` /
   `MCP_TOKENS` / `APP_CONTAINERS` arrays and the HAProxy template in a PR
   (update the test above in that PR), deploy, and smoke with a read-only
   `tools/list`: exactly the 20 names.
5. Render the operator configs; the runner preflight then probes the endpoint.
