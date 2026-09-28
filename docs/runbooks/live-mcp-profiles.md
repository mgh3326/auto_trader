# live-* MCP profiles (task #891 / Q-53)

The `live-kr`, `live-us`, `live-crypto` profiles are closed-world MCP
surfaces for live trading sessions. Their membership lives in exactly one
operator-readable file:

```
config/mcp_profiles/live.yaml
```

Each profile declares **three groups**, all loaded:

| group | size | contents |
|---|---|---|
| `core` | 15 | quotes, balances, open orders, proposals, watches, records. `get_available_capital`, never `get_cash_balance`. |
| `extension` | ≤10 | per-market heavy hitters (route_request, get_operating_briefing, analysis_artifact_save, trade_retrospective_pending, execution_ledger_fill_events_list_recent, get_market_index; crypto adds fear_greed/long_short_ratio/orderbook) |
| `emergency` | named set | recovery exceptions — existing tools only: cancel, modify, reconcile, watch void, loss_cut exit paths |

Q-53 (2026-09-28) replaced the earlier Q-52 CORE/EXTENDED tiering — there is
no tier switch; the whitelist is the baseline before runner mechanization
and will be re-cut later. Scope is live-* profiles only: nothing is
unregistered server-wide (desk/director/fable/analysis lanes keep using
`default`).

Every manifest line carries `calls_30d` (reference only — Sentry counts
exclude failures, refusals, wrong calls replaced within a minute, and reads
that never led to a write) and a one-line `purpose`. Emergency entries may
omit `calls_30d` (inclusion is categorical). Tools with
`gate: ORDER_PROPOSALS_ENABLED` register only when that settings flag is on.

Hard properties (enforced in `app/mcp_server/tooling/live_profile_registration.py`):

- A manifest name that is not a classified registered tool fails at load
  (startup/test time) — a typo or an unregistered name can never widen the
  surface.
- Mutation-class tools (direct broker order/cancel/modify, reconcile
  writers, proposal lifecycle, persistence coordinators, harness-denied
  tools) are rejected at load **unless** they are one of the explicitly
  named `LIVE_EMERGENCY_TOOL_NAMES` listed in the `emergency` group — and
  emergency names outside that group are rejected too.
- At registration, everything the shared registrars emit that is not
  manifest-selected is physically dropped, and the registered set must
  equal the manifest selection exactly.
- Write tools: `core` carries only the operator-draft set
  (`order_proposal_create/get/list`, `investment_watch_create`,
  `session_context_append`, `forecast_save/resolve`,
  `save_trade_retrospective`); other writes appear only in `extension`
  (doc-evidenced) or `emergency` (named exceptions).

## The emergency group (Q-53)

Recovery tools exist so a live session can still cancel/modify/reconcile and
run the loss_cut exit path — but they stay visible in their own group.
Named per lane:

- `live-kr`: `place_order`, `toss_place_order` (loss_cut exit paths via
  `exit_intent="loss_cut"`), `cancel_order`, `modify_order`,
  `kis_live_cancel_order`, `kis_live_modify_order`,
  `kis_live_reconcile_orders`, `toss_cancel_order`, `toss_modify_order`,
  `toss_reconcile_orders`, `investment_watch_void`
- `live-us` / `live-crypto`: `place_order`, `cancel_order`, `modify_order`,
  `live_reconcile_orders`, `investment_watch_void`

Deliberate non-members:

- `kis_live_place_order` — ROB-864 disables `exit_intent="loss_cut"` on that
  direct tool, so it cannot serve the loss_cut path and is refused even in
  the emergency group.
- `kis_live_get_order_history` — harness-denied for live sessions (#678,
  `HARNESS_DENIED_TOOLS`); refused in every group.

## Switching the NCP live sessions to a live-* profile

Desk-owned; do this **only after the #180 freeze** (about
2026-09-29 18:00 KST).

1. Confirm the deploy is running this PR's build and the lane's session is
   idle (no open proposal awaiting approval you need from this process).
2. On the NCP host, edit the MCP server env for that lane's process/unit:
   - `MCP_PROFILE=live-kr` for the KR lane
   - `MCP_PROFILE=live-us` for the US lane
   - `MCP_PROFILE=live-crypto` for the crypto lane
   Keep `MCP_TYPE`, auth token, and all other env unchanged.
3. Ensure `ORDER_PROPOSALS_ENABLED=true` is set for the process — the
   manifest's `order_proposal_*` tools are gated on it; without it the
   session can read but cannot create/list proposals.
4. Restart the MCP process. At startup the registry loads the manifest and
   fails fast if anything drifted — a failed boot is a correct failure;
   restore the previous `MCP_PROFILE` and escalate.
5. Verify: the session's tool list should match the manifest exactly
   (`tools/list` count equals the profile's group totals; see
   `tests/mcp_server/profile_tool_snapshot.json` for the expected names).
   Order-mutation names may appear ONLY as the lane's named emergency
   entries — no `kis_live_place_order`, no `kis_live_get_order_history`.
6. Rollback = set `MCP_PROFILE` back to `default` and restart.

## Adding a tool (operator-approved PR)

1. Open a PR that adds a line to `config/mcp_profiles/live.yaml` under the
   right group for the lane(s): `name`, `calls_30d` (required outside
   `emergency`), `purpose`, and `gate` if the tool is flag-gated.
2. The tool must already be a registered, classified tool — i.e. it must
   appear in `app/mcp_server/tooling/route_request_lanes.py`'s
   `ALL_KNOWN_TOOLS` taxonomy. If it doesn't, the same PR must classify it
   (the registry-diff tests will fail otherwise).
3. Mutation-class names are refused outside `emergency`, and inside
   `emergency` only `LIVE_EMERGENCY_TOOL_NAMES` members are accepted — a
   new emergency tool means changing that allowlist too, which is an
   operator decision requiring its own review, not a manifest-only edit.
4. Group caps are enforced: `core` ≤ 15, `extension` ≤ 10.
5. Update `tests/mcp_server/profile_tool_snapshot.json` for the changed
   profile (both `gates_enabled` / `gates_disabled` keys), and the
   per-lane counts asserted in
   `tests/mcp_server/test_live_profiles.py::TestGroupCounts`.
6. CI must pass: `test_live_profiles.py`, `test_profile_tool_snapshot.py`,
   `test_lane_allowlist_contract.py`, `test_route_request_registry_diff.py`.

## Removing a tool

Same PR shape — delete the line. Quarterly Sentry review (Q-52/Q-53)
proposes removals for tools with ~0 calls over the window (excluding
failures, refusals, and one-minute corrections).

## Known exclusions (deliberate)

- `kis_live_get_order_history` — 227 calls/30d but harness-denied for live
  sessions per the #678 ruling (`HARNESS_DENIED_TOOLS`); cannot be added
  without lifting the harness denial first.
- `kis_live_place_order` — ROB-864 disables the loss_cut exit on it; the
  generic `place_order` (and `toss_place_order` on KR) carry that path.
- `get_cash_balance` — 12 calls/30d; superseded by `get_available_capital`
  (208 calls).
- `get_fx_rate`, `get_indicators`, `screen_stocks`, `search_symbol`,
  `get_trading_policy`, `list_active_watches`, `get_forecasts`,
  `analysis_artifact_get`, `session_bootstrap_pack`,
  `get_protected_positions` — below the documented cutoff or not
  evidenced for live lanes; returnable via an operator-approved PR.
