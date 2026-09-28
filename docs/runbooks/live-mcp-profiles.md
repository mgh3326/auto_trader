# live-* MCP profiles (task #891 / Q-52)

The `live-kr`, `live-us`, `live-crypto` profiles are closed-world MCP
surfaces for live trading sessions. Their membership lives in exactly one
operator-readable file:

```
config/mcp_profiles/live.yaml
```

Each profile has a `tiers:` field — the single switch that selects which
tiers (`core`, `extended`) load. Default is `[core, extended]` until the
operator decides the final per-lane cut (open question in Q-52).

Every manifest line carries `calls_30d` (the 30-day call count from the
usage doc `hk:doc report/2026-09-28/q52-mcp-usage-30d`) and a one-line
`purpose`. Tools with `gate: ORDER_PROPOSALS_ENABLED` register only when
that settings flag is on.

Hard properties (enforced in `app/mcp_server/tooling/live_profile_registration.py`):

- A manifest name that is not a classified registered tool fails at load
  (startup/test time) — a typo or an unregistered name can never widen the
  surface.
- Direct broker order/cancel/modify tools, reconcile writers, proposal
  lifecycle tools, persistence coordinators and harness-denied tools are
  rejected at load (`LIVE_FORBIDDEN_TOOL_NAMES` + name-pattern backstop).
- At registration, everything the shared registrars emit that is not
  manifest-selected is physically dropped, and the registered set must
  equal the manifest selection exactly.
- Write tools: CORE carries only the operator-draft set
  (`order_proposal_create/get/list`, `investment_watch_create/void`,
  `session_context_append`, `forecast_save/resolve`,
  `save_trade_retrospective`); any other write tool may appear only in
  `extended` with doc evidence.

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
   (`tools/list` count equals the profile's core+extended count; see
   `tests/mcp_server/profile_tool_snapshot.json` for the expected names).
   No `*_place_order` / `*_modify_order` / `*_cancel_order` names may
   appear.
6. Rollback = set `MCP_PROFILE` back to `default` and restart.

## Adding a tool (operator-approved PR)

1. Open a PR that adds a line to `config/mcp_profiles/live.yaml` for the
   lane(s): `name`, `tier`, `calls_30d` (from the quarterly Sentry/usage
   evidence), `purpose`, and `gate` if the tool is flag-gated.
2. The tool must already be a registered, classified tool — i.e. it must
   appear in `app/mcp_server/tooling/route_request_lanes.py`'s
   `ALL_KNOWN_TOOLS` taxonomy. If it doesn't, the same PR must classify it
   (the registry-diff tests will fail otherwise).
3. The name must not be in `LIVE_FORBIDDEN_TOOL_NAMES` — order mutations,
   reconcile writers, proposal lifecycle and persistence tools are
   structurally refused for live profiles. If a genuine need arises, that
   is an operator decision requiring its own review, not a manifest edit.
4. Update `tests/mcp_server/profile_tool_snapshot.json` for the changed
   profile (both `gates_enabled` / `gates_disabled` keys), and the
   per-lane counts asserted in
   `tests/mcp_server/test_live_profiles.py::TestTierCounts`.
5. CI must pass: `test_live_profiles.py`, `test_profile_tool_snapshot.py`,
   `test_lane_allowlist_contract.py`, `test_route_request_registry_diff.py`.

## Removing / demoting a tool

Same PR shape — delete the line, or move it between `core` and
`extended`, or remove a tier from `tiers:` to shrink the live surface
without deleting the reviewed entries. Quarterly Sentry review (Q-52)
proposes removals for tools with ~0 calls over the window.

## Known exclusions (deliberate)

- `kis_live_get_order_history` — 227 calls/30d but harness-denied for live
  sessions per the #678 ruling (`HARNESS_DENIED_TOOLS`); cannot be added
  without lifting the harness denial first.
- `get_cash_balance` — 12 calls/30d; superseded by `get_available_capital`
  (208 calls).
- `get_fx_rate`, `get_indicators`, `screen_stocks`, `search_symbol`,
  `get_trading_policy`, `list_active_watches`, `get_forecasts`,
  `analysis_artifact_get`, `session_bootstrap_pack`,
  `get_protected_positions` — below the documented cutoff or not
  evidenced for live lanes; returnable via an operator-approved PR.
