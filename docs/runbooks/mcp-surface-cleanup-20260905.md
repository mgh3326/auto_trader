# MCP surface cleanup — 2026-09-05

The sole classification authority is [Complete classification](../mcp-tool-usage-audit-20260903.md#complete-classification), dated 2026-09-03. Registration tests execute the real registrar in memory; no server or broker is started.

The original 14 profiles / 228 distinct tools become 13 profiles / 206 distinct tools. **21 distinct D tools / 34 D profile registrations** are removed. **78 distinct C tools / 339 profile registrations** use the `niche` group. 6 modules and 5 dedicated test files are deleted.

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
| `get_dividends` | Mixed retained-tool regression contract (PROMPT default §4) |
| `get_financials` | Mixed retained-tool regression contract (PROMPT default §4) |
| `get_insider_transactions` | Mixed retained-tool regression contract (PROMPT default §4) |
| `get_investor_trends` | Mixed retained-tool regression contract (PROMPT default §4) |
| `get_market_reports` | Mixed retained-tool regression contract (PROMPT default §4) |
| `get_retrospective_aggregate` | Mixed retained-tool regression contract (PROMPT default §4) |
| `get_sector_peers` | Mixed retained-tool regression contract (PROMPT default §4) |
| `get_short_interest` | Mixed retained-tool regression contract (PROMPT default §4) |
| `get_trading_scoreboard` | Mixed retained-tool regression contract (PROMPT default §4) |
| `get_user_setting` | Mixed retained-tool regression contract (PROMPT default §4) |
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
| `paper_cancel_pending_order` | Mixed retained-tool regression contract (PROMPT default §4) |
| `research_summary_get` | Mixed retained-tool regression contract (PROMPT default §4) |
| `reset_paper_account` | Mixed retained-tool regression contract (PROMPT default §4) |
| `save_position_intake_retrospective` | Mixed retained-tool regression contract (PROMPT default §4) |
| `save_trade_journal` | Mixed retained-tool regression contract (PROMPT default §4) |
| `set_user_setting` | Mixed retained-tool regression contract (PROMPT default §4) |
| `stage_analysis_get` | Mixed retained-tool regression contract (PROMPT default §4) |
| `sweep_expired_watches` | Mixed retained-tool regression contract (PROMPT default §4) |
| `update_manual_holdings` | Mixed retained-tool regression contract (PROMPT default §4) |
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
| `crypto` | 142 | 140 | 2 | 0 | 41 |
| `db-paper` | 143 | 136 | 7 | 0 | 35 |
| `default` | 202 | 200 | 2 | 0 | 76 |
| `hermes-paper-kis` | 138 | 136 | 2 | 0 | 37 |
| `kiwoom` | 149 | 147 | 2 | 0 | 43 |
| `kiwoom_kr` | 141 | 139 | 2 | 0 | 38 |
| `paper_execution` | 15 | 0 | 14 | 1 | 0 |
| `shadow-replay` | 3 | 3 | 0 | 0 | 1 |
| `tradingcodex_execution` | 43 | 43 | 0 | 0 | 14 |
| `us-paper` | 157 | 155 | 2 | 0 | 47 |
| `watch_repricing` | 15 | 15 | 0 | 0 | 0 |

## Removed profile × tool registrations

| Profile | Tool | Audit class |
|---|---|---|
| `analysis_readonly` | `analysis_bundle_get` | D |
| `crypto` | `analysis_bundle_create` | D |
| `crypto` | `analysis_bundle_get` | D |
| `db-paper` | `analysis_bundle_create` | D |
| `db-paper` | `analysis_bundle_get` | D |
| `db-paper` | `compare_paper_accounts` | D |
| `db-paper` | `compare_strategies` | D |
| `db-paper` | `get_paper_performance` | D |
| `db-paper` | `get_paper_trade_log` | D |
| `db-paper` | `recommend_go_live` | D |
| `default` | `analysis_bundle_create` | D |
| `default` | `analysis_bundle_get` | D |
| `hermes-paper-kis` | `analysis_bundle_create` | D |
| `hermes-paper-kis` | `analysis_bundle_get` | D |
| `kiwoom` | `analysis_bundle_create` | D |
| `kiwoom` | `analysis_bundle_get` | D |
| `kiwoom_kr` | `analysis_bundle_create` | D |
| `kiwoom_kr` | `analysis_bundle_get` | D |
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

## Deleted modules

- `app/mcp_server/tooling/analysis_bundle_handlers.py`
- `app/mcp_server/tooling/paper_analytics_registration.py`
- `app/mcp_server/tooling/paper_execution_registration.py`
- `app/mcp_server/tooling/paper_journal_bridge.py`
- `app/mcp_server/tooling/paper_journal_registration.py`
- `app/mcp_server/tooling/paper_validation_registration.py`

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
| `docs/mcp-tool-usage-audit-20260903-data/profile-unlisted.json` | `30:analysis_bundle_get`, `34:analysis_bundle_create`, `35:analysis_bundle_get`, `131:analysis_bundle_create`, `132:analysis_bundle_get`, `136:compare_paper_accounts`, `137:compare_strategies`, `189:get_paper_performance`, `190:get_paper_trade_log`, `248:recommend_go_live`, `285:analysis_bundle_create`, `286:analysis_bundle_get`, `400:analysis_bundle_create`, `401:analysis_bundle_get`, `534:analysis_bundle_create`, `535:analysis_bundle_get`, `654:analysis_bundle_create`, `655:analysis_bundle_get`, `794:paper_cohort_kill_switch`, `795:paper_execution_cancel_order`, `796:paper_execution_get_capabilities`, `797:paper_execution_get_order`, `798:paper_execution_preview_order`, `799:paper_execution_reconcile`, `800:paper_execution_submit_order`, `801:paper_validation_advance`, `802:paper_validation_append_hypothesis`, `803:paper_validation_append_review`, `804:paper_validation_authorize_order_submit`, `805:paper_validation_confirm_promotion`, `806:paper_validation_get_audit`, `807:paper_validation_register`, `808:paper_validation_reject_or_abort`, `878:analysis_bundle_create`, `879:analysis_bundle_get` |
| `docs/mcp-tool-usage-audit-20260903-data/references.json` | `362:analysis_bundle_create`, `367:analysis_bundle_get`, `499:compare_paper_accounts`, `504:compare_strategies`, `1201:get_paper_performance`, `1206:get_paper_trade_log`, `2183:paper_cohort_kill_switch`, `2190:paper_execution_cancel_order`, `2195:paper_execution_get_capabilities`, `2200:paper_execution_get_order`, `2205:paper_execution_preview_order`, `2210:paper_execution_reconcile`, `2215:paper_execution_submit_order`, `2247:paper_validation_advance`, `2252:paper_validation_append_hypothesis`, `2257:paper_validation_append_review`, `2262:paper_validation_authorize_order_submit`, `2267:paper_validation_confirm_promotion`, `2272:paper_validation_get_audit`, `2277:paper_validation_register`, `2282:paper_validation_reject_or_abort`, `2325:recommend_go_live` |
| `docs/mcp-tool-usage-audit-20260903-data/registry.json` | `250:analysis_bundle_create`, `267:analysis_bundle_get`, `362:compare_paper_accounts`, `371:compare_strategies`, `1174:get_paper_performance`, `1183:get_paper_trade_log`, `2448:paper_cohort_kill_switch`, `2459:paper_execution_cancel_order`, `2470:paper_execution_get_capabilities`, `2479:paper_execution_get_order`, `2488:paper_execution_preview_order`, `2497:paper_execution_reconcile`, `2508:paper_execution_submit_order`, `2550:paper_validation_advance`, `2561:paper_validation_append_hypothesis`, `2572:paper_validation_append_review`, `2583:paper_validation_authorize_order_submit`, `2594:paper_validation_confirm_promotion`, `2605:paper_validation_get_audit`, `2614:paper_validation_register`, `2625:paper_validation_reject_or_abort`, `2649:recommend_go_live` |
| `docs/mcp-tool-usage-audit-20260903-data/usage-90d.json` | `2147:analysis_bundle_create`, `2236:analysis_bundle_get`, `2941:compare_paper_accounts`, `3030:compare_strategies`, `9005:get_paper_performance`, `9094:get_paper_trade_log`, `17969:paper_cohort_kill_switch`, `18058:paper_execution_cancel_order`, `18147:paper_execution_get_capabilities`, `18236:paper_execution_get_order`, `18325:paper_execution_preview_order`, `18414:paper_execution_reconcile`, `18503:paper_execution_submit_order`, `18859:paper_validation_advance`, `18948:paper_validation_append_hypothesis`, `19037:paper_validation_append_review`, `19126:paper_validation_authorize_order_submit`, `19215:paper_validation_confirm_promotion`, `19304:paper_validation_get_audit`, `19393:paper_validation_register`, `19482:paper_validation_reject_or_abort`, `19660:recommend_go_live` |
| `docs/mcp-tool-usage-audit-20260903.md` | `35:analysis_bundle_get`, `36:analysis_bundle_create`, `36:analysis_bundle_get`, `37:analysis_bundle_create`, `37:analysis_bundle_get`, `37:compare_paper_accounts`, `37:compare_strategies`, `37:get_paper_performance`, `37:get_paper_trade_log`, `37:recommend_go_live`, `38:analysis_bundle_create`, `38:analysis_bundle_get`, `39:analysis_bundle_create`, `39:analysis_bundle_get`, `40:analysis_bundle_create`, `40:analysis_bundle_get`, `41:analysis_bundle_create`, `41:analysis_bundle_get`, `42:paper_cohort_kill_switch`, `42:paper_execution_cancel_order`, `42:paper_execution_get_capabilities`, `42:paper_execution_get_order`, `42:paper_execution_preview_order`, `42:paper_execution_reconcile`, `42:paper_execution_submit_order`, `42:paper_validation_advance`, `42:paper_validation_append_hypothesis`, `42:paper_validation_append_review`, `42:paper_validation_authorize_order_submit`, `42:paper_validation_confirm_promotion`, `42:paper_validation_get_audit`, `42:paper_validation_register`, `42:paper_validation_reject_or_abort`, `45:analysis_bundle_create`, `45:analysis_bundle_get`, `73:analysis_bundle_create`, `74:analysis_bundle_get`, `81:compare_paper_accounts`, `82:compare_strategies`, `135:get_paper_performance`, `136:get_paper_trade_log`, `224:paper_cohort_kill_switch`, `225:paper_execution_cancel_order`, `226:paper_execution_get_capabilities`, `227:paper_execution_get_order`, `228:paper_execution_preview_order`, `229:paper_execution_reconcile`, `230:paper_execution_submit_order`, `234:paper_validation_advance`, `235:paper_validation_append_hypothesis`, `236:paper_validation_append_review`, `237:paper_validation_authorize_order_submit`, `238:paper_validation_confirm_promotion`, `239:paper_validation_get_audit`, `240:paper_validation_register`, `241:paper_validation_reject_or_abort`, `243:recommend_go_live`, `305:analysis_bundle_get`, `306:analysis_bundle_create`, `306:analysis_bundle_get`, `307:analysis_bundle_create`, `307:analysis_bundle_get`, `307:compare_paper_accounts`, `307:compare_strategies`, `307:get_paper_performance`, `307:get_paper_trade_log`, `307:recommend_go_live`, `308:analysis_bundle_create`, `308:analysis_bundle_get`, `309:analysis_bundle_create`, `309:analysis_bundle_get`, `310:analysis_bundle_create`, `310:analysis_bundle_get`, `311:analysis_bundle_create`, `311:analysis_bundle_get`, `312:paper_cohort_kill_switch`, `312:paper_execution_cancel_order`, `312:paper_execution_get_capabilities`, `312:paper_execution_get_order`, `312:paper_execution_preview_order`, `312:paper_execution_reconcile`, `312:paper_execution_submit_order`, `312:paper_validation_advance`, `312:paper_validation_append_hypothesis`, `312:paper_validation_append_review`, `312:paper_validation_authorize_order_submit`, `312:paper_validation_confirm_promotion`, `312:paper_validation_get_audit`, `312:paper_validation_register`, `312:paper_validation_reject_or_abort`, `315:analysis_bundle_create`, `315:analysis_bundle_get` |
| `docs/plans/ROB-316-nautilustrader-adoption-spike-plan.md` | `374:compare_strategies`, `410:compare_strategies`, `444:compare_strategies` |
| `docs/plans/ROB-320-validated-signal-research-pipeline.md` | `76:compare_strategies`, `679:compare_strategies`, `1248:compare_strategies` |
| `docs/plans/ROB-351-cost-blind-funnel-campaign.md` | `18:compare_strategies` |
| `docs/runbooks/paper-cohort-kill-switch.md` | `19:paper_cohort_kill_switch` |
| `docs/superpowers/plans/2026-04-13-paper-strategy-journal-integration.md` | `22:compare_strategies`, `22:recommend_go_live`, `1672:compare_strategies`, `1678:compare_strategies`, `1684:compare_strategies`, `1778:compare_strategies`, `1818:compare_strategies`, `1871:compare_strategies`, `1889:compare_strategies`, `1897:compare_strategies`, `2038:compare_strategies`, `2039:compare_strategies`, `2051:compare_strategies`, `2056:recommend_go_live`, `2062:recommend_go_live`, `2068:recommend_go_live`, `2142:recommend_go_live`, `2166:recommend_go_live`, `2190:recommend_go_live`, `2210:recommend_go_live`, `2225:recommend_go_live`, `2237:recommend_go_live`, `2245:recommend_go_live`, `2365:recommend_go_live`, `2366:recommend_go_live`, `2378:recommend_go_live`, `2383:compare_strategies`, `2383:recommend_go_live`, `2394:compare_strategies`, `2394:recommend_go_live`, `2401:compare_strategies`, `2402:recommend_go_live`, `2409:compare_strategies`, `2410:recommend_go_live`, `2416:compare_strategies`, `2424:compare_strategies`, `2427:recommend_go_live`, `2434:recommend_go_live`, `2469:compare_strategies`, `2470:recommend_go_live`, `2494:compare_strategies`, `2494:recommend_go_live` |
| `docs/superpowers/plans/2026-04-13-paper-strategy-remaining-tasks.md` | `5:compare_strategies`, `5:recommend_go_live`, `7:compare_strategies`, `7:recommend_go_live`, `17:compare_strategies`, `17:recommend_go_live`, `27:compare_strategies`, `27:recommend_go_live`, `64:compare_strategies`, `74:compare_strategies`, `74:recommend_go_live`, `149:compare_strategies`, `189:compare_strategies`, `223:compare_strategies`, `241:compare_strategies`, `272:compare_strategies`, `299:compare_strategies`, `313:compare_strategies`, `326:compare_strategies`, `371:compare_strategies`, `507:compare_strategies`, `508:compare_strategies`, `520:compare_strategies`, `525:recommend_go_live`, `530:recommend_go_live`, `553:recommend_go_live`, `585:recommend_go_live`, `622:recommend_go_live`, `659:recommend_go_live`, `694:recommend_go_live`, `725:recommend_go_live`, `746:recommend_go_live`, `779:recommend_go_live`, `789:recommend_go_live`, `793:recommend_go_live`, `798:recommend_go_live`, `803:recommend_go_live`, `927:recommend_go_live`, `928:recommend_go_live`, `940:recommend_go_live`, `945:compare_strategies`, `945:recommend_go_live`, `965:compare_strategies`, `966:recommend_go_live`, `989:compare_strategies`, `989:recommend_go_live`, `996:compare_strategies`, `997:recommend_go_live`, `1004:compare_strategies`, `1005:recommend_go_live`, `1011:compare_strategies`, `1019:compare_strategies`, `1022:recommend_go_live`, `1029:recommend_go_live`, `1060:compare_strategies`, `1060:recommend_go_live` |
| `docs/superpowers/plans/2026-07-04-upbit-shadow-sim.md` | `454:get_paper_performance` |
| `docs/superpowers/plans/2026-07-12-rob-838-analysis-snapshot-bundle.md` | `13:analysis_bundle_get`, `22:analysis_bundle_create`, `22:analysis_bundle_get`, `668:analysis_bundle_create`, `668:analysis_bundle_get`, `670:analysis_bundle_get`, `721:analysis_bundle_get`, `725:analysis_bundle_create`, `872:analysis_bundle_get` |
| `docs/superpowers/specs/2026-04-13-paper-strategy-journal-integration-design.md` | `128:compare_strategies`, `132:recommend_go_live`, `232:compare_strategies`, `237:compare_strategies`, `304:recommend_go_live`, `309:recommend_go_live`, `357:compare_strategies`, `359:compare_strategies`, `376:compare_strategies`, `377:recommend_go_live`, `394:compare_strategies`, `394:recommend_go_live`, `419:compare_strategies`, `427:recommend_go_live` |
| `docs/superpowers/specs/2026-07-04-upbit-shadow-sim-design.md` | `14:compare_paper_accounts`, `14:get_paper_performance`, `14:get_paper_trade_log` |
| `docs/superpowers/specs/2026-07-12-rob-838-analysis-snapshot-bundle-design.md` | `83:analysis_bundle_create`, `84:analysis_bundle_get`, `86:analysis_bundle_get`, `127:analysis_bundle_create`, `127:analysis_bundle_get` |

## Verification

Run `DEV_ENV_FILE=/dev/null make lint`, `DEV_ENV_FILE=/dev/null make typecheck`, `uv run pytest tests/mcp_server -q`, the ROB-501 guard, full non-live collection and CI exact-cover. Mutation evidence must be an assertion failure, not an import/collection error. The delivery report contains original command output, four restored mutants, exact commit/remote SHA and all six required GitHub checks. No deployment is part of this draft PR.
