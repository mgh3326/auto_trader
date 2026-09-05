# MCP surface cleanup — 2026-09-03 audit follow-up

Applied change for the audit in
[`docs/mcp-tool-usage-audit-20260903.md`](../mcp-tool-usage-audit-20260903.md).
The audit itself changed nothing; this is the removal, the isolation, and the
contract tests that keep both honest.

**Result: 228 registered tools -> 167.** 60 class-D tools deregistered, 5 kept
for stated structural reasons, 79 class-C tools kept and tagged, one MCP
profile retired.

---

## 1. What the audit's classes mean here

| Class | Definition | Action taken |
|---|---|---|
| A | called in the last 30 days | untouched |
| B | called in the last 90 days only | untouched |
| C | referenced (prompt/runbook/code) but never called | kept, tagged `niche` |
| D | no calls, no references | deregistered (60 of 65) |

Class is a property of the **tool**, not of a (tool, profile) pair — the audit's
"Complete classification" table has one row per tool. So a class-D tool is dead
on every profile that registered it, and every removal below is global. The
brief's default about "D on one profile, A/B/C on another" therefore never
fired.

## 2. Profile-by-profile counts

| Profile | tools before | tools after | class-D removed | class-C now tagged niche |
|---|---:|---:|---:|---:|
| account_read | 10 | 10 | 0 | 1 |
| alpaca-paper-clean | 13 | 13 | 0 | 5 |
| analysis_readonly | 28 | 27 | 1 | 1 |
| crypto | 142 | 107 | 35 | 41 |
| db-paper | 143 | 100 | 43 | 35 |
| default | 202 | 164 | 38 | 76 |
| hermes-paper-kis | 138 | 103 | 35 | 37 |
| kiwoom | 149 | 114 | 35 | 43 |
| kiwoom_kr | 141 | 106 | 35 | 38 |
| **paper_execution** | 15 | **profile removed** | 14 | 0 |
| shadow-replay | 3 | 3 | 0 | 1 |
| tradingcodex_execution | 43 | 41 | 2 | 14 |
| us-paper | 157 | 121 | 36 | 47 |
| watch_repricing | 15 | 15 | 0 | 0 |

`paper_execution` was retired outright: 14 of its 15 tools were class D, so
emptying it would have left a profile whose only remaining tool
(`paper_cohort_kill_switch`, class C) had no other reason to exist as a
surface. `MCP_PROFILE=paper_execution` now fails closed with
`Unknown MCP_PROFILE` instead of silently degrading to `default`.

## 3. Removed tools (complete)

| Tool | Profiles it registered on | Module | Audit mutation label |
|---|---|---|---|
| `alpaca_paper_reconcile_orders` | default, us-paper | `alpaca_paper_orders` | order |
| `analysis_bundle_create` | crypto, db-paper, default, hermes-paper-kis, kiwoom, kiwoom_kr, us-paper | `analysis_bundle_handlers` | persistence |
| `analysis_bundle_get` | analysis_readonly, crypto, db-paper, default, hermes-paper-kis, kiwoom, kiwoom_kr, us-paper | `analysis_bundle_handlers` | no |
| `analyze_portfolio` | crypto, db-paper, default, hermes-paper-kis, kiwoom, kiwoom_kr, us-paper | `analysis_registration` | no |
| `compare_paper_accounts` | db-paper | `paper_analytics_registration` | no |
| `compare_strategies` | db-paper | `paper_journal_bridge` | no |
| `create_paper_account` | db-paper | `paper_account_registration` | persistence |
| `delete_paper_account` | db-paper | `paper_account_registration` | persistence |
| `get_analyst_consensus` | crypto, db-paper, default, hermes-paper-kis, kiwoom, kiwoom_kr, us-paper | `fundamentals_handlers` | no |
| `get_dividends` | crypto, db-paper, default, hermes-paper-kis, kiwoom, kiwoom_kr, us-paper | `analysis_registration` | no |
| `get_financials` | crypto, db-paper, default, hermes-paper-kis, kiwoom, kiwoom_kr, us-paper | `fundamentals_handlers` | no |
| `get_insider_transactions` | crypto, db-paper, default, hermes-paper-kis, kiwoom, kiwoom_kr, us-paper | `fundamentals_handlers` | no |
| `get_investor_trends` | crypto, db-paper, default, hermes-paper-kis, kiwoom, kiwoom_kr, us-paper | `fundamentals_handlers` | no |
| `get_market_reports` | crypto, db-paper, default, hermes-paper-kis, kiwoom, kiwoom_kr, us-paper | `market_brief_tools` | no |
| `get_paper_performance` | db-paper | `paper_analytics_registration` | no |
| `get_paper_trade_log` | db-paper | `paper_analytics_registration` | no |
| `get_retrospective_aggregate` | crypto, db-paper, default, hermes-paper-kis, kiwoom, kiwoom_kr, us-paper | `trade_retrospective_tools` | no |
| `get_short_interest` | crypto, db-paper, default, hermes-paper-kis, kiwoom, kiwoom_kr, us-paper | `fundamentals_handlers` | no |
| `get_trading_scoreboard` | crypto, db-paper, default, hermes-paper-kis, kiwoom, kiwoom_kr, us-paper | `trading_scoreboard_tools` | no |
| `get_user_setting` | crypto, db-paper, default, hermes-paper-kis, kiwoom, kiwoom_kr, us-paper | `user_settings_tools` | no |
| `investment_report_activate_watch` | crypto, db-paper, default, hermes-paper-kis, kiwoom, kiwoom_kr, us-paper | `investment_reports_handlers` | watch |
| `investment_report_add_items` | crypto, db-paper, default, hermes-paper-kis, kiwoom, kiwoom_kr, us-paper | `investment_reports_handlers` | report |
| `investment_report_context_get` | crypto, db-paper, default, hermes-paper-kis, kiwoom, kiwoom_kr, us-paper | `investment_reports_handlers` | no |
| `investment_report_decide_item` | crypto, db-paper, default, hermes-paper-kis, kiwoom, kiwoom_kr, us-paper | `investment_reports_handlers` | report |
| `investment_report_delta_get` | crypto, db-paper, default, hermes-paper-kis, kiwoom, kiwoom_kr, us-paper | `investment_reports_handlers` | no |
| `investment_report_list` | crypto, db-paper, default, hermes-paper-kis, kiwoom, kiwoom_kr, us-paper | `investment_reports_handlers` | no |
| `investment_report_prepare_intraday_context` | crypto, db-paper, default, hermes-paper-kis, kiwoom, kiwoom_kr, us-paper | `investment_hermes_handlers` | no |
| `investment_report_set_status` | crypto, db-paper, default, hermes-paper-kis, kiwoom, kiwoom_kr, us-paper | `investment_reports_handlers` | report |
| `investment_report_update` | crypto, db-paper, default, hermes-paper-kis, kiwoom, kiwoom_kr, us-paper | `investment_reports_handlers` | report |
| `investment_watch_expire` | crypto, db-paper, default, hermes-paper-kis, kiwoom, kiwoom_kr, us-paper | `investment_reports_handlers` | watch |
| `investment_watch_recommend` | crypto, db-paper, default, hermes-paper-kis, kiwoom, kiwoom_kr, us-paper | `investment_reports_handlers` | no |
| `investment_watch_void` | crypto, db-paper, default, hermes-paper-kis, kiwoom, kiwoom_kr, us-paper | `investment_reports_handlers` | watch |
| `list_active_journals` | crypto, db-paper, default, hermes-paper-kis, kiwoom, kiwoom_kr, us-paper | `trade_journal_registration` | no |
| `order_proposal_expire_sweep` | crypto, db-paper, default, hermes-paper-kis, kiwoom, kiwoom_kr, tradingcodex_execution, us-paper | `order_proposal_tools` | order, proposal |
| `order_proposal_redispatch` | crypto, db-paper, default, hermes-paper-kis, kiwoom, kiwoom_kr, tradingcodex_execution, us-paper | `order_proposal_tools` | no |
| `paper_cancel_pending_order` | default | `paper_limit_order_handler` | order |
| `paper_execution_cancel_order` | paper_execution | `paper_execution_registration` | order |
| `paper_execution_get_capabilities` | paper_execution | `paper_execution_registration` | no |
| `paper_execution_get_order` | paper_execution | `paper_execution_registration` | no |
| `paper_execution_preview_order` | paper_execution | `paper_execution_registration` | no |
| `paper_execution_reconcile` | paper_execution | `paper_execution_registration` | persistence |
| `paper_execution_submit_order` | paper_execution | `paper_execution_registration` | order |
| `paper_validation_advance` | paper_execution | `paper_validation_registration` | order |
| `paper_validation_append_hypothesis` | paper_execution | `paper_validation_registration` | order |
| `paper_validation_append_review` | paper_execution | `paper_validation_registration` | order |
| `paper_validation_authorize_order_submit` | paper_execution | `paper_validation_registration` | order |
| `paper_validation_confirm_promotion` | paper_execution | `paper_validation_registration` | order |
| `paper_validation_get_audit` | paper_execution | `paper_validation_registration` | no |
| `paper_validation_register` | paper_execution | `paper_validation_registration` | order |
| `paper_validation_reject_or_abort` | paper_execution | `paper_validation_registration` | order |
| `recommend_go_live` | db-paper | `paper_journal_bridge` | no |
| `research_summary_get` | crypto, db-paper, default, hermes-paper-kis, kiwoom, kiwoom_kr, us-paper | `analysis_registration` | no |
| `reset_paper_account` | db-paper | `paper_account_registration` | persistence |
| `save_position_intake_retrospective` | default | `trade_retrospective_tools` | persistence |
| `save_trade_journal` | crypto, db-paper, default, hermes-paper-kis, kiwoom, kiwoom_kr, us-paper | `trade_journal_tools` | persistence |
| `set_user_setting` | crypto, db-paper, default, hermes-paper-kis, kiwoom, kiwoom_kr, us-paper | `user_settings_tools` | persistence |
| `stage_analysis_get` | crypto, db-paper, default, hermes-paper-kis, kiwoom, kiwoom_kr, us-paper | `analysis_registration` | no |
| `sweep_expired_watches` | crypto, db-paper, default, hermes-paper-kis, kiwoom, kiwoom_kr, us-paper | `investment_reports_handlers` | no |
| `update_manual_holdings` | crypto, db-paper, default, hermes-paper-kis, kiwoom, kiwoom_kr, us-paper | `portfolio_holdings` | persistence |
| `update_trade_journal` | crypto, db-paper, default, hermes-paper-kis, kiwoom, kiwoom_kr, us-paper | `trade_journal_tools` | persistence |

## 4. Class-D tools deliberately **kept**

Removing these would have broken something the audit's evidence could not see.
Each is tagged `niche` (section 6) so the next audit gets call evidence for it.

| Tool | Why it stays |
|---|---|
| `get_toss_ai_signal` | named by 6 lane manifests in `config/mcp_lane_allowlists/` (`basis=prompt`) |
| `get_toss_buy_balance` | same 6 lane manifests |
| `investment_report_create_from_hermes_composition` | same 6 lane manifests |
| `alpaca_paper_automated_preview_order` | mints the only `approval_token` its class-C partner `alpaca_paper_automated_submit_order` accepts; removing it alone leaves a documented two-step flow uncallable |
| `get_sector_peers` | a step in the discovery lane of `docs/playbooks/trading-decision-playbook.md`, which `route_request` serves and two lane tests verify line-by-line against `LANE_SEQUENCES` |

Note on the first three: the audit's table records `Prompt refs = 0` for them,
yet the lane drafts record `basis=prompt`. Both are outputs of the same script —
`find_exact_references` scans fixed operator-repo roots, while
`build_lane_allowlists` scans the same repo through per-lane globs, so the two
disagree. The lane manifest is the stricter signal and wins, per the rule that
no removal may break a lane allowlist.

## 5. Deleted modules

Deleted only after a grep over `app/`, `tests/` and `scripts/` for the tool name
and its handler name showed no remaining reference.

| File | Why |
|---|---|
| `app/mcp_server/tooling/paper_execution_registration.py` | 6/6 tools class D |
| `app/mcp_server/tooling/paper_validation_registration.py` | 8/8 tools class D |
| `app/mcp_server/tooling/analysis_bundle_handlers.py` | 2/2 tools class D |
| `app/mcp_server/tooling/paper_analytics_registration.py` | 3/3 tools class D |
| `app/mcp_server/tooling/paper_journal_bridge.py` | 2/2 tools class D |
| `app/mcp_server/tooling/paper_journal_registration.py` | registers only those 2 |
| `app/mcp_server/tooling/trading_scoreboard_tools.py` | 1/1 tool class D, no other importer |
| `app/mcp_server/tooling/trading_scoreboard_registration.py` | registers only that 1 |
| `app/mcp_server/tooling/user_settings_registration.py` | 2/2 tools class D (`user_settings_tools.py` stays — see section 7) |

Deleted test files: `tests/mcp_server/test_analysis_bundle_tools.py`,
`tests/mcp_server/tooling/test_paper_execution_registration.py`,
`tests/mcp_server/tooling/test_paper_validation_registration.py`,
`tests/test_mcp_trading_scoreboard.py`, `tests/test_paper_analytics_tools.py`,
`tests/test_paper_journal_bridge.py`. Each was removed from its
`ci_shards/shard-*.txt` entry in the same commit, so exact-cover stays green.

## 6. Niche isolation (class C)

`app/mcp_server/tooling/niche_tools.py`. `NicheTaggingMCP` wraps the `mcp`
object at the top of `register_all_tools`, **before the first profile branch**,
so the allowlist profiles that return early are covered too. A niche call
emits one `mcp.niche_tool_called` warning and sets the Sentry tag
`mcp.niche=true`; arguments, return values and exceptions pass through
untouched.

`CLASS_C_TOOL_NAMES` (79 names) is asserted equal to the audit table's own class
column by `tests/mcp_server/test_niche_tool_isolation.py`, so it cannot drift
into a hand-maintained list. `RETAINED_CLASS_D_TOOL_NAMES` holds the five
section-4 tools as a separate named set — tagging them is an observation, not a
re-grade.

One class-C tool is now registered by **no** profile:
`paper_cohort_kill_switch`, whose only carrier was `paper_execution`. Its
handler and registrar are kept; re-exposing it needs a deliberate profile
assignment.

## 7. Orphaned code (kept, not deleted)

`app/services/**` was not touched. These are reachable in Python but no longer
reachable through MCP:

**Fully unreferenced after the drop (candidates for a later, separate cleanup):**

- `app/mcp_server/tooling/analysis_tool_handlers.py`: `analyze_portfolio_impl`,
  `get_dividends_impl`
- `app/mcp_server/tooling/research_pipeline_read.py`: `stage_analysis_get_impl`,
  `research_summary_get_impl`
- `app/mcp_server/tooling/fundamentals/_financials.py`: `handle_get_financials`,
  `handle_get_insider_transactions`
- `app/mcp_server/tooling/fundamentals/_valuation.py`:
  `handle_get_investor_trends`, `handle_get_short_interest`
- `app/mcp_server/tooling/fundamentals/_analyst_consensus.py`:
  `handle_get_analyst_consensus`
- `app/mcp_server/tooling/portfolio_holdings.py`: `_update_manual_holdings_impl`
- `app/mcp_server/tooling/trade_retrospective_tools.py`:
  `get_retrospective_aggregate`
- `app/mcp_server/tooling/investment_reports_handlers.py`: the 12 `*_impl`
  coroutines behind the removed report/watch tools
- `app/services/paper_trading_service.py`: `create_account`, `reset_account`,
  `delete_account` and the analytics/journal-bridge queries
- `app/services/analysis_snapshot_bundle/**` and
  `app/schemas/analysis_snapshot_bundle.py` (capture + read services)
- `app/services/trade_journal/aggregates.py`: `build_trading_scoreboard` is
  still used by `operating_briefing`, but the scoreboard *tool* wrapper is gone

**Kept because a live non-MCP caller still needs them:**

- `run_order_proposal_expire_sweep` — `app/tasks/order_proposal_expiry_tasks.py`
- `run_expired_watches_sweep` — `app/tasks/watch_expiry_tasks.py`
- `get_user_setting` — `portfolio_cash`, `account_routing_tools`
- `save_position_intake_retrospective` (service) —
  `app/services/trade_journal/trade_retrospective_service.py`
- `PAPER_EXECUTION_ENABLED` — `app/jobs/paper_cohort.py`,
  `app/services/paper_cohort/runner.py`

**Kept because a doc/code string still names the tool:**
the `order_proposal_expire_sweep` and `order_proposal_redispatch` coroutines
stay in `order_proposal_tools.py` — see section 8.

## 8. Documents and code strings that still name a removed tool

Not edited (per the "list only, do not delete documentation" rule). Two groups:

### 8a. Live operator-facing strings — needs a follow-up decision

| Where | What it says |
|---|---|
| `app/services/order_proposals/alerts.py:44,49,54,58` | four alert bodies tell an operator to run `order_proposal_redispatch(dry_run=true)`; that tool is no longer registered |
| `app/services/watch_trigger_repricing/capability.py:39` | documents `order_proposal_redispatch` as a deliberately excluded tool (harmless, but now vacuous) |
| `CLAUDE.md` "Investment Report Item Contract" | names `investment_report_add_items`; `investment_report_create` takes the same `items` list inline, so the contract still holds through create |
| `app/core/config.py` | the comment on `order_proposal_expire_sweep_enabled` names the removed tool |

The audit could not have caught the first two: its `code_refs` scan covers
`scripts/`, `app/flows/` and `app/tasks/` — not `app/services/`.

### 8b. Historical plan/spec records — no action

`docs/plans/**`, `docs/superpowers/plans/**`, `docs/superpowers/specs/**`,
`docs/post-mortems/**` and `docs/archive/**` contain several hundred mentions
across about 30 removed tools (largest: `save_trade_journal` 65,
`compare_strategies` 63, `recommend_go_live` 60, `update_trade_journal` 41,
`create_paper_account` 35, `get_investor_trends` 34,
`get_trading_scoreboard` 33). These are dated records of decisions, not live
contracts; rewriting them would falsify the history.

`app/mcp_server/README.md` still describes many removed tools in its older
sections. Rather than rewrite it wholesale, a banner at the top of that file
lists every removed tool and points here.

## 9. Lane allowlists

`lane-allowlists.draft/*.txt` promoted verbatim to
`config/mcp_lane_allowlists/*.txt` (11 lanes, 98 distinct tools, tab-separated
`tool` and `basis` preserved). Every line carries a non-empty `basis`, so no
line needed the "empty basis" note.

`tests/mcp_server/test_lane_allowlist_contract.py` asserts every lane's tools
are registered by its assigned profiles. **Union, not intersection**: a lane
with several assigned profiles runs under one of them per session, and the
drafts were derived with exactly that reading (`build_lane_allowlists` uses
`any(profile in ...)`). Reading it as an intersection would demand Alpaca tools
from a KIS-only profile — the four multi-profile lanes fail immediately at the
unmodified baseline under that reading. A second test asserts the strict
per-profile property for the seven single-profile lanes, where the two readings
coincide. A third ties the lane-to-profile literal to
`scripts/mcp_tool_usage_audit.LANE_SPECS` so they cannot drift.

## 10. Contract tests

| Test | What it pins |
|---|---|
| `tests/mcp_server/test_lane_allowlist_contract.py` | no removal may drop a tool a live lane names |
| `tests/mcp_server/test_profile_tool_snapshot.py` + `data/mcp_profile_tool_snapshot.json` | the exact per-profile tool set; any change must move the snapshot in the same commit |
| `tests/mcp_server/test_niche_tool_isolation.py` | the niche warning, the Sentry tag, pass-through behavior, the set matching the audit's class column, and that the proxy is applied before the first profile branch |

All three measure the surface the way the audit did: the real
`register_all_tools` run against an in-memory recorder with feature gates
enabled. No server, client, broker, or DB.

## 11. Regenerating the profile snapshot

After an intentional surface change:

```python
import json
from scripts.mcp_tool_usage_audit import collect_registry

by_profile = {}
for tool, entry in collect_registry().items():
    for profile in entry["profiles"]:
        by_profile.setdefault(profile, []).append(tool)

with open("tests/mcp_server/data/mcp_profile_tool_snapshot.json", "w") as fh:
    json.dump(
        {p: sorted(v) for p, v in sorted(by_profile.items())},
        fh,
        indent=2,
        sort_keys=True,
    )
    fh.write("\n")
```

The registry half of the audit needs no secrets; only its Sentry usage half
does.
