# MCP surface cleanup — 2026-09-05

The sole classification authority is [Complete classification](../mcp-tool-usage-audit-20260903.md#complete-classification), dated 2026-09-03. Registration tests execute the real registrar in memory; no server or broker is started.

The original 14 profiles / 228 distinct tools become 13 profiles / 192 distinct tools. **35 distinct D tools / 126 D profile registrations** are removed. **78 distinct C tools / 339 profile registrations** use the `niche` group. 10 modules and 6 dedicated test files are deleted.

## Decisions and preserved contracts

- Feature gates, per-call confirmations, broker/ledger/watch business logic and `app/services/**` are unchanged. No scheduler is registered. No environment files, credentials, migrations or trading policy are changed.
- The lane-conflict default preserves D tools `get_toss_ai_signal`, `get_toss_buy_balance`, and `investment_report_create_from_hermes_composition` on their original profiles. Their D classification is unchanged.
- The mixed-test default preserves the following additional D tools. Existing regression tests exercise them alongside retained tools or shared contracts; those tests and paths are retained. This is a removal exception, not reclassification:

| Preserved D tool | Reason |
|---|---|
| `alpaca_paper_automated_preview_order` | Mixed retained-tool regression contract (PROMPT default §4) |
| `alpaca_paper_reconcile_orders` | Mixed retained-tool regression contract (PROMPT default §4) |
| `analyze_portfolio` | Mixed retained-tool regression contract (PROMPT default §4) |
| `create_paper_account` | Mixed retained-tool regression contract (PROMPT default §4) |
| `delete_paper_account` | Mixed retained-tool regression contract (PROMPT default §4) |
| `get_analyst_consensus` | Mixed retained-tool regression contract (PROMPT default §4) |
| `get_retrospective_aggregate` | Mixed retained-tool regression contract (PROMPT default §4) |
| `investment_report_activate_watch` | Mixed retained-tool regression contract (PROMPT default §4) |
| `investment_report_add_items` | Mixed retained-tool regression contract (PROMPT default §4) |
| `investment_report_context_get` | Mixed retained-tool regression contract (PROMPT default §4) |
| `investment_report_decide_item` | Mixed retained-tool regression contract (PROMPT default §4) |
| `investment_report_delta_get` | Mixed retained-tool regression contract (PROMPT default §4) |
| `investment_report_list` | Mixed retained-tool regression contract (PROMPT default §4) |
| `investment_report_prepare_intraday_context` | Mixed retained-tool regression contract (PROMPT default §4) |
| `investment_report_set_status` | Mixed retained-tool regression contract (PROMPT default §4) |
| `investment_report_update` | Mixed retained-tool regression contract (PROMPT default §4) |
| `investment_watch_expire` | Mixed retained-tool regression contract (PROMPT default §4) |
| `investment_watch_recommend` | Mixed retained-tool regression contract (PROMPT default §4) |
| `investment_watch_void` | Mixed retained-tool regression contract (PROMPT default §4) |
| `list_active_journals` | Mixed retained-tool regression contract (PROMPT default §4) |
| `order_proposal_expire_sweep` | Mixed retained-tool regression contract (PROMPT default §4) |
| `order_proposal_redispatch` | Mixed retained-tool regression contract (PROMPT default §4) |
| `reset_paper_account` | Mixed retained-tool regression contract (PROMPT default §4) |
| `save_position_intake_retrospective` | Mixed retained-tool regression contract (PROMPT default §4) |
| `save_trade_journal` | Mixed retained-tool regression contract (PROMPT default §4) |
| `sweep_expired_watches` | Mixed retained-tool regression contract (PROMPT default §4) |
| `update_trade_journal` | Mixed retained-tool regression contract (PROMPT default §4) |

- `paper_execution` is rejected by profile resolution. All 14 D names disappear with the profile. Its sole C tool, `paper_cohort_kill_switch`, keeps its handler, actor-role lookup and serializer but has no active MCP profile. It is not moved to another surface. Existing services and default-off flags remain for their other consumers.
- Registrar-owned name sets and the existing Kiwoom inventory-count snapshot are synchronized with actual registration. A/B/C behavior assertions remain. Tests dedicated solely to removed D tools are deleted; shared service tests stay.
- An in-memory test temporarily toggles local settings only to enumerate both gate-on and gate-off inventories. Runtime gate defaults are never changed.

## Profile counts

| Profile | Before | After | Removed D | Removed C with profile | Niche C |
|---|---:|---:|---:|---:|---:|
| `account_read` | 10 | 10 | 0 | 0 | 1 |
| `alpaca-paper-clean` | 13 | 13 | 0 | 0 | 5 |
| `analysis_readonly` | 28 | 27 | 1 | 0 | 1 |
| `crypto` | 142 | 127 | 15 | 0 | 41 |
| `db-paper` | 143 | 123 | 20 | 0 | 35 |
| `default` | 202 | 186 | 16 | 0 | 76 |
| `hermes-paper-kis` | 138 | 123 | 15 | 0 | 37 |
| `kiwoom` | 149 | 134 | 15 | 0 | 43 |
| `kiwoom_kr` | 141 | 126 | 15 | 0 | 38 |
| `paper_execution` | 15 | 0 | 14 | 1 | 0 |
| `shadow-replay` | 3 | 3 | 0 | 0 | 1 |
| `tradingcodex_execution` | 43 | 43 | 0 | 0 | 14 |
| `us-paper` | 157 | 142 | 15 | 0 | 47 |
| `watch_repricing` | 15 | 15 | 0 | 0 | 0 |

## Removed profile × tool registrations

| Profile | Tool | Audit class |
|---|---|---|
| `analysis_readonly` | `analysis_bundle_get` | D |
| `crypto` | `analysis_bundle_create` | D |
| `crypto` | `analysis_bundle_get` | D |
| `crypto` | `get_dividends` | D |
| `crypto` | `get_financials` | D |
| `crypto` | `get_insider_transactions` | D |
| `crypto` | `get_investor_trends` | D |
| `crypto` | `get_market_reports` | D |
| `crypto` | `get_sector_peers` | D |
| `crypto` | `get_short_interest` | D |
| `crypto` | `get_trading_scoreboard` | D |
| `crypto` | `get_user_setting` | D |
| `crypto` | `research_summary_get` | D |
| `crypto` | `set_user_setting` | D |
| `crypto` | `stage_analysis_get` | D |
| `crypto` | `update_manual_holdings` | D |
| `db-paper` | `analysis_bundle_create` | D |
| `db-paper` | `analysis_bundle_get` | D |
| `db-paper` | `compare_paper_accounts` | D |
| `db-paper` | `compare_strategies` | D |
| `db-paper` | `get_dividends` | D |
| `db-paper` | `get_financials` | D |
| `db-paper` | `get_insider_transactions` | D |
| `db-paper` | `get_investor_trends` | D |
| `db-paper` | `get_market_reports` | D |
| `db-paper` | `get_paper_performance` | D |
| `db-paper` | `get_paper_trade_log` | D |
| `db-paper` | `get_sector_peers` | D |
| `db-paper` | `get_short_interest` | D |
| `db-paper` | `get_trading_scoreboard` | D |
| `db-paper` | `get_user_setting` | D |
| `db-paper` | `recommend_go_live` | D |
| `db-paper` | `research_summary_get` | D |
| `db-paper` | `set_user_setting` | D |
| `db-paper` | `stage_analysis_get` | D |
| `db-paper` | `update_manual_holdings` | D |
| `default` | `analysis_bundle_create` | D |
| `default` | `analysis_bundle_get` | D |
| `default` | `get_dividends` | D |
| `default` | `get_financials` | D |
| `default` | `get_insider_transactions` | D |
| `default` | `get_investor_trends` | D |
| `default` | `get_market_reports` | D |
| `default` | `get_sector_peers` | D |
| `default` | `get_short_interest` | D |
| `default` | `get_trading_scoreboard` | D |
| `default` | `get_user_setting` | D |
| `default` | `paper_cancel_pending_order` | D |
| `default` | `research_summary_get` | D |
| `default` | `set_user_setting` | D |
| `default` | `stage_analysis_get` | D |
| `default` | `update_manual_holdings` | D |
| `hermes-paper-kis` | `analysis_bundle_create` | D |
| `hermes-paper-kis` | `analysis_bundle_get` | D |
| `hermes-paper-kis` | `get_dividends` | D |
| `hermes-paper-kis` | `get_financials` | D |
| `hermes-paper-kis` | `get_insider_transactions` | D |
| `hermes-paper-kis` | `get_investor_trends` | D |
| `hermes-paper-kis` | `get_market_reports` | D |
| `hermes-paper-kis` | `get_sector_peers` | D |
| `hermes-paper-kis` | `get_short_interest` | D |
| `hermes-paper-kis` | `get_trading_scoreboard` | D |
| `hermes-paper-kis` | `get_user_setting` | D |
| `hermes-paper-kis` | `research_summary_get` | D |
| `hermes-paper-kis` | `set_user_setting` | D |
| `hermes-paper-kis` | `stage_analysis_get` | D |
| `hermes-paper-kis` | `update_manual_holdings` | D |
| `kiwoom` | `analysis_bundle_create` | D |
| `kiwoom` | `analysis_bundle_get` | D |
| `kiwoom` | `get_dividends` | D |
| `kiwoom` | `get_financials` | D |
| `kiwoom` | `get_insider_transactions` | D |
| `kiwoom` | `get_investor_trends` | D |
| `kiwoom` | `get_market_reports` | D |
| `kiwoom` | `get_sector_peers` | D |
| `kiwoom` | `get_short_interest` | D |
| `kiwoom` | `get_trading_scoreboard` | D |
| `kiwoom` | `get_user_setting` | D |
| `kiwoom` | `research_summary_get` | D |
| `kiwoom` | `set_user_setting` | D |
| `kiwoom` | `stage_analysis_get` | D |
| `kiwoom` | `update_manual_holdings` | D |
| `kiwoom_kr` | `analysis_bundle_create` | D |
| `kiwoom_kr` | `analysis_bundle_get` | D |
| `kiwoom_kr` | `get_dividends` | D |
| `kiwoom_kr` | `get_financials` | D |
| `kiwoom_kr` | `get_insider_transactions` | D |
| `kiwoom_kr` | `get_investor_trends` | D |
| `kiwoom_kr` | `get_market_reports` | D |
| `kiwoom_kr` | `get_sector_peers` | D |
| `kiwoom_kr` | `get_short_interest` | D |
| `kiwoom_kr` | `get_trading_scoreboard` | D |
| `kiwoom_kr` | `get_user_setting` | D |
| `kiwoom_kr` | `research_summary_get` | D |
| `kiwoom_kr` | `set_user_setting` | D |
| `kiwoom_kr` | `stage_analysis_get` | D |
| `kiwoom_kr` | `update_manual_holdings` | D |
| `paper_execution` | `paper_cohort_kill_switch` | C |
| `paper_execution` | `paper_execution_cancel_order` | D |
| `paper_execution` | `paper_execution_get_capabilities` | D |
| `paper_execution` | `paper_execution_get_order` | D |
| `paper_execution` | `paper_execution_preview_order` | D |
| `paper_execution` | `paper_execution_reconcile` | D |
| `paper_execution` | `paper_execution_submit_order` | D |
| `paper_execution` | `paper_validation_advance` | D |
| `paper_execution` | `paper_validation_append_hypothesis` | D |
| `paper_execution` | `paper_validation_append_review` | D |
| `paper_execution` | `paper_validation_authorize_order_submit` | D |
| `paper_execution` | `paper_validation_confirm_promotion` | D |
| `paper_execution` | `paper_validation_get_audit` | D |
| `paper_execution` | `paper_validation_register` | D |
| `paper_execution` | `paper_validation_reject_or_abort` | D |
| `us-paper` | `analysis_bundle_create` | D |
| `us-paper` | `analysis_bundle_get` | D |
| `us-paper` | `get_dividends` | D |
| `us-paper` | `get_financials` | D |
| `us-paper` | `get_insider_transactions` | D |
| `us-paper` | `get_investor_trends` | D |
| `us-paper` | `get_market_reports` | D |
| `us-paper` | `get_sector_peers` | D |
| `us-paper` | `get_short_interest` | D |
| `us-paper` | `get_trading_scoreboard` | D |
| `us-paper` | `get_user_setting` | D |
| `us-paper` | `research_summary_get` | D |
| `us-paper` | `set_user_setting` | D |
| `us-paper` | `stage_analysis_get` | D |
| `us-paper` | `update_manual_holdings` | D |

## Deleted modules

- `app/mcp_server/tooling/analysis_bundle_handlers.py`
- `app/mcp_server/tooling/fundamentals/_sector_peers.py`
- `app/mcp_server/tooling/paper_analytics_registration.py`
- `app/mcp_server/tooling/paper_execution_registration.py`
- `app/mcp_server/tooling/paper_journal_bridge.py`
- `app/mcp_server/tooling/paper_journal_registration.py`
- `app/mcp_server/tooling/paper_validation_registration.py`
- `app/mcp_server/tooling/trading_scoreboard_registration.py`
- `app/mcp_server/tooling/trading_scoreboard_tools.py`
- `app/mcp_server/tooling/user_settings_registration.py`

## Service functions preserved after losing their MCP caller

No service function is deleted. A static scan of `app/` and `scripts/` finds no remaining runtime construction/call site for these entry points outside their own service package/export. Tests still exercise them:

- `app/services/analysis_snapshot_bundle/capture.py::AnalysisBundleCaptureService.capture`
- `app/services/analysis_snapshot_bundle/read.py::AnalysisBundleReadService.get`

Other removed facades delegated to existing paper execution / validation services or performed ORM reads themselves. Shared `build_trading_scoreboard` / `build_counterfactual_delta_scoreboard` remain consumed by operating briefing and decision history; `get_user_setting` remains an internal account-routing/cash dependency. `ConfiguredActorRoleProvider` and `jsonable` remain required by the C cohort-control handler. These are not deleted as orphans.

## Niche group and invocation observation

FastMCP `tags={"niche"}` marks each audited C registration inside its existing profile. Calls attempt exactly one structured WARNING `mcp.niche_tool_called` with only the public `tool` name. No arguments, results, identities or exception strings are logged by the observer. The call-local Sentry scope and current span receive `mcp.niche="true"`; the parent scope is restored on return, exception and cancellation. Telemetry setup/log failures do not replace handler results or exceptions. Sync and async execution kinds, signatures, schemas and existing account pins are preserved.

The span tag intentionally remains on the observed span. A later outer Sentry error catcher is not guaranteed to inherit the already-restored function scope; this change does not emit duplicate error events or change request middleware.

| Profile | Niche tools |
|---|---|
| `account_read` | `get_order_history` |
| `alpaca-paper-clean` | `alpaca_paper_get_order`, `alpaca_paper_ledger_get`, `alpaca_paper_list_assets`, `alpaca_paper_preview_order`, `alpaca_paper_roundtrip_report` |
| `analysis_readonly` | `discover_buy_candidates_fanout` |
| `crypto` | `buy_ladder_fill_preview`, `cancel_order`, `discover_buy_candidates_fanout`, `get_company_profile`, `get_correlation`, `get_crypto_funding_rate`, `get_crypto_market_regime`, `get_crypto_open_interest`, `get_crypto_profile`, `get_crypto_social`, `get_execution_strength`, `get_forecast_calibration`, `get_investment_opinions`, `get_latest_market_brief`, `get_market_issues`, `get_market_news`, `get_mock_loop_retrospective`, `get_order_history`, `get_retail_sentiment`, `get_theme_events`, `get_upbit_index`, `get_valuation`, `investment_report_create`, `investment_report_generate_from_bundle`, `investment_report_get_hermes_context`, `investment_report_prepare_bundle`, `investment_snapshot_bundle_get`, `investment_snapshot_bundle_list`, `investment_snapshot_list`, `investment_stage_artifacts_ingest_from_hermes`, `kis_mock_mirror_execute_report`, `kis_mock_reconciliation_run`, `live_reconcile_orders`, `modify_journal_entry`, `modify_order`, `order_proposal_list_expired_defensive`, `place_order`, `research_session_get`, `research_session_list_recent`, `support_reserve_net_consume`, `watch_downside_register_sweep` |
| `db-paper` | `discover_buy_candidates_fanout`, `get_company_profile`, `get_correlation`, `get_crypto_funding_rate`, `get_crypto_market_regime`, `get_crypto_open_interest`, `get_crypto_profile`, `get_crypto_social`, `get_execution_strength`, `get_forecast_calibration`, `get_investment_opinions`, `get_latest_market_brief`, `get_market_issues`, `get_market_news`, `get_mock_loop_retrospective`, `get_retail_sentiment`, `get_theme_events`, `get_upbit_index`, `get_valuation`, `investment_report_create`, `investment_report_generate_from_bundle`, `investment_report_get_hermes_context`, `investment_report_prepare_bundle`, `investment_snapshot_bundle_get`, `investment_snapshot_bundle_list`, `investment_snapshot_list`, `investment_stage_artifacts_ingest_from_hermes`, `kis_mock_mirror_execute_report`, `list_paper_accounts`, `modify_journal_entry`, `order_proposal_list_expired_defensive`, `research_session_get`, `research_session_list_recent`, `support_reserve_net_consume`, `watch_downside_register_sweep` |
| `default` | `alpaca_paper_cancel_order`, `alpaca_paper_get_order`, `alpaca_paper_ledger_get`, `alpaca_paper_list_assets`, `alpaca_paper_preview_order`, `alpaca_paper_roundtrip_report`, `alpaca_paper_submit_order`, `buy_ladder_fill_preview`, `cancel_order`, `discover_buy_candidates_fanout`, `get_company_profile`, `get_correlation`, `get_crypto_funding_rate`, `get_crypto_market_regime`, `get_crypto_open_interest`, `get_crypto_profile`, `get_crypto_social`, `get_execution_strength`, `get_forecast_calibration`, `get_investment_opinions`, `get_latest_market_brief`, `get_market_issues`, `get_market_news`, `get_mock_loop_retrospective`, `get_order_history`, `get_retail_sentiment`, `get_theme_events`, `get_upbit_index`, `get_valuation`, `investment_report_create`, `investment_report_generate_from_bundle`, `investment_report_get_hermes_context`, `investment_report_prepare_bundle`, `investment_snapshot_bundle_get`, `investment_snapshot_bundle_list`, `investment_snapshot_list`, `investment_stage_artifacts_ingest_from_hermes`, `kis_live_cancel_order`, `kis_live_modify_order`, `kis_live_place_order`, `kis_live_reconcile_orders`, `kis_mock_cancel_order`, `kis_mock_mirror_execute_report`, `kis_mock_modify_order`, `kis_mock_place_order`, `kis_mock_reconciliation_run`, `kiwoom_mock_cancel_order`, `kiwoom_mock_get_order_detail`, `kiwoom_mock_modify_order`, `kiwoom_mock_place_order`, `kiwoom_mock_preview_order`, `kiwoom_mock_us_cancel_order`, `kiwoom_mock_us_modify_order`, `kiwoom_mock_us_place_order`, `kiwoom_mock_us_preview_order`, `live_reconcile_orders`, `market_quote_snapshot_ensure`, `market_quote_snapshot_latest`, `modify_journal_entry`, `modify_order`, `order_proposal_list_expired_defensive`, `paper_list_pending_orders`, `paper_place_limit_order`, `paper_reconcile_orders`, `place_order`, `research_session_get`, `research_session_list_recent`, `support_reserve_net_consume`, `toss_cancel_order`, `toss_detect_manual_activity`, `toss_modify_order`, `toss_place_order`, `us_dual_paper_account_states`, `us_dual_paper_capability_matrix`, `us_dual_paper_preview`, `watch_downside_register_sweep` |
| `hermes-paper-kis` | `discover_buy_candidates_fanout`, `get_company_profile`, `get_correlation`, `get_crypto_funding_rate`, `get_crypto_market_regime`, `get_crypto_open_interest`, `get_crypto_profile`, `get_crypto_social`, `get_execution_strength`, `get_forecast_calibration`, `get_investment_opinions`, `get_latest_market_brief`, `get_market_issues`, `get_market_news`, `get_mock_loop_retrospective`, `get_retail_sentiment`, `get_theme_events`, `get_upbit_index`, `get_valuation`, `investment_report_create`, `investment_report_generate_from_bundle`, `investment_report_get_hermes_context`, `investment_report_prepare_bundle`, `investment_snapshot_bundle_get`, `investment_snapshot_bundle_list`, `investment_snapshot_list`, `investment_stage_artifacts_ingest_from_hermes`, `kis_mock_cancel_order`, `kis_mock_mirror_execute_report`, `kis_mock_modify_order`, `kis_mock_place_order`, `modify_journal_entry`, `order_proposal_list_expired_defensive`, `research_session_get`, `research_session_list_recent`, `support_reserve_net_consume`, `watch_downside_register_sweep` |
| `kiwoom` | `discover_buy_candidates_fanout`, `get_company_profile`, `get_correlation`, `get_crypto_funding_rate`, `get_crypto_market_regime`, `get_crypto_open_interest`, `get_crypto_profile`, `get_crypto_social`, `get_execution_strength`, `get_forecast_calibration`, `get_investment_opinions`, `get_latest_market_brief`, `get_market_issues`, `get_market_news`, `get_mock_loop_retrospective`, `get_retail_sentiment`, `get_theme_events`, `get_upbit_index`, `get_valuation`, `investment_report_create`, `investment_report_generate_from_bundle`, `investment_report_get_hermes_context`, `investment_report_prepare_bundle`, `investment_snapshot_bundle_get`, `investment_snapshot_bundle_list`, `investment_snapshot_list`, `investment_stage_artifacts_ingest_from_hermes`, `kis_mock_mirror_execute_report`, `kiwoom_mock_cancel_order`, `kiwoom_mock_get_order_detail`, `kiwoom_mock_modify_order`, `kiwoom_mock_place_order`, `kiwoom_mock_preview_order`, `kiwoom_mock_us_cancel_order`, `kiwoom_mock_us_modify_order`, `kiwoom_mock_us_place_order`, `kiwoom_mock_us_preview_order`, `modify_journal_entry`, `order_proposal_list_expired_defensive`, `research_session_get`, `research_session_list_recent`, `support_reserve_net_consume`, `watch_downside_register_sweep` |
| `kiwoom_kr` | `discover_buy_candidates_fanout`, `get_company_profile`, `get_correlation`, `get_crypto_funding_rate`, `get_crypto_market_regime`, `get_crypto_open_interest`, `get_crypto_profile`, `get_crypto_social`, `get_execution_strength`, `get_forecast_calibration`, `get_investment_opinions`, `get_latest_market_brief`, `get_market_issues`, `get_market_news`, `get_mock_loop_retrospective`, `get_retail_sentiment`, `get_theme_events`, `get_upbit_index`, `get_valuation`, `investment_report_create`, `investment_report_generate_from_bundle`, `investment_report_get_hermes_context`, `investment_report_prepare_bundle`, `investment_snapshot_bundle_get`, `investment_snapshot_bundle_list`, `investment_snapshot_list`, `investment_stage_artifacts_ingest_from_hermes`, `kiwoom_mock_cancel_order`, `kiwoom_mock_get_order_detail`, `kiwoom_mock_modify_order`, `kiwoom_mock_place_order`, `kiwoom_mock_preview_order`, `modify_journal_entry`, `order_proposal_list_expired_defensive`, `research_session_get`, `research_session_list_recent`, `support_reserve_net_consume`, `watch_downside_register_sweep` |
| `shadow-replay` | `investment_report_get_hermes_context` |
| `tradingcodex_execution` | `buy_ladder_fill_preview`, `cancel_order`, `get_order_history`, `kis_live_cancel_order`, `kis_live_place_order`, `kiwoom_mock_cancel_order`, `kiwoom_mock_modify_order`, `kiwoom_mock_place_order`, `kiwoom_mock_preview_order`, `order_proposal_list_expired_defensive`, `place_order`, `support_reserve_net_consume`, `toss_cancel_order`, `toss_place_order` |
| `us-paper` | `alpaca_paper_automated_submit_order`, `alpaca_paper_cancel_order`, `alpaca_paper_get_order`, `alpaca_paper_ledger_get`, `alpaca_paper_list_assets`, `alpaca_paper_preview_order`, `alpaca_paper_roundtrip_report`, `alpaca_paper_submit_order`, `discover_buy_candidates_fanout`, `get_company_profile`, `get_correlation`, `get_crypto_funding_rate`, `get_crypto_market_regime`, `get_crypto_open_interest`, `get_crypto_profile`, `get_crypto_social`, `get_execution_strength`, `get_forecast_calibration`, `get_investment_opinions`, `get_latest_market_brief`, `get_market_issues`, `get_market_news`, `get_mock_loop_retrospective`, `get_retail_sentiment`, `get_theme_events`, `get_upbit_index`, `get_valuation`, `investment_report_create`, `investment_report_generate_from_bundle`, `investment_report_get_hermes_context`, `investment_report_prepare_bundle`, `investment_snapshot_bundle_get`, `investment_snapshot_bundle_list`, `investment_snapshot_list`, `investment_stage_artifacts_ingest_from_hermes`, `kis_mock_mirror_execute_report`, `market_quote_snapshot_ensure`, `market_quote_snapshot_latest`, `modify_journal_entry`, `order_proposal_list_expired_defensive`, `research_session_get`, `research_session_list_recent`, `support_reserve_net_consume`, `us_dual_paper_account_states`, `us_dual_paper_capability_matrix`, `us_dual_paper_preview`, `watch_downside_register_sweep` |
| `watch_repricing` | — |

## Promoted lane contracts

`lane-allowlists.draft/<lane>.txt` → `config/mcp_lane_allowlists/<lane>.txt`, byte-for-byte, including the `tool<TAB>basis` column and the empty `shadow-crypto` lane. There are 11 lane files and no rows with an empty basis. Lane→Profiles mapping comes directly from the audit and uses the union of the assigned profiles.

- `tests/mcp_server/test_lane_allowlist_contract.py` checks real registration plus reviewed bytes, row counts and lane coverage.
- `tests/mcp_server/test_profile_tool_snapshot.py` freezes both gate states, checks audit-derived retained sets, and rejects deleted module files/imports. The original 228-tool snapshot remains in commit `e071786aa`.
- `tests/mcp_server/test_niche_tools.py` checks every C profile/name pair, real FastMCP schemas/defaults, actual fan-out and account-history forwarding, one warning, scope concurrency, failure/cancellation and clean-account pinning.

## Documentation references requiring follow-up

The existing `docs/**` strings below are retained as requested. Their presence does not change the audit classification. Historical audit/data references are evidence, not an active deployment instruction. Active operator/runbook references need owner review; external operator prompts were not edited. Exact file/line/tool occurrences follow.

| Document | References (`line:tool`) |
|---|---|
| `docs/invest/data-source-contract.md` | `40:get_financials` |
| `docs/mcp-tool-usage-audit-20260903-data/profile-unlisted.json` | `30:analysis_bundle_get`, `34:analysis_bundle_create`, `35:analysis_bundle_get`, `54:get_dividends`, `57:get_financials`, `60:get_insider_transactions`, `63:get_investor_trends`, `65:get_market_reports`, `72:get_sector_peers`, `73:get_short_interest`, `78:get_trading_scoreboard`, `81:get_user_setting`, `112:research_summary_get`, `118:set_user_setting`, `119:stage_analysis_get`, `123:update_manual_holdings`, `131:analysis_bundle_create`, `132:analysis_bundle_get`, `136:compare_paper_accounts`, `137:compare_strategies`, `162:get_dividends`, `165:get_financials`, `172:get_insider_transactions`, `175:get_investor_trends`, `182:get_market_reports`, `189:get_paper_performance`, `190:get_paper_trade_log`, `196:get_sector_peers`, `197:get_short_interest`, `207:get_trading_scoreboard`, `210:get_user_setting`, `248:recommend_go_live`, `251:research_summary_get`, `262:set_user_setting`, `263:stage_analysis_get`, `268:update_manual_holdings`, `285:analysis_bundle_create`, `286:analysis_bundle_get`, `306:get_dividends`, `308:get_financials`, `311:get_insider_transactions`, `313:get_investor_trends`, `315:get_market_reports`, `322:get_sector_peers`, `323:get_short_interest`, `328:get_trading_scoreboard`, `331:get_user_setting`, `375:paper_cancel_pending_order`, `378:research_summary_get`, `385:set_user_setting`, `386:stage_analysis_get`, `391:update_manual_holdings`, `400:analysis_bundle_create`, `401:analysis_bundle_get`, `427:get_dividends`, `430:get_financials`, `437:get_insider_transactions`, `440:get_investor_trends`, `447:get_market_reports`, `458:get_sector_peers`, `459:get_short_interest`, `469:get_trading_scoreboard`, `472:get_user_setting`, `513:research_summary_get`, `522:set_user_setting`, `523:stage_analysis_get`, `528:update_manual_holdings`, `534:analysis_bundle_create`, `535:analysis_bundle_get`, `556:get_dividends`, `559:get_financials`, `562:get_insider_transactions`, `565:get_investor_trends`, `572:get_market_reports`, `579:get_sector_peers`, `580:get_short_interest`, `588:get_trading_scoreboard`, `590:get_user_setting`, `635:research_summary_get`, `640:set_user_setting`, `641:stage_analysis_get`, `646:update_manual_holdings`, `654:analysis_bundle_create`, `655:analysis_bundle_get`, `681:get_dividends`, `684:get_financials`, `691:get_insider_transactions`, `694:get_investor_trends`, `701:get_market_reports`, `713:get_sector_peers`, `714:get_short_interest`, `724:get_trading_scoreboard`, `727:get_user_setting`, `773:research_summary_get`, `783:set_user_setting`, `784:stage_analysis_get`, `789:update_manual_holdings`, `794:paper_cohort_kill_switch`, `795:paper_execution_cancel_order`, `796:paper_execution_get_capabilities`, `797:paper_execution_get_order`, `798:paper_execution_preview_order`, `799:paper_execution_reconcile`, `800:paper_execution_submit_order`, `801:paper_validation_advance`, `802:paper_validation_append_hypothesis`, `803:paper_validation_append_review`, `804:paper_validation_authorize_order_submit`, `805:paper_validation_confirm_promotion`, `806:paper_validation_get_audit`, `807:paper_validation_register`, `808:paper_validation_reject_or_abort`, `878:analysis_bundle_create`, `879:analysis_bundle_get`, `905:get_dividends`, `908:get_financials`, `915:get_insider_transactions`, `918:get_investor_trends`, `925:get_market_reports`, `936:get_sector_peers`, `937:get_short_interest`, `947:get_trading_scoreboard`, `950:get_user_setting`, `989:research_summary_get`, `998:set_user_setting`, `999:stage_analysis_get`, `1004:update_manual_holdings` |
| `docs/mcp-tool-usage-audit-20260903-data/references.json` | `362:analysis_bundle_create`, `367:analysis_bundle_get`, `499:compare_paper_accounts`, `504:compare_strategies`, `804:get_dividends`, `840:get_financials`, `947:get_insider_transactions`, `980:get_investor_trends`, `1057:get_market_reports`, `1201:get_paper_performance`, `1206:get_paper_trade_log`, `1317:get_sector_peers`, `1322:get_short_interest`, `1451:get_trading_scoreboard`, `1474:get_user_setting`, `2178:paper_cancel_pending_order`, `2183:paper_cohort_kill_switch`, `2190:paper_execution_cancel_order`, `2195:paper_execution_get_capabilities`, `2200:paper_execution_get_order`, `2205:paper_execution_preview_order`, `2210:paper_execution_reconcile`, `2215:paper_execution_submit_order`, `2247:paper_validation_advance`, `2252:paper_validation_append_hypothesis`, `2257:paper_validation_append_review`, `2262:paper_validation_authorize_order_submit`, `2267:paper_validation_confirm_promotion`, `2272:paper_validation_get_audit`, `2277:paper_validation_register`, `2282:paper_validation_reject_or_abort`, `2325:recommend_go_live`, `2346:research_summary_get`, `2612:set_user_setting`, `2617:stage_analysis_get`, `2773:update_manual_holdings` |
| `docs/mcp-tool-usage-audit-20260903-data/registry.json` | `250:analysis_bundle_create`, `267:analysis_bundle_get`, `362:compare_paper_accounts`, `371:compare_strategies`, `740:get_dividends`, `785:get_financials`, `900:get_insider_transactions`, `946:get_investor_trends`, `1054:get_market_reports`, `1174:get_paper_performance`, `1183:get_paper_trade_log`, `1269:get_sector_peers`, `1284:get_short_interest`, `1443:get_trading_scoreboard`, `1488:get_user_setting`, `2437:paper_cancel_pending_order`, `2448:paper_cohort_kill_switch`, `2459:paper_execution_cancel_order`, `2470:paper_execution_get_capabilities`, `2479:paper_execution_get_order`, `2488:paper_execution_preview_order`, `2497:paper_execution_reconcile`, `2508:paper_execution_submit_order`, `2550:paper_validation_advance`, `2561:paper_validation_append_hypothesis`, `2572:paper_validation_append_review`, `2583:paper_validation_authorize_order_submit`, `2594:paper_validation_confirm_promotion`, `2605:paper_validation_get_audit`, `2614:paper_validation_register`, `2625:paper_validation_reject_or_abort`, `2649:recommend_go_live`, `2688:research_summary_get`, `2887:set_user_setting`, `2904:stage_analysis_get`, `3083:update_manual_holdings` |
| `docs/mcp-tool-usage-audit-20260903-data/usage-90d.json` | `2147:analysis_bundle_create`, `2236:analysis_bundle_get`, `2941:compare_paper_accounts`, `3030:compare_strategies`, `5769:get_dividends`, `6081:get_financials`, `6983:get_insider_transactions`, `7286:get_investor_trends`, `8057:get_market_reports`, `9005:get_paper_performance`, `9094:get_paper_trade_log`, `9762:get_sector_peers`, `9851:get_short_interest`, `10988:get_trading_scoreboard`, `11308:get_user_setting`, `17880:paper_cancel_pending_order`, `17969:paper_cohort_kill_switch`, `18058:paper_execution_cancel_order`, `18147:paper_execution_get_capabilities`, `18236:paper_execution_get_order`, `18325:paper_execution_preview_order`, `18414:paper_execution_reconcile`, `18503:paper_execution_submit_order`, `18859:paper_validation_advance`, `18948:paper_validation_append_hypothesis`, `19037:paper_validation_append_review`, `19126:paper_validation_authorize_order_submit`, `19215:paper_validation_confirm_promotion`, `19304:paper_validation_get_audit`, `19393:paper_validation_register`, `19482:paper_validation_reject_or_abort`, `19660:recommend_go_live`, `19927:research_summary_get`, `21481:set_user_setting`, `21570:stage_analysis_get`, `23045:update_manual_holdings` |
| `docs/mcp-tool-usage-audit-20260903.md` | `35:analysis_bundle_get`, `36:analysis_bundle_create`, `36:analysis_bundle_get`, `36:get_dividends`, `36:get_financials`, `36:get_insider_transactions`, `36:get_investor_trends`, `36:get_market_reports`, `36:get_sector_peers`, `36:get_short_interest`, `36:get_trading_scoreboard`, `36:get_user_setting`, `36:research_summary_get`, `36:set_user_setting`, `36:stage_analysis_get`, `36:update_manual_holdings`, `37:analysis_bundle_create`, `37:analysis_bundle_get`, `37:compare_paper_accounts`, `37:compare_strategies`, `37:get_dividends`, `37:get_financials`, `37:get_insider_transactions`, `37:get_investor_trends`, `37:get_market_reports`, `37:get_paper_performance`, `37:get_paper_trade_log`, `37:get_sector_peers`, `37:get_short_interest`, `37:get_trading_scoreboard`, `37:get_user_setting`, `37:recommend_go_live`, `37:research_summary_get`, `37:set_user_setting`, `37:stage_analysis_get`, `37:update_manual_holdings`, `38:analysis_bundle_create`, `38:analysis_bundle_get`, `38:get_dividends`, `38:get_financials`, `38:get_insider_transactions`, `38:get_investor_trends`, `38:get_market_reports`, `38:get_sector_peers`, `38:get_short_interest`, `38:get_trading_scoreboard`, `38:get_user_setting`, `38:paper_cancel_pending_order`, `38:research_summary_get`, `38:set_user_setting`, `38:stage_analysis_get`, `38:update_manual_holdings`, `39:analysis_bundle_create`, `39:analysis_bundle_get`, `39:get_dividends`, `39:get_financials`, `39:get_insider_transactions`, `39:get_investor_trends`, `39:get_market_reports`, `39:get_sector_peers`, `39:get_short_interest`, `39:get_trading_scoreboard`, `39:get_user_setting`, `39:research_summary_get`, `39:set_user_setting`, `39:stage_analysis_get`, `39:update_manual_holdings`, `40:analysis_bundle_create`, `40:analysis_bundle_get`, `40:get_dividends`, `40:get_financials`, `40:get_insider_transactions`, `40:get_investor_trends`, `40:get_market_reports`, `40:get_sector_peers`, `40:get_short_interest`, `40:get_trading_scoreboard`, `40:get_user_setting`, `40:research_summary_get`, `40:set_user_setting`, `40:stage_analysis_get`, `40:update_manual_holdings`, `41:analysis_bundle_create`, `41:analysis_bundle_get`, `41:get_dividends`, `41:get_financials`, `41:get_insider_transactions`, `41:get_investor_trends`, `41:get_market_reports`, `41:get_sector_peers`, `41:get_short_interest`, `41:get_trading_scoreboard`, `41:get_user_setting`, `41:research_summary_get`, `41:set_user_setting`, `41:stage_analysis_get`, `41:update_manual_holdings`, `42:paper_cohort_kill_switch`, `42:paper_execution_cancel_order`, `42:paper_execution_get_capabilities`, `42:paper_execution_get_order`, `42:paper_execution_preview_order`, `42:paper_execution_reconcile`, `42:paper_execution_submit_order`, `42:paper_validation_advance`, `42:paper_validation_append_hypothesis`, `42:paper_validation_append_review`, `42:paper_validation_authorize_order_submit`, `42:paper_validation_confirm_promotion`, `42:paper_validation_get_audit`, `42:paper_validation_register`, `42:paper_validation_reject_or_abort`, `45:analysis_bundle_create`, `45:analysis_bundle_get`, `45:get_dividends`, `45:get_financials`, `45:get_insider_transactions`, `45:get_investor_trends`, `45:get_market_reports`, `45:get_sector_peers`, `45:get_short_interest`, `45:get_trading_scoreboard`, `45:get_user_setting`, `45:research_summary_get`, `45:set_user_setting`, `45:stage_analysis_get`, `45:update_manual_holdings`, `73:analysis_bundle_create`, `74:analysis_bundle_get`, `81:compare_paper_accounts`, `82:compare_strategies`, `107:get_dividends`, `110:get_financials`, `117:get_insider_transactions`, `120:get_investor_trends`, `127:get_market_reports`, `135:get_paper_performance`, `136:get_paper_trade_log`, `142:get_sector_peers`, `143:get_short_interest`, `153:get_trading_scoreboard`, `156:get_user_setting`, `223:paper_cancel_pending_order`, `224:paper_cohort_kill_switch`, `225:paper_execution_cancel_order`, `226:paper_execution_get_capabilities`, `227:paper_execution_get_order`, `228:paper_execution_preview_order`, `229:paper_execution_reconcile`, `230:paper_execution_submit_order`, `234:paper_validation_advance`, `235:paper_validation_append_hypothesis`, `236:paper_validation_append_review`, `237:paper_validation_authorize_order_submit`, `238:paper_validation_confirm_promotion`, `239:paper_validation_get_audit`, `240:paper_validation_register`, `241:paper_validation_reject_or_abort`, `243:recommend_go_live`, `246:research_summary_get`, `259:set_user_setting`, `260:stage_analysis_get`, `274:update_manual_holdings`, `305:analysis_bundle_get`, `306:analysis_bundle_create`, `306:analysis_bundle_get`, `306:get_dividends`, `306:get_financials`, `306:get_insider_transactions`, `306:get_investor_trends`, `306:get_market_reports`, `306:get_sector_peers`, `306:get_short_interest`, `306:get_trading_scoreboard`, `306:get_user_setting`, `306:research_summary_get`, `306:set_user_setting`, `306:stage_analysis_get`, `306:update_manual_holdings`, `307:analysis_bundle_create`, `307:analysis_bundle_get`, `307:compare_paper_accounts`, `307:compare_strategies`, `307:get_dividends`, `307:get_financials`, `307:get_insider_transactions`, `307:get_investor_trends`, `307:get_market_reports`, `307:get_paper_performance`, `307:get_paper_trade_log`, `307:get_sector_peers`, `307:get_short_interest`, `307:get_trading_scoreboard`, `307:get_user_setting`, `307:recommend_go_live`, `307:research_summary_get`, `307:set_user_setting`, `307:stage_analysis_get`, `307:update_manual_holdings`, `308:analysis_bundle_create`, `308:analysis_bundle_get`, `308:get_dividends`, `308:get_financials`, `308:get_insider_transactions`, `308:get_investor_trends`, `308:get_market_reports`, `308:get_sector_peers`, `308:get_short_interest`, `308:get_trading_scoreboard`, `308:get_user_setting`, `308:paper_cancel_pending_order`, `308:research_summary_get`, `308:set_user_setting`, `308:stage_analysis_get`, `308:update_manual_holdings`, `309:analysis_bundle_create`, `309:analysis_bundle_get`, `309:get_dividends`, `309:get_financials`, `309:get_insider_transactions`, `309:get_investor_trends`, `309:get_market_reports`, `309:get_sector_peers`, `309:get_short_interest`, `309:get_trading_scoreboard`, `309:get_user_setting`, `309:research_summary_get`, `309:set_user_setting`, `309:stage_analysis_get`, `309:update_manual_holdings`, `310:analysis_bundle_create`, `310:analysis_bundle_get`, `310:get_dividends`, `310:get_financials`, `310:get_insider_transactions`, `310:get_investor_trends`, `310:get_market_reports`, `310:get_sector_peers`, `310:get_short_interest`, `310:get_trading_scoreboard`, `310:get_user_setting`, `310:research_summary_get`, `310:set_user_setting`, `310:stage_analysis_get`, `310:update_manual_holdings`, `311:analysis_bundle_create`, `311:analysis_bundle_get`, `311:get_dividends`, `311:get_financials`, `311:get_insider_transactions`, `311:get_investor_trends`, `311:get_market_reports`, `311:get_sector_peers`, `311:get_short_interest`, `311:get_trading_scoreboard`, `311:get_user_setting`, `311:research_summary_get`, `311:set_user_setting`, `311:stage_analysis_get`, `311:update_manual_holdings`, `312:paper_cohort_kill_switch`, `312:paper_execution_cancel_order`, `312:paper_execution_get_capabilities`, `312:paper_execution_get_order`, `312:paper_execution_preview_order`, `312:paper_execution_reconcile`, `312:paper_execution_submit_order`, `312:paper_validation_advance`, `312:paper_validation_append_hypothesis`, `312:paper_validation_append_review`, `312:paper_validation_authorize_order_submit`, `312:paper_validation_confirm_promotion`, `312:paper_validation_get_audit`, `312:paper_validation_register`, `312:paper_validation_reject_or_abort`, `315:analysis_bundle_create`, `315:analysis_bundle_get`, `315:get_dividends`, `315:get_financials`, `315:get_insider_transactions`, `315:get_investor_trends`, `315:get_market_reports`, `315:get_sector_peers`, `315:get_short_interest`, `315:get_trading_scoreboard`, `315:get_user_setting`, `315:research_summary_get`, `315:set_user_setting`, `315:stage_analysis_get`, `315:update_manual_holdings` |
| `docs/plans/2026-02-17-dca-removal-implementation-plan.md` | `53:update_manual_holdings`, `67:update_manual_holdings` |
| `docs/plans/2026-03-10-issue-261-get-short-interest-kis-implementation-plan.md` | `1:get_short_interest`, `5:get_short_interest`, `7:get_short_interest`, `216:get_short_interest`, `268:get_short_interest`, `291:get_short_interest`, `346:get_short_interest`, `370:get_short_interest`, `381:get_short_interest`, `390:get_short_interest`, `488:get_short_interest` |
| `docs/plans/2026-04-01-get-available-capital-implementation-plan.md` | `129:get_user_setting`, `129:set_user_setting`, `146:get_user_setting`, `147:set_user_setting`, `156:get_user_setting`, `162:get_user_setting`, `162:set_user_setting`, `273:get_user_setting`, `273:set_user_setting`, `317:get_user_setting` |
| `docs/plans/ROB-112-research-pipeline-plan.md` | `49:research_summary_get`, `49:stage_analysis_get`, `1036:stage_analysis_get`, `1037:research_summary_get` |
| `docs/plans/ROB-316-nautilustrader-adoption-spike-plan.md` | `374:compare_strategies`, `410:compare_strategies`, `444:compare_strategies` |
| `docs/plans/ROB-320-validated-signal-research-pipeline.md` | `76:compare_strategies`, `679:compare_strategies`, `1248:compare_strategies` |
| `docs/plans/ROB-351-cost-blind-funnel-campaign.md` | `18:compare_strategies` |
| `docs/plans/ROB-512-gap3-kr-sector-master-lazy-fill-spec.md` | `133:get_sector_peers` |
| `docs/plans/ROB-56-review-report.md` | `123:update_manual_holdings` |
| `docs/plans/ROB-668-nxt-tradable-mcp-preflight.md` | `816:get_user_setting` |
| `docs/plans/ROB-688-sector-peers-bounded-fanout.md` | `1:get_sector_peers`, `5:get_sector_peers`, `11:get_sector_peers`, `134:get_sector_peers`, `163:get_sector_peers`, `292:get_sector_peers`, `422:get_sector_peers`, `516:get_sector_peers`, `531:get_sector_peers`, `788:get_sector_peers`, `794:get_sector_peers` |
| `docs/playbooks/trading-decision-playbook.md` | `386:get_sector_peers`, `416:get_sector_peers` |
| `docs/post-mortems/2026-04-17-scout-depth-variance.md` | `36:get_financials`, `36:get_sector_peers` |
| `docs/runbooks/paper-cohort-kill-switch.md` | `19:paper_cohort_kill_switch` |
| `docs/superpowers/plans/2026-03-27-split-fundamentals-handlers.md` | `477:get_financials`, `477:get_insider_transactions`, `482:get_financials`, `482:get_insider_transactions`, `638:get_investor_trends`, `638:get_short_interest`, `645:get_investor_trends`, `645:get_short_interest`, `788:get_short_interest`, `1197:get_sector_peers`, `1202:get_sector_peers`, `1273:get_sector_peers`, `1361:get_financials`, `1362:get_insider_transactions`, `1364:get_investor_trends`, `1367:get_short_interest`, `1372:get_sector_peers`, `1415:get_financials`, `1421:get_financials`, `1430:get_insider_transactions`, `1436:get_insider_transactions`, `1457:get_investor_trends`, `1463:get_investor_trends`, `1498:get_short_interest`, `1505:get_short_interest`, `1567:get_sector_peers`, `1573:get_sector_peers` |
| `docs/superpowers/plans/2026-04-13-mcp-portfolio-paper-trading-support.md` | `1584:set_user_setting`, `1585:get_user_setting` |
| `docs/superpowers/plans/2026-04-13-paper-strategy-journal-integration.md` | `22:compare_strategies`, `22:recommend_go_live`, `1672:compare_strategies`, `1678:compare_strategies`, `1684:compare_strategies`, `1778:compare_strategies`, `1818:compare_strategies`, `1871:compare_strategies`, `1889:compare_strategies`, `1897:compare_strategies`, `2038:compare_strategies`, `2039:compare_strategies`, `2051:compare_strategies`, `2056:recommend_go_live`, `2062:recommend_go_live`, `2068:recommend_go_live`, `2142:recommend_go_live`, `2166:recommend_go_live`, `2190:recommend_go_live`, `2210:recommend_go_live`, `2225:recommend_go_live`, `2237:recommend_go_live`, `2245:recommend_go_live`, `2365:recommend_go_live`, `2366:recommend_go_live`, `2378:recommend_go_live`, `2383:compare_strategies`, `2383:recommend_go_live`, `2394:compare_strategies`, `2394:recommend_go_live`, `2401:compare_strategies`, `2402:recommend_go_live`, `2409:compare_strategies`, `2410:recommend_go_live`, `2416:compare_strategies`, `2424:compare_strategies`, `2427:recommend_go_live`, `2434:recommend_go_live`, `2469:compare_strategies`, `2470:recommend_go_live`, `2494:compare_strategies`, `2494:recommend_go_live` |
| `docs/superpowers/plans/2026-04-13-paper-strategy-remaining-tasks.md` | `5:compare_strategies`, `5:recommend_go_live`, `7:compare_strategies`, `7:recommend_go_live`, `17:compare_strategies`, `17:recommend_go_live`, `27:compare_strategies`, `27:recommend_go_live`, `64:compare_strategies`, `74:compare_strategies`, `74:recommend_go_live`, `149:compare_strategies`, `189:compare_strategies`, `223:compare_strategies`, `241:compare_strategies`, `272:compare_strategies`, `299:compare_strategies`, `313:compare_strategies`, `326:compare_strategies`, `371:compare_strategies`, `507:compare_strategies`, `508:compare_strategies`, `520:compare_strategies`, `525:recommend_go_live`, `530:recommend_go_live`, `553:recommend_go_live`, `585:recommend_go_live`, `622:recommend_go_live`, `659:recommend_go_live`, `694:recommend_go_live`, `725:recommend_go_live`, `746:recommend_go_live`, `779:recommend_go_live`, `789:recommend_go_live`, `793:recommend_go_live`, `798:recommend_go_live`, `803:recommend_go_live`, `927:recommend_go_live`, `928:recommend_go_live`, `940:recommend_go_live`, `945:compare_strategies`, `945:recommend_go_live`, `965:compare_strategies`, `966:recommend_go_live`, `989:compare_strategies`, `989:recommend_go_live`, `996:compare_strategies`, `997:recommend_go_live`, `1004:compare_strategies`, `1005:recommend_go_live`, `1011:compare_strategies`, `1019:compare_strategies`, `1022:recommend_go_live`, `1029:recommend_go_live`, `1060:compare_strategies`, `1060:recommend_go_live` |
| `docs/superpowers/plans/2026-05-08-invest-desktop-mvp.md` | `1372:get_market_reports`, `1385:get_market_reports`, `1414:get_market_reports`, `1553:get_market_reports` |
| `docs/superpowers/plans/2026-06-09-rob-469-pr1-mcp-observe-detect.md` | `337:get_market_reports`, `355:get_market_reports` |
| `docs/superpowers/plans/2026-06-09-rob-469-pr2-mcp-harden-loop.md` | `213:get_financials`, `216:get_sector_peers` |
| `docs/superpowers/plans/2026-06-10-rob-492-intraday-investor-flow.md` | `7:get_investor_trends`, `515:get_investor_trends`, `533:get_investor_trends`, `537:get_short_interest`, `546:get_investor_trends`, `567:get_investor_trends`, `652:get_investor_trends` |
| `docs/superpowers/plans/2026-06-11-rob-509-manual-holdings-dry-run-preview.md` | `5:update_manual_holdings`, `629:update_manual_holdings` |
| `docs/superpowers/plans/2026-06-26-rob-626-intraday-investor-flow-reliability.md` | `5:get_investor_trends`, `7:get_investor_trends`, `35:get_investor_trends`, `109:get_investor_trends`, `341:get_investor_trends`, `349:get_investor_trends`, `353:get_investor_trends`, `372:get_investor_trends`, `420:get_investor_trends` |
| `docs/superpowers/plans/2026-07-02-rob-649-route-request-lane-router.md` | `253:get_sector_peers`, `333:get_dividends`, `336:get_financials`, `343:get_insider_transactions`, `346:get_investor_trends`, `352:get_market_reports`, `364:get_sector_peers`, `365:get_short_interest`, `375:get_user_setting`, `393:research_summary_get`, `401:set_user_setting`, `402:stage_analysis_get`, `405:update_manual_holdings` |
| `docs/superpowers/plans/2026-07-04-upbit-shadow-sim.md` | `365:paper_cancel_pending_order`, `380:paper_cancel_pending_order`, `409:paper_cancel_pending_order`, `410:paper_cancel_pending_order`, `454:get_paper_performance` |
| `docs/superpowers/plans/2026-07-05-rob-713-trade-journal-aggregates.md` | `7:get_trading_scoreboard`, `981:get_trading_scoreboard`, `992:get_trading_scoreboard`, `993:get_trading_scoreboard`, `1001:get_trading_scoreboard`, `1006:get_trading_scoreboard`, `1039:get_trading_scoreboard`, `1067:get_trading_scoreboard`, `1069:get_trading_scoreboard`, `1074:get_trading_scoreboard`, `1084:get_trading_scoreboard`, `1112:get_trading_scoreboard`, `1230:get_trading_scoreboard` |
| `docs/superpowers/plans/2026-07-05-rob-717-decision-history-scoreboard-fanout.md` | `439:get_trading_scoreboard`, `456:get_trading_scoreboard` |
| `docs/superpowers/plans/2026-07-06-rob-734-mirror-counterfactual.md` | `7:get_trading_scoreboard`, `1247:get_trading_scoreboard`, `1568:get_trading_scoreboard`, `1775:get_trading_scoreboard`, `1823:get_trading_scoreboard` |
| `docs/superpowers/plans/2026-07-06-rob-744-mirror-pairing-cohort-closure.md` | `9:get_trading_scoreboard`, `30:get_trading_scoreboard`, `763:get_trading_scoreboard`, `787:get_trading_scoreboard`, `810:get_trading_scoreboard`, `817:get_trading_scoreboard`, `850:get_trading_scoreboard` |
| `docs/superpowers/plans/2026-07-12-rob-838-analysis-snapshot-bundle.md` | `13:analysis_bundle_get`, `22:analysis_bundle_create`, `22:analysis_bundle_get`, `668:analysis_bundle_create`, `668:analysis_bundle_get`, `670:analysis_bundle_get`, `721:analysis_bundle_get`, `725:analysis_bundle_create`, `872:analysis_bundle_get` |
| `docs/superpowers/specs/2026-04-13-paper-strategy-journal-integration-design.md` | `128:compare_strategies`, `132:recommend_go_live`, `232:compare_strategies`, `237:compare_strategies`, `304:recommend_go_live`, `309:recommend_go_live`, `357:compare_strategies`, `359:compare_strategies`, `376:compare_strategies`, `377:recommend_go_live`, `394:compare_strategies`, `394:recommend_go_live`, `419:compare_strategies`, `427:recommend_go_live` |
| `docs/superpowers/specs/2026-05-08-invest-desktop-mvp-design.md` | `202:get_market_reports` |
| `docs/superpowers/specs/2026-06-09-rob-469-mcp-server-resilience-design.md` | `164:get_financials` |
| `docs/superpowers/specs/2026-06-10-rob-492-intraday-investor-flow-design.md` | `7:get_investor_trends`, `11:get_investor_trends`, `37:get_investor_trends`, `41:get_investor_trends`, `93:get_investor_trends` |
| `docs/superpowers/specs/2026-06-25-rob-626-intraday-investor-flow-reliability-design.md` | `36:get_investor_trends`, `45:get_investor_trends`, `138:get_investor_trends`, `147:get_investor_trends`, `151:get_investor_trends`, `195:get_investor_trends` |
| `docs/superpowers/specs/2026-07-02-rob-646-trading-policy-yaml-design.md` | `26:set_user_setting` |
| `docs/superpowers/specs/2026-07-04-upbit-shadow-sim-design.md` | `14:compare_paper_accounts`, `14:get_paper_performance`, `14:get_paper_trade_log`, `39:paper_cancel_pending_order`, `83:paper_cancel_pending_order` |
| `docs/superpowers/specs/2026-07-05-rob-713-trade-journal-aggregates-design.md` | `130:get_trading_scoreboard`, `170:get_trading_scoreboard`, `178:get_trading_scoreboard` |
| `docs/superpowers/specs/2026-07-05-rob-717-decision-history-scoreboard-fanout-design.md` | `58:get_trading_scoreboard` |
| `docs/superpowers/specs/2026-07-12-rob-838-analysis-snapshot-bundle-design.md` | `83:analysis_bundle_create`, `84:analysis_bundle_get`, `86:analysis_bundle_get`, `127:analysis_bundle_create`, `127:analysis_bundle_get` |
| `docs/superpowers/specs/2026-07-13-rob-820-mock-data-truthfulness-design.md` | `58:get_financials` |

## Verification

Run `DEV_ENV_FILE=/dev/null make lint`, `DEV_ENV_FILE=/dev/null make typecheck`, `uv run pytest tests/mcp_server -q`, the ROB-501 guard, full non-live collection and CI exact-cover. Mutation evidence must be an assertion failure, not an import/collection error. The delivery report contains original command output, four restored mutants, exact commit/remote SHA and all six required GitHub checks. No deployment is part of this draft PR.
