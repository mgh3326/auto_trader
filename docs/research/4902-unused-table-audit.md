# hk 4902 — 미사용 테이블 감사

운영자(2026-11-16): "이번 기회에 안 쓰는 테이블은 좀 정리하고 가자"(NCP→pi 이전 hk 4896 과 함께). 설계 세션이 운영 DB 통계로 뽑은 후보 94개(hk:doc `data/2026-11-16/at-unused-table-candidates`)를 코드와 대조해 분류했다. **이 문서는 분류와 근거이고, 이 PR 이 실제로 지우는 것은 [DROP-now](#4-drop-now-4) 4개뿐이다.**

## 1. 요약

| 분류 | 개수 | 이 PR 의 처리 |
|---|---:|---|
| DROP-now | 4 | 마이그레이션 `20261116_hk4902_drop_unused` 가 지운다(적용은 운영자 승인 뒤) |
| KEEP-live | 68 | 유지 |
| KEEP-hypertable | 8 | 유지 |
| DEAD-code | 8 | 유지. 코드·테이블 삭제는 후속 |
| REVIEW | 6 | 유지. 운영자 결정용 |
| 합계 | 94 | |

## 2. 분류 기준과 방법

- **KEEP-live**: `app/`·`scripts/` 의 런타임 경로(서비스 writer·reader·MCP 도구·라우터·TaskIQ·CLI)가 그 테이블을 쓰거나 읽는다. 새 기능이라 비어 있는 테이블(kiwoom_mock_*, strategy_sleeve*, decision_order_link*, execution_intents 등)도 여기에 든다.
- **KEEP-hypertable**: TimescaleDB hypertable(쓰기가 청크로 가서 부모 통계가 0 이다). 후보 목록의 `hypertable` 열을 그대로 쓴다.
- **DROP-now**: 런타임 코드 참조 0(모델 파일·alembic 이력·그 모델만 검사하는 테스트 제외), 행 0, 다른 테이블이 FK 로 참조하지 않음(ORM 메타데이터와 `alembic/`·`app/`·`scripts/` 의 `REFERENCES`/`ForeignKey` grep 0건, 숨은 의존은 마이그레이션이 `CASCADE` 없이 DROP 해 실패로 드러낸다), 원장(하드룰 5)·트리거 보호 테이블이 아님, `docs/agent-contracts/*`·`AGENTS.md` 가 이름을 들지 않음. 전부 만족해야 한다.
- **DEAD-code**: 코드(서비스·모델)는 있으나 그 코드에 닿는 런타임 진입점이 없다(퇴역 기능). 이번 PR 에서는 지우지 않고 목록만 둔다.
- **REVIEW**: 행이 있는데 런타임 참조가 없다, 또는 판단이 애매하다. 운영자 결정용이며 지우지 않는다.

**도달성 판정 방법.** `app/`·`scripts/`·루트 `*.py` 전체의 정적 import 그래프(함수 안 지연 import, `importlib.import_module("…")` 상수 인자 포함)를 만들고, 진입점에서 닿는 모듈 집합을 구했다. 진입점은 `app.main`(라우터), `app.mcp_server.*`(MCP 레지스트리), `app/tasks/*`·`app/flows/*`(TaskIQ·Prefect), `app.execution_intent_gateway_server`(별도 loopback 프로세스), `scripts/*`·루트 모니터 스크립트(CLI)다. 테이블마다 테이블 이름 단어와 ORM 클래스 이름으로 `app/`·`scripts/`·`research/`·`deploy/`·`config/` 를 grep 해 참조 파일을 모으고, 모델 정의·`app/models/__init__.py` 는 뺀 뒤 각 파일이 위 도달 집합에 드는지 확인했다. 이름이 흔한 클래스(`Exchange`, `MarketFilter` 등)는 손으로 용도를 확인했다(예: `app/services/invest_view_model/buy_plan/service.py:72` 의 `MarketFilter = str` 은 같은 이름의 타입 별칭이지 모델이 아니다).

**한계.** 정적 import 도달성은 "호출된다"는 증명이 아니라 "호출 경로가 코드에 존재한다"는 뜻이다. 기능 게이트(예: `PAPER_COHORT_ENABLED` 기본 false)가 꺼져 있어도 진입점이 있으면 KEEP-live 로 본다. KEEP-live 라도 운영에서 오래 안 쓰이는 것(아래 5.1절)은 별도 판단 대상이다.

## 3. 후보 통계의 행수를 믿지 않는 이유

후보 목록의 `추정 행수`(reltuples)가 `-1` 인 테이블은 "비어 있음"이 아니라 **한 번도 분석(ANALYZE)되지 않은** 테이블이다. 같은 기간 ROB-1488 인벤토리(`docs/architecture/rob-1488-feature-inventory.md` §4, 2026-11-02/03 기준 정확한 `count(*)`)는 이 중 일부에 행이 있음을 보였다. 이 문서는 두 값을 나란히 적고, **DROP-now 는 정확한 행수가 0 인 것만** 담았다.

| 테이블 | reltuples | 정확 행수 | 분류 |
|---|---:|---:|---|
| `public.asset_profiles` | -1 | 12 | REVIEW |
| `public.market_reports` | -1 | 1 | KEEP-live |
| `public.portfolio_decision_runs` | -1 | 3 | REVIEW |
| `public.profile_change_log` | -1 | 14 | REVIEW |
| `public.prompt_results` | -1 | 36 | REVIEW |
| `public.scalping_daily_reviews` | -1 | 2 | KEEP-live |
| `public.sell_conditions` | -1 | 1 | DEAD-code |
| `public.symbol_trade_settings` | -1 | 17 | KEEP-live |
| `public.trading_decision_strategy_events` | -1 | 5 | KEEP-live |

정확 행수 열이 `n/a` 인 테이블은 인벤토리 이후 생긴 새 기능 테이블이며 모두 KEEP-live 다.

## 4. DROP-now (4)

이 4개가 마이그레이션 `alembic/versions/20261116_hk4902_drop_unused_tables.py` 의 대상 전부다. 아래 블록은 정적 테스트(`tests/test_hk4902_drop_unused_tables_migration.py`)가 파싱해 마이그레이션의 대상 집합과 같은지 검사한다.

<!-- drop-now:begin -->
public.market_filters
public.tier_rule_params
public.user_channels
public.user_research_notes
<!-- drop-now:end -->

| 테이블 | reltuples | 정확 행수 | 근거 |
|---|---:|---:|---|
| `public.market_filters` | -1 | 0 | `app/`·`scripts/`·`deploy/`·`config/` 참조 0. 모델 `MarketFilter` 의 사용처 0(`buy_plan/service.py:72` 는 같은 이름의 `str` 별칭). trade_profile 계열은 ROB-488 부터 dead(`config/trading_policy.yaml:314`), 관련 MCP 도구는 미등록(`tests/test_mcp_tool_registration.py:85`). 이 테이블을 쓰던 테스트는 `tests/models/test_trade_profile.py` 뿐이다. |
| `public.tier_rule_params` | -1 | 0 | `app/`·`scripts/`·`deploy/`·`config/` 참조 0. 모델 `TierRuleParam`·`TierParamType` 사용처 0. 같은 trade_profile 계열(ROB-488 dead). 쓰던 테스트는 `tests/models/test_trade_profile.py` 뿐이다. |
| `public.user_channels` | -1 | 0 | `app/`·`scripts/` 참조 0. 모델 `UserChannel`·`NotifyChannel` 사용처 0. 초기 스키마(`b3e58be9e79b_init.py`)에서 만들어진 뒤 writer 가 없다. PostgreSQL enum 타입 `notify_channel` 은 건드리지 않는다. |
| `public.user_research_notes` | -1 | 0 | `app/`·`scripts/` 참조 0. 모델 `UserResearchNote` 사용처 0. ROB-112 가 "schema only, no service writes yet" 로 만들었고(`docs/runbooks/research-pipeline-rollout.md:139`) 이후 writer 가 생기지 않았다. FK 는 이 테이블이 `research_sessions`·`users` 를 가리키는 방향뿐이다. |

함께 바뀌는 것: `app/models/trade_profile.py`(`TierRuleParam`, `MarketFilter`, `TierParamType`, `FilterName` 제거), `app/models/trading.py`(`UserChannel`, `NotifyChannel` 제거), `app/models/research_pipeline.py`(`UserResearchNote` 와 `ResearchSession.notes` 관계 제거), `app/models/__init__.py`(export), `tests/models/test_trade_profile.py`(두 테이블을 만지던 부분 제거), `tests/models/test_research_pipeline_models.py`(`test_user_research_note_columns` 제거), `tests/test_hk4734_kiwoom_broker_migration.py`("head 가 곧 이 리비전" 핀을 "단일 체인 안에 있다" 로 완화 — 새 head 가 생기면 깨지는 닫힌 목록이었다).

**적용 안전장치.** 마이그레이션은 테이블마다 `ACCESS EXCLUSIVE` 잠금을 잡고 행이 하나라도 있으면 `hk4902_<table>_rows_retained` 로 거부한다. `DROP TABLE` 은 `CASCADE` 없이 실행해 모르는 의존 객체가 있으면 실패한다. downgrade 는 네 테이블을 마지막 스키마(컬럼·제약·인덱스) 그대로 빈 채로 다시 만들고, `at_app` 역할이 있으면 DML·시퀀스 권한을 조건부로 부여한다(하드룰 18). 운영 적용은 pi 이전 뒤 최종 덤프 다음 운영자 승인으로만 한다.

## 5. KEEP-live (68)

근거 열은 그 테이블을 쓰거나 읽는 코드 위치이고, 비고의 진입점은 도달 그래프에서 그 모듈에 닿는 진입점(`app.main`=라우터, `app.mcp_server`=MCP 레지스트리, `app.tasks.*`=TaskIQ, `scripts.*`=CLI)이다. 진입점 이름이 `?` 이면 도달 그래프 밖의 SQL 문자열 참조다.

| 테이블 | reltuples | 정확 행수 | 근거 (path:line) | 비고 |
|---|---:|---:|---|---|
| `public.instruments` | -1 | 0 | app/services/invest_view_model/account_panel_service.py:60, app/services/invest_view_model/relation_resolver.py:83, app/services/invest_view_model/relation_resolver.py:84 | 진입점 `app.main` |
| `public.market_reports` | -1 | 1 | app/services/market_report_service.py:34, app/services/market_report_service.py:135, app/services/market_report_service.py:156 | 진입점 `app.main` |
| `public.news_analysis_results` | -1 | 0 | app/services/llm_news_service.py:222 | 진입점 `app.mcp_server` |
| `public.news_issue_lab_results` | 24 | 24 | scripts/news_issue_lab.py:2439 | 진입점 `scripts.news_issue_lab` |
| `public.news_issue_lab_runs` | 2 | 2 | scripts/news_issue_lab.py:2379, scripts/news_issue_lab.py:2407 | 진입점 `scripts.news_issue_lab` |
| `public.order_execution_request` | -1 | 0 | app/services/order_preview_session_service.py:20, app/services/order_preview_session_service.py:126, app/services/order_preview_session_service.py:136 | 진입점 `app.main` |
| `public.order_preview_leg` | -1 | 0 | app/services/order_preview_session_service.py:21, app/services/order_preview_session_service.py:70, scripts/rob278_kr_dryrun.py:54 | 진입점 `app.main` |
| `public.order_preview_session` | -1 | 0 | app/services/order_preview_session_service.py:55, app/services/order_preview_session_service.py:221 | 진입점 `app.main` |
| `public.pending_orders` | -1 | 0 | app/services/invest_view_model/crypto_dashboard_service.py:164, app/services/invest_view_model/stock_detail_service.py:228, app/services/pending_order_sync_service.py:65 | 진입점 `app.main` |
| `public.research_sessions` | 735 | 735 | app/analysis/pipeline.py:53, app/analysis/pipeline.py:58, app/mcp_server/tooling/research_pipeline_read.py:23 | 진입점 `app.main` |
| `public.research_summaries` | 673 | 673 | app/analysis/pipeline.py:123, app/mcp_server/tooling/research_pipeline_read.py:187, app/services/research_pipeline_service.py:274 | 진입점 `app.main` |
| `public.scalp_trade_analytics` | 206 | 206 | app/services/brokers/binance/demo_scalping_exec/analytics.py:59, app/services/brokers/binance/demo_scalping_exec/analytics.py:98, app/services/scalping_reviews/service.py:132 | 진입점 `app.tasks.paper_cohort_tasks` |
| `public.scalping_daily_reviews` | -1 | 2 | app/services/scalping_reviews/service.py:91, app/services/scalping_reviews/service.py:115, app/services/scalping_reviews/service.py:227 | 진입점 `app.main` |
| `public.scalping_review_actions` | -1 | 0 | app/services/scalping_reviews/service.py:251, app/services/scalping_reviews/service.py:306, app/services/scalping_reviews/service.py:335 | 진입점 `app.main` |
| `public.stage_analysis` | 2270 | 2,270 | app/analysis/pipeline.py:97, app/mcp_server/tooling/research_pipeline_read.py:149, app/services/research_pipeline_service.py:194 | 진입점 `app.main` |
| `public.stock_aliases` | -1 | 0 | app/services/stock_alias_service.py:39 | 진입점 `app.mcp_server` · 계약 문서가 이름을 든다 |
| `public.stock_analysis_results` | 2413 | 2,413 | app/mcp_server/tooling/market_brief_tools.py:47, app/mcp_server/tooling/market_brief_tools.py:48, app/mcp_server/tooling/market_brief_tools.py:139 | 진입점 `app.mcp_server` · 계약 문서가 이름을 든다 |
| `public.stock_info` | 176 | 176 | app/mcp_server/tooling/market_brief_tools.py:47, app/mcp_server/tooling/market_brief_tools.py:48, app/mcp_server/tooling/market_brief_tools.py:139 | 진입점 `app.mcp_server` · 계약 문서가 이름을 든다 |
| `public.summary_stage_links` | 2088 | 2,088 | app/analysis/pipeline.py:146, app/services/research_retrospective_service.py:140 | 진입점 `app.main` |
| `public.symbol_trade_settings` | -1 | 17 | app/services/symbol_trade_settings_service.py:94, app/services/symbol_trade_settings_service.py:116, app/services/symbol_trade_settings_service.py:125 | 진입점 `app.main` · 계약 문서가 이름을 든다 |
| `public.trading_decision_actions` | -1 | 0 | app/services/trading_decision_service.py:167 | 진입점 `app.tasks.research_run_refresh_tasks` |
| `public.trading_decision_counterfactuals` | -1 | 0 | app/services/trading_decision_service.py:194 | 진입점 `app.tasks.research_run_refresh_tasks` |
| `public.trading_decision_outcomes` | -1 | 0 | app/services/research_retrospective_service.py:367, app/services/trading_decision_service.py:231 | 진입점 `app.main` |
| `public.trading_decision_strategy_events` | -1 | 5 | app/services/strategy_event_service.py:74, app/services/strategy_event_service.py:99, app/services/strategy_event_service.py:133 | 진입점 `app.main` |
| `public.user_trade_defaults` | -1 | 0 | app/services/symbol_trade_settings_service.py:27, app/services/symbol_trade_settings_service.py:35, app/services/symbol_trade_settings_service.py:66 | 진입점 `app.main` |
| `public.user_watch_items` | -1 | 0 | app/services/invest_view_model/account_panel_service.py:60, app/services/invest_view_model/relation_resolver.py:84, app/services/market_events/user_context.py:34 | 진입점 `app.main` |
| `research.backtest_pairs` | -1 | 0 | app/services/research_ingestion_service.py:115 | 진입점 `scripts.ingest_freqtrade_report` |
| `research.backtest_runs` | -1 | 0 | app/services/paper_cohort/cohort_service.py:66, app/services/research_campaign_bridge.py:256, app/services/research_ingestion_service.py:80 | 진입점 `app.tasks.paper_cohort_tasks` |
| `research.canonical_market_snapshots` | -1 | 0 | app/services/paper_cohort/provenance.py:154, app/services/paper_cohort/runner.py:416 | 진입점 `app.tasks.paper_cohort_tasks` |
| `research.kr_candle_promotion_conflicts` | -1 | 0 | app/services/research_candles/promotion.py:219 | 진입점 `scripts.promote_kr_candles_to_research` |
| `research.kr_candle_promotion_watermark` | -1 | 0 | app/services/research_candles/promotion.py:99, app/services/research_candles/promotion.py:283 | 진입점 `scripts.promote_kr_candles_to_research` |
| `research.paper_cohort_decisions` | -1 | 0 | app/jobs/paper_cohort.py:104, app/services/paper_cohort/provenance.py:149, app/services/paper_cohort/runner.py:429 | 진입점 `app.tasks.paper_cohort_tasks` |
| `research.paper_cohort_run_claims` | -1 | 0 | app/jobs/paper_cohort.py:104, app/services/paper_cohort/runner.py:156, app/services/paper_cohort/runner.py:171 | 진입점 `app.tasks.paper_cohort_tasks` |
| `research.paper_cohort_target_reservations` | -1 | 0 | app/services/paper_cohort/runner.py:690, app/services/paper_cohort/runner.py:699 | 진입점 `app.tasks.paper_cohort_tasks` |
| `research.paper_cohort_terminal_fences` | -1 | 0 | app/jobs/paper_cohort.py:167, app/services/paper_cohort/provenance.py:202, app/services/paper_cohort/runner.py:280 | 진입점 `app.tasks.paper_cohort_tasks` |
| `research.paper_cohort_venue_intents` | -1 | 0 | app/services/paper_cohort/provenance.py:142, app/services/paper_cohort/runner.py:444, app/services/paper_cohort/runner.py:602 | 진입점 `app.tasks.paper_cohort_tasks` |
| `research.paper_run_order_links` | -1 | 0 | app/services/paper_cohort/runner.py:894 | 진입점 `app.tasks.paper_cohort_tasks` |
| `research.paper_validation_cohort_assignments` | -1 | 0 | app/jobs/paper_cohort.py:51, app/services/paper_cohort/provenance.py:166, app/services/paper_cohort/provenance.py:175 | 진입점 `app.tasks.paper_cohort_tasks` |
| `research.paper_validation_cohorts` | -1 | 0 | app/jobs/paper_cohort.py:162, app/jobs/paper_cohort.py:239, app/services/paper_cohort/cohort_service.py:45 | 진입점 `app.tasks.paper_cohort_tasks` |
| `research.paper_validation_state_transitions` | -1 | 0 | app/jobs/paper_cohort.py:59, app/services/paper_cohort/cohort_service.py:54, app/services/paper_cohort/runner.py:309 | 진입점 `app.tasks.paper_cohort_tasks` |
| `research.promotion_candidates` | -1 | 0 | app/services/strategy_experiment_registry.py:472 | 진입점 `scripts.run_rob974_r2_campaign` |
| `research.strategy_experiments` | -1 | 0 | app/services/paper_cohort/cohort_service.py:62, app/services/paper_cohort/provenance.py:184, app/services/research_campaign_bridge.py:214 | 진입점 `app.tasks.paper_cohort_tasks` |
| `research.strategy_learning_events` | -1 | 0 | app/services/strategy_learning_event_service.py:83, app/services/strategy_learning_event_service.py:93, app/services/strategy_learning_event_service.py:147 | 진입점 `scripts.record_strategy_learning_event` |
| `research.sync_jobs` | -1 | 0 | app/services/research_ingestion_service.py:40, app/services/research_ingestion_service.py:51 | 진입점 `scripts.ingest_freqtrade_report` |
| `review.account_external_flows` | -1 | n/a | app/services/account_value/flows.py:51, app/services/account_value/flows.py:60, app/services/account_value/load_report.py:149 | 진입점 `scripts.account_value_snapshot` |
| `review.account_flow_confirmation_revocations` | -1 | n/a | app/services/account_value/completeness.py:34 | 진입점 `app.main` · 계약 문서가 이름을 든다 |
| `review.decision_order_link_outcomes` | -1 | n/a | app/services/decision_evidence.py:194, app/services/retro_v1/links.py:45 | 진입점 `app.mcp_server` · 계약 문서가 이름을 든다 |
| `review.decision_order_links` | -1 | n/a | app/services/decision_evidence.py:179, app/services/retro_v1/links.py:35 | 진입점 `app.mcp_server` · 계약 문서가 이름을 든다 |
| `review.execution_child_requests` | -1 | 0 | app/services/execution_intent_gateway/store.py:192, app/services/execution_intent_gateway/store.py:222, app/services/execution_intent_gateway/store.py:228 | 진입점 `app.execution_intent_gateway_server` · 계약 문서가 이름을 든다 |
| `review.execution_intents` | -1 | 0 | app/services/execution_intent_gateway/store.py:155, app/services/execution_intent_gateway/store.py:180, app/services/execution_intent_gateway/store.py:204 | 진입점 `app.execution_intent_gateway_server` · 계약 문서가 이름을 든다 |
| `review.fill_watch_context_outcomes` | -1 | 0 | app/services/fill_watch_context/repository.py:28, app/services/fill_watch_context/repository.py:39, app/services/fill_watch_context/service.py:154 | 진입점 `app.mcp_server` |
| `review.kis_mock_dispatch_evidence` | -1 | n/a | app/services/kis_mock_runner/evidence_store.py:226, app/services/kis_mock_runner/evidence_store.py:314, app/services/kis_mock_runner/evidence_store.py:330 | 진입점 `app.mcp_server` |
| `review.kis_mock_dispatch_lineage` | -1 | n/a | app/services/kis_mock_runner/evidence_store.py:278, app/services/kis_mock_runner/evidence_store.py:663, app/services/kis_mock_runner/evidence_store.py:706 | 진입점 `app.mcp_server` |
| `review.kis_mock_inference_expiry_events` | -1 | 0 | app/services/kis_mock_inference_expiry_service.py:292 | 진입점 `app.mcp_server` · 계약 문서가 이름을 든다 |
| `review.kis_mock_position_baseline` | -1 | n/a | app/services/kis_mock_runner/evidence_store.py:68 | 진입점 `app.mcp_server` · 계약 문서가 이름을 든다 |
| `review.kis_us_inference_expiry_events` | -1 | n/a | app/services/kis_us_inference_expiry_service.py:262, app/services/kis_us_inference_expiry_service.py:276 | 진입점 `scripts.expire_kis_us_rows_by_inference` |
| `review.kiwoom_mock_order_ledger` | -1 | n/a | app/services/kiwoom_mock_order_ledger_service.py:585, app/services/kiwoom_mock_order_ledger_service.py:591, app/services/kiwoom_mock_order_ledger_service.py:618 | 진입점 `app.tasks.telegram_callback_inbox_tasks` · 계약 문서가 이름을 든다 |
| `review.kiwoom_mock_signal_ledger` | -1 | n/a | app/services/kiwoom_mock_attribution.py:200, app/services/kiwoom_mock_attribution.py:274, app/services/kiwoom_mock_attribution.py:323 | 진입점 `app.tasks.telegram_callback_inbox_tasks` · 계약 문서가 이름을 든다 |
| `review.ladder_touch_events` | -1 | 0 | app/services/quotes_consumer/repository.py:125 | 진입점 `app.tasks.quotes_consumer_tasks` |
| `review.nhplug_mock_account_binding` | -1 | 0 | app/services/nhplug_mock/account_identity.py:136, app/services/nhplug_mock/account_identity.py:164 | 진입점 `app.mcp_server` |
| `review.nhplug_mock_account_ref` | -1 | 0 | app/services/nhplug_mock/account_identity.py:153 | 진입점 `app.mcp_server` |
| `review.nhplug_mock_operator_authorization` | -1 | 0 | alembic/versions/20260926_task711_nhplug_dispatch.py:136,161 (live `nhplug_mock_order_ledger` 가 FK 로 참조), scripts/db_roles/core.py:33 (권한 표) | 직접 읽고 쓰는 서비스 SQL 은 없다. 그러나 `review.nhplug_mock_order_ledger` 의 `second_order_authorization_id`·`resolution_authorization_id` 가 이 테이블의 1회용 행을 FK 로 소비한다(트리거 보호). 계약 문서(NHPLUG 절)가 "일회용 authorization row" 를 서술한다. KEEP-live(FK 부모) — 원장과 함께 움직인다. |
| `review.nhplug_mock_order_ledger` | -1 | 0 | app/services/nhplug_mock/ledger.py:140, app/services/nhplug_mock/ledger.py:160, app/services/nhplug_mock/ledger.py:183 | 진입점 `app.mcp_server` · 계약 문서가 이름을 든다 |
| `review.nhplug_no_order_proof_code` | -1 | 0 | app/services/nhplug_mock/ledger.py:235 | 진입점 `app.mcp_server` |
| `review.nhplug_success_proof_code` | -1 | 0 | app/services/nhplug_mock/ledger.py:223 | 진입점 `app.mcp_server` |
| `review.quotes_trigger_firings` | -1 | 0 | app/services/quotes_consumer/repository.py:114, app/services/quotes_consumer/repository.py:339 | 진입점 `app.tasks.quotes_consumer_tasks` |
| `review.strategy_sleeve_versions` | -1 | 0 | app/services/retro_v1/retro_decisions.py:421, app/services/strategy_sleeves/store.py:167, app/services/strategy_sleeves/store.py:178 | 진입점 `app.main` · 계약 문서가 이름을 든다 |
| `review.strategy_sleeves` | -1 | 0 | app/services/strategy_sleeves/store.py:148, app/services/strategy_sleeves/store.py:155, app/services/strategy_sleeves/store.py:161 | 진입점 `app.mcp_server` · 계약 문서가 이름을 든다 |

### 5.1 KEEP-live 이지만 운영에서 쓰임이 의심되는 테이블(참고)

코드 경로가 있어 KEEP-live 로 뒀다. rob-1488 §4.1 은 아래를 "legacy analysis/research pipeline — 2026-06-01 이후 쓰기 0, 관련 MCP 도구 호출 0" 으로 묶어 퇴역 후보로 제안했다. 행이 있는 것들이라 지우려면 덤프와 운영자 결정이 먼저다. 이 PR 은 건드리지 않는다.

`public.stock_info`(176), `public.stock_analysis_results`(2,413), `public.stage_analysis`(2,270), `public.summary_stage_links`(2,088), `public.research_sessions`(735), `public.research_summaries`(673), `public.scalp_trade_analytics`(206), `public.scalping_daily_reviews`(2), `public.symbol_trade_settings`(17), `public.market_reports`(1), `public.trading_decision_strategy_events`(5), `public.news_issue_lab_results`(24), `public.news_issue_lab_runs`(2).

## 6. KEEP-hypertable (8)

| 테이블 | reltuples | 정확 행수(rob-1488) | 비고 |
|---|---:|---:|---|
| `public.crypto_candles_1d` | -1 | 60,415 | TimescaleDB hypertable(부모 통계가 0 인 것은 청크로 쓰기 때문) |
| `public.crypto_candles_1m` | -1 | 0 | TimescaleDB hypertable(부모 통계가 0 인 것은 청크로 쓰기 때문) |
| `public.kr_candles_1d` | -1 | 585,026 | TimescaleDB hypertable(부모 통계가 0 인 것은 청크로 쓰기 때문) |
| `public.kr_candles_1m` | -1 | 1,161,123 | TimescaleDB hypertable(부모 통계가 0 인 것은 청크로 쓰기 때문) · 계약 문서가 이름을 든다 |
| `public.us_candles_1d` | -1 | 1,043,609 | TimescaleDB hypertable(부모 통계가 0 인 것은 청크로 쓰기 때문) · 계약 문서가 이름을 든다 |
| `public.us_candles_1m` | -1 | 303,738 | TimescaleDB hypertable(부모 통계가 0 인 것은 청크로 쓰기 때문) |
| `research.kr_candles_1m` | -1 | 0 | TimescaleDB hypertable(부모 통계가 0 인 것은 청크로 쓰기 때문) · 계약 문서가 이름을 든다 |
| `research.kr_candles_1m_toss` | -1 | 1,171,860 | TimescaleDB hypertable(부모 통계가 0 인 것은 청크로 쓰기 때문) · 계약 문서가 이름을 든다 |

## 7. DEAD-code (8)

코드는 있으나 그 코드에 닿는 런타임 진입점이 없다. 이번 PR 은 지우지 않는다. 후속: 코드(서비스·모델·테스트)를 먼저 지우고, 같은 PR 또는 다음 PR 에서 테이블을 DROP 한다.

| 테이블 | reltuples | 정확 행수 | 코드 위치 (path:line) | 비고 |
|---|---:|---:|---|---|
| `public.crypto_instrument_health` | -1 | 0 | app/services/instrument_health/service.py:66 (`CryptoInstrumentHealthService`), app/services/instrument_health/repository.py:27 (`CryptoInstrumentHealthRepository`) | `app.services.instrument_health` 패키지를 import 하는 `app/`·`scripts/`·`deploy/` 코드가 없다(모델 export `app/models/__init__.py:24` 와 모델 docstring 제외). MCP 도구·라우터·TaskIQ·스크립트 어느 진입점도 닿지 않는다. |
| `public.sell_conditions` | -1 | 1 | app/services/sell_conditions_service.py:13 (`get_sell_condition`), :20 (`get_active_sell_conditions`) | `app.services.sell_conditions_service` 를 import 하는 코드가 없다. `config/trading_policy.yaml:313` 도 "dormant/test-only (not authoritative)" 로 선언한다. 행 1건(2026-04-16)이 있어 DROP 전 덤프가 필요하다. |
| `research.evaluation_configs` | -1 | 0 | app/services/paper_evaluation/contracts.py:243 (`EvaluationConfig`), app/services/paper_evaluation/evaluation_config.py:14 (`compute_config_hash`) | `app.services.paper_evaluation` 패키지를 패키지 밖에서 import 하는 `app/`·`scripts/` 코드가 없다(테스트만 import). TaskIQ `paper_cohort.run_active` 는 이 패키지를 부르지 않는다. |
| `research.evaluation_epochs` | -1 | 0 | app/services/paper_evaluation/evidence.py:31 (`EvaluationEpoch`) | 위와 같다: `paper_evaluation` 패키지에 진입점이 없다. |
| `research.evaluation_scorecards` | -1 | 0 | app/services/paper_evaluation/service.py:197 (`EvaluationScorecard(`) | 위와 같다: `paper_evaluation.service` 의 호출자가 없다. |
| `research.evaluation_verdicts` | -1 | 0 | app/services/paper_evaluation/service.py:153,210 (`EvaluationVerdict`) | 위와 같다: `paper_evaluation.service` 의 호출자가 없다. |
| `research.paper_validation_postmortem_reviews` | -1 | 0 | app/services/paper_validation/service.py:348 (`append_postmortem_review`), :474 (`get_audit`) | `PaperValidationService` 는 `app/jobs/paper_cohort.py:184` 가 만들지만 `append_postmortem_review`·`get_audit` 를 부르는 `app/` 코드가 없다. MCP 쪽 `register_paper_cohort_control_tools`(app/mcp_server/tooling/paper_cohort_control_registration.py:22)는 `app/mcp_server/tooling/registry.py` 어디서도 호출되지 않는다. |
| `research.strategy_hypothesis_drafts` | -1 | 0 | app/services/paper_validation/service.py:277 (`append_hypothesis`) | 위와 같다: `append_hypothesis` 를 부르는 `app/` 코드가 없고 MCP 도구도 미등록이다. |

진입점이 없다는 근거(공통): `app/mcp_server/tooling/registry.py` 가 `paper_validation*`·`paper_cohort_control*` 등록 함수를 부르지 않고(grep 0), `app/tasks/*`·`scripts/*` 가 `paper_evaluation`·`instrument_health`·`sell_conditions_service` 를 import 하지 않는다. 테스트(`tests/services/paper_evaluation/`, `tests/mcp_server/tooling/test_paper_cohort_control_registration.py` 등)만 이 코드를 부른다.

## 8. REVIEW (6)

지우지 않는다. 운영자가 결정할 것: (a) 행이 있는 4개(`asset_profiles` 12, `profile_change_log` 14, `portfolio_decision_runs` 3, `prompt_results` 36)는 덤프 후 DROP 할지, (b) `buy_gate_ab_collection_epoch_v2` 는 하드룰 12 영역이라 별도 결정, (c) `exchanges` 는 `instruments` 와 같이 결정.

| 테이블 | reltuples | 정확 행수 | 위치 (path:line) | 비고 |
|---|---:|---:|---|---|
| `public.asset_profiles` | -1 | 12 | app/models/trade_profile.py:42 (모델만), tests/models/test_trade_profile.py | 런타임 참조 0(MCP `get_asset_profile` 등은 ROB-488 에서 미등록, tests/test_mcp_tool_registration.py:85). 하지만 rob-1488 정확 행수 12건(마지막 쓰기 2026-03-13)이라 비어 있지 않다. 후보 통계의 -1 은 미분석 값이다. |
| `public.exchanges` | -1 | 0 | app/models/trading.py:42 (모델만; FK 자식 `Instrument.exchange_id` app/models/trading.py:49) | `Exchange` 클래스를 직접 쓰는 런타임 코드는 0, 행 0. 하지만 `public.instruments.exchange_id` 가 이 테이블을 FK 로 참조하고 `public.instruments` 는 KEEP-live 다(account_panel_service.py:60). `instruments` 와 함께 결정한다. |
| `public.portfolio_decision_runs` | -1 | 3 | app/models/portfolio_decision_run.py:17 (모델만), tests/test_portfolio_decision_run_model.py | 런타임 참조 0. rob-1488 정확 행수 3건. |
| `public.profile_change_log` | -1 | 14 | app/models/trade_profile.py:110 (모델만), tests/models/test_trade_profile.py | 런타임 참조 0. rob-1488 정확 행수 14건(2026-03-13). |
| `public.prompt_results` | -1 | 36 | app/models/prompt.py:8 (모델만) | 런타임 참조 0. rob-1488 정확 행수 36건(2026-01-05). |
| `review.buy_gate_ab_collection_epoch_v2` | -1 | 0 | app/models/buy_gate_ab_experiment_lifecycle.py:112 (모델만), alembic/versions/20260907_rob1351_buy_gate_lifecycle.py:91 | 런타임 참조 0, 행 0. 그러나 append-only 트리거(tests/_schema_bootstrap.py:1985-1993)가 걸린 불변 singleton 이고 런북 docs/runbooks/buy-gate-ab-shadow.md:31 이 "등록됐고 아직 무장 전" 으로 서술한다. 하드룰 12(매수 게이트 A/B shadow) 영역이라 지우지 않는다. |

## 9. 계약 문서·원장·트리거 보호와 DROP-now 의 관계

- DROP-now 4개 중 `docs/agent-contracts/*`·`AGENTS.md` 가 이름을 드는 것은 없다(정적 테스트가 단어 경계로 검사한다). `docs/agent-contracts/data-model-and-symbol-contracts.md:151` 이 "trade_profile — dead since ROB-488" 을 언급하지만 테이블 이름이 아니라 기능 이름이다.
- 원장 테이블(하드룰 5 목록)과 append-only/트리거 보호 테이블은 DROP-now 에 없다. 이런 테이블 중 런타임 참조가 0 인 `review.buy_gate_ab_collection_epoch_v2` 는 REVIEW 로 뒀다.
- 계약 문서가 이름을 드는 후보(KEEP 쪽): `public.kr_candles_1m`, `public.stock_aliases`, `public.stock_analysis_results`, `public.stock_info`, `public.symbol_trade_settings`, `public.us_candles_1d`, `research.kr_candles_1m`, `research.kr_candles_1m_toss`, `review.account_flow_confirmation_revocations`, `review.decision_order_link_outcomes`, `review.decision_order_links`, `review.execution_child_requests`, `review.execution_intents`, `review.kis_mock_inference_expiry_events`, `review.kis_mock_position_baseline`, `review.kiwoom_mock_order_ledger`, `review.kiwoom_mock_signal_ledger`, `review.nhplug_mock_order_ledger`, `review.strategy_sleeve_versions`, `review.strategy_sleeves`.

## 10. 이번 PR 의 범위 밖 (noticed)

- PostgreSQL enum 타입 `public.notify_channel` 은 `user_channels` 삭제 뒤 고아가 된다. 지우지 않았다(downgrade 가 재사용). 정리는 후속.
- `.test_durations`·`.call_durations.json` 에 지운 테스트 이름이 남는다(생성 캐시). 재생성은 CI 소관.
- `docs/` 아래 과거 계획서(ROB-112 등)와 `docs/architecture/rob-1488-feature-inventory.md` 는 그대로 둔다(당시 기록).
