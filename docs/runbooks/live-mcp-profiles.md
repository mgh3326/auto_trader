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
| `extension` | ≤10 | per-market heavy hitters (route_request, get_operating_briefing, analysis_artifact_save, trade_retrospective_pending, execution_ledger_fill_events_list_recent, get_market_index; crypto adds fear_greed/long_short_ratio/orderbook and `get_upbit_altseason` per Q-58) |
| `emergency` | named set | recovery exceptions — existing tools only: cancel, modify, reconcile, watch void, loss_cut recovery/planning reads |

Q-53's "20-25 tools per lane" budget applies to `core` + `extension`
(15 + ≤10); `emergency` entries are additive named exceptions — an
all-inclusive cap would be unsatisfiable (crypto's mandated minimum alone is
29).

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

Recovery tools exist so a live session can still cancel/modify/reconcile,
void a watch, and work the loss_cut path — but they stay visible in their
own group. Named per lane:

- `live-kr`: `cancel_order`, `modify_order`, `kis_live_cancel_order`,
  `kis_live_modify_order`, `kis_live_reconcile_orders`,
  `toss_cancel_order`, `toss_modify_order`, `toss_reconcile_orders`,
  `investment_watch_void`, `order_proposal_list_expired_defensive`,
  `sell_ladder_fill_preview`
- `live-us` / `live-crypto`: `cancel_order`, `modify_order`,
  `live_reconcile_orders`, `investment_watch_void`,
  `order_proposal_list_expired_defensive`, `sell_ladder_fill_preview`

### loss_cut truth — why no direct place tool is listed

The ONLY existing loss_cut execution path is
`order_proposal_create(exit_intent="loss_cut")`, which lives in `core` on
every lane. All direct place tools (`place_order`, `toss_place_order`,
`kis_live_place_order`) reject `exit_intent="loss_cut"` outright (ROB-864 —
`loss_cut_direct_path_disabled_use_order_proposal_create`), so they are NOT
loss_cut paths: listing them would silently grant unrestricted direct live
buy/sell outside the proposal/Telegram approval flow (independent-tester
round-3 finding). The emergency group therefore carries the loss_cut
recovery/planning reads instead:

- `order_proposal_list_expired_defensive` — read-only handoff of
  expired/voided loss_cut/defensive_trim proposals for re-judgment (ROB-929;
  gated on `ORDER_PROPOSALS_ENABLED`).
- `sell_ladder_fill_preview` — non-executing ladder-exit fill preview to
  plan an emergency exit before proposing it.

Deliberate non-members:

- `place_order` / `toss_place_order` / `kis_live_place_order` — cannot do
  loss_cut (ROB-864); exposing them would add direct live placement, which
  is not an operator-approved live surface.
- `kis_live_get_order_history` — harness-denied for live sessions (#678,
  `HARNESS_DENIED_TOOLS`); refused in every group.

## The Q-58 crypto exception

`get_upbit_altseason` sits in `HARNESS_DENIED_TOOLS` (#678 — it primes the
shared Upbit index cache that `get_upbit_index` serves as truth), but the
operator approved it for live-crypto's C1 breadth read. It is a read-only
public Upbit index with a process TTL cache and registers on the profile's
`extension` group. Caveat: live-session harnesses still deny it at the
lane-policy layer until #678 is lifted — the profile surface exposes it,
the live-session runtime may not reach it. The exception is scoped: the
same manifest line is refused on live-kr / live-us.

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
   entries — no direct place tool at all (`place_order`, `toss_place_order`,
   `kis_live_place_order` are all absent), no `kis_live_get_order_history`.
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
- `place_order` / `toss_place_order` / `kis_live_place_order` — direct
  place paths: ROB-864 disables `loss_cut` on all of them, so they cannot
  serve the loss_cut path and would only grant unrestricted direct
  placement. Not listed anywhere; the proposal flow is the exit path.
- `get_cash_balance` — 12 calls/30d; superseded by `get_available_capital`
  (208 calls).
- `get_fx_rate`, `get_indicators`, `screen_stocks`, `search_symbol`,
  `get_trading_policy`, `list_active_watches`, `get_forecasts`,
  `analysis_artifact_get`, `session_bootstrap_pack`,
  `get_protected_positions` — below the documented cutoff or not
  evidenced for live lanes; returnable via an operator-approved PR.
