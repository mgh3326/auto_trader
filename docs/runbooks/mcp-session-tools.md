# MCP session tools

## 1. `session_bootstrap_pack`

`session_bootstrap_pack` is the read-only, one-call starting point for a market
session. It accepts `market` (`kr`, `us`, or `crypto`), optional `include`, and
optional `compact`. The fixed section order is `briefing`, `holdings`, `cash`,
`resting`, `pending_retros`, `due_forecasts`, `policy`, and `recent_context`.

| Section | Source tool |
| --- | --- |
| `briefing` | `get_operating_briefing` |
| `holdings` | `get_holdings` |
| `cash` | `get_available_capital` |
| `resting` | `order_proposal_list` plus the briefing pending-order snapshot |
| `pending_retros` | `trade_retrospective_pending` |
| `due_forecasts` | `forecast_resolve` |
| `policy` | `get_trading_policy` (`buy`, `sell`, and `discovery`) |
| `recent_context` | `session_context_get_recent` |

The non-composite sections preserve their source tool response without adding
or renaming fields. `resting` exposes the nonterminal `proposed`, `approved`,
`partially_submitted`, and `submitted` proposal groups as state counts and
items, plus the pending-order snapshot as `ledger_open`; `policy` combines the
three lane responses. Call the individual source tool when a detailed field is
needed. Section failure is fail-open for the pack:
a source fault produces that section's `missing` state while the rest of the
pack continues.

The states are `fresh` (the source returned normally), `stale` (the source
reported stale or degraded data), `missing` (a source fault or exception), and
`denied_by_profile` (policy: the source tool is not registered for this MCP
profile). `missing` and `denied_by_profile` are intentionally distinct.

The pack is write-free and creates no broker surface. `forecast_resolve` is
always invoked with `dry_run=True`. `pending_retros` is capped at its count plus
the first 20 entries. On an explicit `compact=True` request, or when the normal
serialized response is over 65,536 bytes, compact limits apply: holdings,
resting proposals, due forecasts, and briefing list fields are capped as
documented by the tool; recent context remains at 10. A truncated section
reports `meta.sections.<section>.truncated_from`; if still over the cap,
`meta.over_limit` is true.

### Approved specification difference

The committed specification at `docs/specs/mcp-session-tools-v1.md` section 1
uses `(market, lanes?, include?)`; this implementation uses `(market, include?,
compact?)`. It does not accept `lanes`, and the `policy` section always returns
the `buy`, `sell`, and `discovery` lanes. There is no information loss; the
approved implementation brief takes precedence.

| Layer | What it determines | Source of truth |
| --- | --- | --- |
| Lane allowlist (`config/mcp_lane_allowlists/*.txt`) | Whether that lane can call `session_bootstrap_pack` | Audited manifest and `test_lane_allowlist_contract.py` |
| MCP profile registration inventory | Which sections are populated for that profile | Actual registrar inventory and `test_session_bootstrap_profile_sections.py` |

All lanes can call the pack; section visibility is determined by the MCP
profile.

For `analysis_readonly`, the populated sections are `briefing`, `holdings`,
`policy`, and `recent_context`; the other four are explicitly denied.

| Outcome | Sections |
| --- | --- |
| Populated | `briefing`, `holdings`, `policy`, `recent_context` |
| `denied_by_profile` | `cash`, `resting`, `pending_retros`, `due_forecasts` |

## 2. Future session-tool work

## 3. Future session-tool work

## 4. `proposal_revalidate`

`proposal_revalidate(market, proposal_ids=None, dry_run=True, confirm=False)`
is a session-tool read of active order proposals. `market` is `kr`, `us`, or
`crypto`; omitting `proposal_ids` scans the existing proposal-list population
whose group lifecycle is `proposed`, `approved`, `partially_submitted`, or
`submitted`. The response echoes the current `policy` as `{version,
content_hash}` and returns one deterministic label per selected proposal.

The label priority is fixed in this order: `filled_or_expired`,
`stale_policy`, `guard_blocked`, `dead_anchor`, then `keep`. A higher entry
wins whenever more than one condition applies.

- `keep` reports `current_price`, `anchor`, and `distance_bps` when the anchor
  remains within its band.
- `dead_anchor` reports those fields plus `band_bps` when the live quote is
  outside the persisted anchor band.
- `guard_blocked` reports the violated rule and measured input. The read path
  recognizes server-recorded loss-sell evidence in `source_asof` and also
  reports unavailable live price or anchor as a fail-closed guard.
- `filled_or_expired` reports group lifecycle, rung states, `valid_until`, and
  observed terminal time from the proposal ledger.
- `stale_policy` reports the proposal policy stamp beside the current stamp.

The current price comes through the existing `get_quote` implementation. The
persisted policy stamp is `source_asof.policy.{version,content_hash}` (with
the legacy top-level pair accepted for old rows). The persisted anchor is
`source_asof.proposal_revalidate.anchor.{price,band_bps}`; if its price is
absent, the first persisted rung limit price is the deterministic fallback.
The default band is 100 bp only when no non-negative persisted band exists.

`dry_run=True` is label-only and writes nothing. `dry_run=False` additionally
requires `confirm=True`; otherwise the request fails closed. Even with both
gates, only `filled_or_expired` results ask the existing proposal-void
function to act. Its authority is intentionally narrow, so a refusal is a
normal reported outcome rather than an error to bypass. The `void` field is
closed to `voided`, `refused`, `skipped_dry_run`, and `not_applicable`.
There is no proposal replacement, new proposal, or proposal-edit operation in
this tool; supersession is unsupported and fresh judgment remains outside this
surface.

Access has two layers. A lane allowlist makes the tool callable. At call time,
the profile's actually registered inventory must contain `order_proposal_list`
for any operation and `order_proposal_void` for a confirmed non-dry request.
Missing list capability, missing void capability for a requested write, or an
unresolvable inventory all deny closed. Thus an analysis-capable lane with a
proposal read but no void capability can still use the dry-run report.
