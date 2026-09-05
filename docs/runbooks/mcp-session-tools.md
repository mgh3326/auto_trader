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

## 4. Future session-tool work
