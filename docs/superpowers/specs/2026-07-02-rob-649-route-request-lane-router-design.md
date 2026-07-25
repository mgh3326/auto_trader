# ROB-649 — `route_request` advisory lane router (design)

**Status:** approved 2026-07-02. Parent: ROB-644. Depends on ROB-643 (playbook,
landed) + ROB-646 (trading policy YAML, landed `da5c229e`).

## Purpose

Give a Claude/Hermes MCP session a single deterministic entry point that turns a
coarse **intent** into the standard tool sequence, advisory allow/block lists,
policy thresholds, and hard constraints for that decision lane. Today this frame
lives only in prompt/context, so the same prompt drifts across sessions and
models (playbook §5). `route_request` moves the frame server-side as **advisory
guidance** — it does **not** enforce anything (no middleware; that is a separate
follow-up issue).

## Scope

- **In:** one read-only advisory MCP tool `route_request(intent, market)`; a
  static lane→tool classification; a registry-diff test; policy-version echo;
  divergence documentation. migration 0.
- **Out (follow-up issue):** enforcement middleware (FastMCP `on_call_tool`
  per-tool-name gating keyed on caller header). MCP session state resets on
  streamable-http reconnect (ROB-469), so session-scoped lane state is unfit;
  enforcement must be caller-header-keyed and is deferred.

## Inputs

```
route_request(intent: str, market: str) -> dict
```

- `intent ∈ {buy_analysis, profit_taking, discovery, market_brief}` — the only
  free choice the LLM makes; everything downstream is server-deterministic.
- `market ∈ {kr, us, crypto}` — **required** (matches `trading_policy.yaml`
  `market_overrides` keys). Omitted/invalid → `success=false`.

Rationale for enum input: the tradingcodex original
(`classify_starter_request`, `harness.py:943`) is ~20 English-only regexes and
does not port to Korean. We take the intent as an explicit enum instead.

## Intent → lane mapping

| intent          | lane (playbook) | policy lane (ROB-646) |
|-----------------|-----------------|-----------------------|
| `buy_analysis`  | `buy`           | `buy`                 |
| `profit_taking` | `sell`          | `sell`                |
| `discovery`     | `discovery`     | `discovery`           |
| `market_brief`  | `bootstrap`     | *(none)*              |

`market_brief` maps to the playbook **bootstrap** lane (context load:
`get_operating_briefing` / `session_context_get_recent` /
`analysis_artifact_list` / `analysis_artifact_get` / `get_market_index` /
`get_fx_rate`). The bootstrap lane has no policy thresholds, so its
`verdict_thresholds.thresholds` is `{}` while `policy_version` is still echoed.

## Return contract (deterministic per `intent, market`)

```jsonc
{
  "success": true,
  "intent": "buy_analysis",
  "lane": "buy",
  "market": "kr",
  "standard_tool_sequence": [               // profile-intersected (req 3)
    {"step": 1, "tool": "get_operating_briefing", "purpose": "..."},
    ...
  ],
  "allowed_tools": [...],                    // lane seq tools + read-only research bucket
  "blocked_actions": [...],                  // MUTATION_TOOLS − lane's own mutation tools
  "verdict_thresholds": {                    // get_policy_for echo; thresholds {} for market_brief
    "market": "kr", "lane": "buy",
    "version": "...", "content_hash": "...",
    "thresholds": { "screen.rsi_max": {...}, ... }
  },
  "policy_version": {"version": "...", "content_hash": "..."},
  "hard_constraints": [                       // per-lane, policy-key references, no magic numbers
    "loss guard: sell price >= avg * sell.loss_guard_min_multiple",
    "KRX tick rounding", "DAY order expiry at order.day_expiry_kst -> re-place next day",
    "no two-sided (buy+sell) resting orders on same Toss symbol"
  ]
}
```

Errors: `{"success": false, "error": "unknown_intent", "detail": "..."}` or
`{"success": false, "error": "unknown_market", "detail": "..."}`.

Determinism: output is a pure function of `(intent, market)` and the current
`trading_policy.yaml` file state + the registered tool surface. No clocks, no
randomness.

## Components

### `app/mcp_server/tooling/route_request_lanes.py` (pure; no MCP dependency)

- `INTENT_TO_LANE: dict[str, str]`
- `LANE_SEQUENCES: dict[str, list[dict]]` — ordered `{step, tool, purpose}` per
  lane (`bootstrap`/`buy`/`sell`/`discovery`), ported from the playbook
  ` ```yaml lanes: ``` ` blocks. Definition **source** is the playbook (req 1);
  this is a static dict (req 2) kept in sync by a drift test.
- `LANE_TO_POLICY_LANE: dict[str, str | None]`
- `HARD_CONSTRAINTS: dict[str, list[str]]` — per-lane constraint summaries that
  reference policy keys, never magic numbers (playbook §0 gates).
- **Classification buckets** (the registry-diff partition):
  - `READ_ONLY_ADVISORY_TOOLS: frozenset[str]` — explicit list of every
    read-only/research tool in the DEFAULT profile that is not a lane-sequence
    tool or a mutation tool (includes `route_request` itself and
    `get_trading_policy`).
  - `MUTATION_TOOLS: frozenset[str]` — composed from the already-exported sets
    (`ORDER_TOOL_NAMES`, `KIS_LIVE_ORDER_TOOL_NAMES`, `KIS_MOCK_ORDER_TOOL_NAMES`,
    `LIVE_RECONCILE_TOOL_NAMES`, `TOSS_LIVE_ORDER_TOOL_NAMES`,
    `KIWOOM_MOCK_TOOL_NAMES`).
  - lane-sequence tool names are derived from `LANE_SEQUENCES`.
- `build_route_plan(intent, market, registered_tools, policy_view) -> dict` —
  pure builder assembling the return contract.
  - `standard_tool_sequence` = lane steps whose `tool ∈ registered_tools`
    (profile intersection, req 3), re-numbered.
  - `allowed_tools` = (lane sequence tools ∪ `READ_ONLY_ADVISORY_TOOLS`) ∩
    `registered_tools`.
  - `blocked_actions` = (`MUTATION_TOOLS` − lane's own mutation tools) ∩
    `registered_tools`, sorted.

### `app/mcp_server/tooling/route_request_registration.py` (thin MCP glue)

- `ROUTE_REQUEST_TOOL_NAMES = {"route_request"}`
- `async def route_request(intent, market)` — validates enums, resolves policy
  view via `get_policy_for` (ROB-646) for lanes with a policy lane, enumerates
  live registered names via `await mcp.get_tools()` (fail-open to the full lane
  list on introspection error), calls `build_route_plan`, returns the contract.
  The `mcp` instance is captured in the registration closure.
- `register_route_request_tools(mcp)` — registers `route_request` with a
  description documenting the advisory nature + divergence.

### `registry.py`

Add `register_route_request_tools(mcp)` to the **always-registered** block (all
profiles — read-only, no order surface).

## Tests

- `tests/test_route_request.py`
  - deterministic output for each of the 4 intents (call twice, assert equal)
  - `unknown_intent` and `unknown_market` error paths
  - `policy_version` present on every success (incl. `market_brief`)
  - `market_brief` → `verdict_thresholds.thresholds == {}`, lane `bootstrap`
  - `blocked_actions` derivation: `buy` blocks non-buy mutation tools; a lane's
    own place tool is not in `blocked_actions`
  - profile intersection: CRYPTO profile route drops `toss_place_order` from
    `standard_tool_sequence` / `allowed_tools`
- `tests/test_route_request_registry_diff.py`
  - **set-equality total partition:** for the DEFAULT profile,
    `registered_tools == LANE_TOOLS ∪ READ_ONLY_ADVISORY_TOOLS ∪ MUTATION_TOOLS`,
    tolerating an explicit `_FLAG_GATED_OR_OPTIONAL` allowlist (kiwoom_mock,
    binance_demo_scalping, investment_snapshots, hermes) that may be absent when
    their flags are off. A new **unclassified** tool → `registered − classified`
    non-empty → CI fails (the core requirement). A removed/renamed classified
    tool → `classified − registered` beyond the allowlist → CI fails.
  - drift guard: every tool in `LANE_SEQUENCES` matches the playbook
    ` ```yaml lanes: ``` ` blocks (reuses the `test_playbook_tool_names.py`
    parser), so the static dict cannot silently diverge from the playbook.

## Divergence documentation (req 4)

Recorded in the `route_request` tool description **and** `app/mcp_server/README.md`:

> The tradingcodex original has no route MCP tool — it injects lane guidance via
> a hook and maps lane→role→tool indirectly. auto_trader exposes a **direct
> lane→tool advisory** MCP tool with no enforcement. Blocking/enforcement
> middleware is a separate follow-up issue (mutation tools only; reads
> unrestricted; caller-header-keyed because MCP session state resets on
> reconnect — ROB-469).

## Acceptance criteria (from the issue)

- [x] `route_request` deterministic for all 4 lanes (same input → same output)
- [x] registry-diff test fails on an unassigned new tool
- [x] policy echo includes `policy_version`
- [x] migration 0; registered on every profile (plan intersects per profile)

## Non-goals / YAGNI

- No enforcement/blocking middleware (follow-up).
- No market-specific tool sequences — the sequence is lane-derived (playbook is
  KR-centric); `market` drives only the policy echo. Profile intersection (not
  market) removes unregistered tools.
- No new DB tables / migrations.
