# #760 database-role cutover design

**Status:** Phase 1 proposal only. This document authorizes neither a production
connection change nor NHPLUG Stage 2 activation. It creates no migration, scheduler,
runtime configuration, role, grant, or database object. Phase 2 requires an explicit
operator decision after the production facts in [Open operator questions](#open-operator-questions)
are answered.

## Decision summary

Use distinct, least-privilege PostgreSQL group roles, with separately rotated login
identities for each deployment class:

| Proposed group role | Login? | Intended authority | Explicit exclusions |
| --- | --- | --- | --- |
| `at_migration_owner` | No | Own application schemas and application-created objects; execute reviewed Alembic DDL under an approved migration runner | No `SUPERUSER`, `CREATEROLE`, `BYPASSRLS`, or application deployment credentials |
| `at_app` | No | The repository application's required table DML and sequence use; shared privilege group for API, TaskIQ, MCP, supported monitors, and approved CLI jobs | No DDL, schema `CREATE`, ownership, role administration, `TRUNCATE`, `REFERENCES`, `TRIGGER`, or broad `ALL TABLES` grants |
| `nhplug_security_owner` | No | Own the #711 SECURITY DEFINER functions and their protected review objects | No login, no untrusted memberships, no `SUPERUSER`, `CREATEROLE`, or `BYPASSRLS` |
| `nhplug_operator` | No | Insert the specifically approved #711 key/proof/authorization control rows and read the operator evidence required for that desk workflow | No table ownership, no direct ledger state update, no application login, no `UPDATE`/`DELETE` on authorization controls |
| `at_reporting` | No | **Deferred.** Create only if an approved report consumer and an object-level read manifest exist | No write, execute, ownership, or schema-create privilege |

`at_api_login`, `at_worker_login`, `at_scheduler_login`, one login per MCP
deployment/profile, supported monitor logins, and `at_migration_runner` are proposed
login identities rather than shared group roles. Their passwords/certificates belong in
the approved secret-management and deployment workflow, not in this repository or this
document. Application logins must be `INHERIT` members of only `at_app`, because the
repository has no startup `SET ROLE` step. The migration runner is `NOINHERIT` and may
`SET ROLE at_migration_owner` in a reviewed, auditable migration invocation. A
desk-only login is `NOINHERIT` and may `SET ROLE nhplug_operator`. No application login
is a member of either owner role.

This separates three powers that are currently easy to conflate: ordinary application
DML, controlled schema evolution, and the #711 operator-control write path. It also
keeps a reporting role out of scope until there is an actual consumer to justify one.

## Scope and evidence boundary

### What this design did inspect

This is a static repository and tracked-template inventory. Citations use the form
`path:Lx-Ly` and name source code or a tracked deployment template, not a statement
about a running production process. No environment file, secret, production database,
broker, or NCP system was opened or contacted.

The shared application engine takes an explicit URL or `settings.DATABASE_URL`, creates
one lazy engine/session factory per process, and reads only the documented pool aliases
(`DB_POOL_CLASS`, `DB_POOL_SIZE`, `DB_MAX_OVERFLOW`, `DB_POOL_RECYCLE_S`, and
`DB_POOL_TIMEOUT_S`) [app/core/db.py:L15-L47, L50-L120]. `DATABASE_URL` is a required
setting and the settings loader selects its dotenv filename through `ENV_FILE`
[app/core/config.py:L637-L640, L1291-L1302]. Thus changing a secret/URL rotates the
connection identity only when a new process constructs a connection; it does not
retroactively change an open pool.

### What remains unknown

The repository cannot establish any of the following production facts:

- database server, database name, PostgreSQL version, extensions, current role
  attributes/memberships, ownership, default ACLs, row-security policies, or current
  grants;
- which tracked deployment template is actually in service, whether every listed unit
  exists, or whether an out-of-repository service, manual shell, BI tool, or automation
  also connects;
- which credentials are embedded in the deployed secret manager, which client pool
  sessions are still authenticated as an older role, or whether a managed PostgreSQL
  service imposes role/extension restrictions.

Every production decision below is therefore conditional on the read-only preflight
queries in [Operator-desk cutover](#operator-desk-cutover), not an assertion that the
repository's templates describe production.

## Repository connection and entrypoint inventory

### Configuration aliases and factory paths

| Configuration source or alias | Repository behavior | Evidence |
| --- | --- | --- |
| `DATABASE_URL` | Required Pydantic setting; default URL for the shared runtime engine and Alembic. | `app/core/config.py:L637-L640`; `app/core/db.py:L15-L47`; `alembic/env.py:L24-L61` |
| `ENV_FILE` | Chooses the settings dotenv filename; the tracked MCP wrapper sets it to its MCP-specific filename. | `app/core/config.py:L1291-L1302`; `scripts/mcp_server.sh:L1-L6` |
| Pool aliases | Per-process shared SQLAlchemy engine pool controls, including a `null` rollback mode. These change connection churn, not role permissions. | `app/core/db.py:L18-L47` |
| Development aliases | Make targets derive `DEV_DATABASE_URL` from development PostgreSQL inputs and pass it with `DEV_ENV_FILE` through a sanitized runtime environment. | `Makefile:L3-L23, L156-L167` |
| Explicit CLI URL | `protected_positions.py` requires `--database-url` and deliberately has no `DATABASE_URL` fallback. | `scripts/protected_positions.py:L1-L23, L78-L103` |
| Promotion URL | `promote_kr_candles_to_research.py` derives an asyncpg URL from `DATABASE_URL`; its write path is separately opt-in. | `scripts/promote_kr_candles_to_research.py:L1-L41, L58-L83` |
| Campaign URL | The ROB-974 script reads only the alias named by `ROB974_DATABASE_URL` after its own fixed-target checks. | `scripts/run_rob974_r2_campaign.py:L96-L109, L304-L337, L420-L446, L734-L780` |
| Dev-seed source URL | `make_dev_seed.py` requires `--source-database-url` and opens an asyncpg read-only transaction. | `scripts/make_dev_seed.py:L126-L137, L192-L213, L260-L305` |
| Tracked host-job input | The NCP job wrapper passes its selected `AT_RUNTIME_ENV_FILE` to the deployed image as `--env-file`; the documented templates set that input for scheduled application jobs. This design inspected templates, not an environment file. | `ops/ncp/bin/at-job.sh:L153-L187`; `ops/ncp/systemd/job-toss-warnings-sync.service:L7-L16`; `ops/ncp/systemd/job-us-market-valuation-snapshots.service:L7-L16` |
| Fill-handoff inputs | The tracked fill-handoff unit passes its application and handoff environment inputs to `scripts.fill_event_handoff`; that entrypoint opens the shared application session. | `ops/ncp/systemd/fill-event-handoff.service:L7-L16`; `scripts/fill_event_handoff.py:L18-L24, L98-L121` |
| Backup libpq inputs | The tracked backup service calls `pg_dump` and `pg_dumpall` through `ops/ncp/pg-backup.sh`. The script requires `PGHOST`, `PGPORT`, `PGUSER`, and one of `PGPASSWORD` or `PGPASSFILE`, and can forward SSL/connect controls. These are key names only, not inspected values. | `ops/ncp/systemd/at-pg-backup.service:L6-L15`; `ops/ncp/pg-backup.sh:L57-L104, L149-L167` |
| Test aliases | The test harness derives a run-owned `DATABASE_URL` from `AUTO_TRADER_TEST_DATABASE_URL` and records the xdist name, base URL, run UID, owner token, and shared-DB flags under its own aliases. | `tests/_run_owned_database.py:L12-L45, L135-L178` |

Within `app/`, `scripts/`, and `alembic/`, the Python-source static constructor audit
found the only non-test SQLAlchemy/asyncpg constructors in `app/core/db.py`,
`alembic/env.py`, and the four scripts above. Tracked
compose migration templates also contain inline connection checks and are called out in
the Alembic inventory below. The
complete test-only hit list is captured in [Tests and throwaway databases](#tests-and-throwaway-databases).
The exact non-test direct-constructor audit command was run under `pipefail` so an
invalid regular expression cannot be hidden by a later pipe stage:

```text
set -o pipefail
rg -n --glob '*.py' 'asyncpg\.(connect|create_pool)|create_async_engine|async_engine_from_config|create_engine\(|psycopg[0-9]?\.connect|psql\(' app scripts research alembic | rg -v '/tests/' | sort
```

It is deliberately complemented by shared-factory and host-unit discovery; a direct
constructor search alone cannot find a CLI that imports the already-created `engine`
or a systemd service that starts an application module:

```text
set -o pipefail
rg -n --glob '*.py' 'from app\.core\.db import (AsyncSessionLocal|engine)' app scripts research | sort
rg -n --glob '*.service' --glob '*.service.in' --glob '*.sh' 'AT_RUNTIME_ENV_FILE|--env-file|pg_dump|pg_dumpall|PGHOST|PGUSER' ops/ncp | sort
```

The `app/services/brokers/binance/demo/ledger/service.py` session factory binds an
already supplied session/engine rather than creating a separate URL-bearing engine
[app/services/brokers/binance/demo/ledger/service.py:L116-L145]; it is not an
additional connection identity.

### API

FastAPI is constructed in `create_app()` and exposed as both `api` and `app`
[app/main.py:L301-L345]. Router dependencies obtain `AsyncSession` through `get_db`
from the shared factory [app/routers/dependencies.py:L1-L33; app/core/db.py:L118-L120].
The tracked API image launches Uvicorn [Dockerfile.api:L98-L108]; the tracked
production compose template supplies an env file to the API service
[docker-compose.prod.yml:L26-L56], and the tracked NCP pull script launches an API
container with its runtime and secret-file arguments [scripts/deploy-ncp-pull.sh:L31-L40,
L77-L80]. These are deployment evidence, not proof of a live instance.

**Role result:** API logins belong only to `at_app`. They must receive the reviewed
table/sequence/function manifest below, never owner or migration authority.

### TaskIQ workers and scheduler

TaskIQ itself configures Redis queues/results, not a separate PostgreSQL factory
[app/core/taskiq_broker.py:L41-L68]. Tasks are centrally imported from
`app.tasks` [app/tasks/__init__.py:L1-L77]; individual tasks open the same lazy
`AsyncSessionLocal` when they need database work. The scheduler wraps that same broker
[app/core/scheduler.py:L1-L13]. The tracked compose template runs worker and scheduler
as distinct services [docker-compose.prod.yml:L50-L87], while the NCP pull script has
separate worker and singleton scheduler launch functions [scripts/deploy-ncp-pull.sh:L77-L80,
L140-L147].

**Role result:** worker and scheduler require separate login identities but the same
`at_app` object manifest until a future task-by-task split is separately designed.
They are separate rotation and drain targets: an old worker can consume queued work and
an old scheduler can continue to open its old pool even after the API has changed.
This design adds no scheduler and does not alter existing TaskIQ behavior.

### MCP server

The MCP process imports application settings, builds a `FastMCP` server, and registers
the selected profile's tools [app/mcp_server/main.py:L1-L55, L148-L188, L201-L276].
Tool handlers use the shared application session factory; for example,
`investment_reports_handlers` imports `AsyncSessionLocal` and opens it in a handler
[app/mcp_server/tooling/investment_reports_handlers.py:L19, L530-L538]. The tracked
compose file defines multiple MCP services [docker-compose.prod.yml:L89-L211] and the
deployment script enumerates named MCP units/profiles and starts
`python -m app.mcp_server.main` [scripts/deploy-ncp-pull.sh:L37-L41, L150-L160].

**Role result:** each MCP deployment gets its own login that is an `INHERIT` member
of only `at_app`, because neither the MCP bootstrap nor the shared engine performs
`SET ROLE`. On PostgreSQL 16 or later, the reviewed membership must explicitly have
`INHERIT TRUE`, `SET FALSE`, and `ADMIN FALSE`; on earlier supported versions, the
login itself must retain `INHERIT` and no role-switch path. A read-only MCP profile is
an MCP-tool surface property, not evidence that its process can safely share a
database reporting role: the server binary registers a broad application tool set by
profile and there is no DB-identity selector in the source. A separate reporting
identity would require a future, explicitly configured read-only process.

### CLI and monitors

The following repository entrypoints are material to a staged rotation:

| Entry point | Connection behavior | Proposed treatment |
| --- | --- | --- |
| `websocket_monitor.py` | Imports settings and `AsyncSessionLocal`; its ledger work opens shared sessions. The tracked compose template starts it in both monitored modes. | Treat every deployed monitor instance as an `at_app` login rotation/drain target. |
| `kis_websocket_monitor.py` and `upbit_websocket_monitor.py` | No independent SQLAlchemy/asyncpg factory was found in these entrypoint modules; `Dockerfile.ws` separately ships them and defaults to an Upbit monitor command. | Do not assume they are DB-free in a wrapper deployment; confirm the actual command and imports during preflight. |
| `manage_users.py` and other shared-factory CLIs | Import `AsyncSessionLocal`; they inherit the shared `DATABASE_URL` behavior. | Use a named operator/app login only after a command-by-command manifest review. |
| `scripts/protected_positions.py` | Explicit required `--database-url`; read-only command surface. | Do not silently replace an operator-provided URL. If it targets production, use a separately approved read-only login. |
| `scripts/promote_kr_candles_to_research.py` | Direct asyncpg connection from `DATABASE_URL`; confirmed mode can write. | Rotate only with the application identity after its normal-table DML manifest covers the promotion path. |
| `scripts/run_rob974_r2_campaign.py` | Direct `create_async_engine` from `ROB974_DATABASE_URL`, including a read-only schema guard and a separate run engine. | Preserve its target/identity gates; do not substitute the generic application role without its own approved campaign manifest. |
| `scripts/make_dev_seed.py` | Direct asyncpg connection from required operator CLI input, explicitly read-only. | Use a future reporting role only if its exact source tables are approved; do not make it a production application credential. |
| `scripts/retrospective_action_cutover.py` | Imports the shared `engine` and begins a database transaction for its guarded cutover. | Treat it as an approved operational application CLI only after its table/lock requirements receive a named manifest review; it is not a separate URL factory. |

The deployment evidence for the unified monitor is the tracked `websocket_monitor.py`
command in compose [docker-compose.prod.yml:L213-L275] and the dedicated `run_ws`
launcher [scripts/deploy-ncp-pull.sh:L77-L83]. Its database imports and session use are
in `websocket_monitor.py:L22-L23, L254-L268, L302-L312`. `manage_users.py` uses the
shared session factory for both reads and updates [manage_users.py:L20-L23, L51-L125].
The direct CLI constructors are cited in the configuration table above; the
retrospective cutover's shared-engine import and transaction are
[scripts/retrospective_action_cutover.py:L51-L60, L91-L94].

#### Tracked NCP systemd consumers and backup client

The direct Python constructor search is not a complete deployment inventory. The
tracked NCP systemd templates contain existing background entrypoints; their presence
does not prove installation, but an operator must inventory and rotate every installed
one before declaring a credential cutover complete. This document did not open any of
the environment files named by those templates.

| Tracked unit class | Repository connection path | Cutover treatment |
| --- | --- | --- |
| Application job timers | `job-crypto-invest-insight-snapshots`, `job-crypto-invest-screener-snapshots`, `job-kr-fundamentals-snapshots`, `job-kr-investor-flow-snapshots`, `job-toss-symbol-master-sync`, `job-toss-warnings-sync`, `job-us-fundamentals-snapshots`, `job-us-invest-screener-snapshots`, and `job-us-market-valuation-snapshots` invoke named application modules through `at-job.sh`. The wrapper supplies its runtime environment to the deployed image; example modules import the shared factory. | Treat every installed unit and paired existing timer as an `at_app` login rotation/drain target. The change record must name its module, login, credential input, timer cadence, and final successful new-login run. Do not add, re-register, or retime a timer in this Phase 1 design. |
| Fill-event handoff timer | `fill-event-handoff.service` starts `scripts.fill_event_handoff --once`, which opens `AsyncSessionLocal`. | Treat an installed unit as an `at_app` consumer and rotate/drain it with the application credentials. The handoff-specific input is not a substitute for the application database identity. |
| PostgreSQL backup timer | `at-pg-backup.service` invokes `pg-backup.sh`, whose `pg_dump` and `pg_dumpall --globals-only` calls use a distinct libpq identity. | Keep it outside `at_app`; inventory its cluster/database scope and least privilege separately. Do not silently reuse an application or migration login for the backup client. Its daily cadence is part of the post-rotation observation window. |
| Resident lane-kickoff timers | The eleven tracked `job-kickoff-{0905,1130,1430,crypto-0220,crypto-0820,crypto-1420,crypto-2020,nxt-eve,nxt-open,nxt-prep,us-2235}` units pass the API and lane-event inputs to `scripts.lane_event_kickoff`. Its static import path is lane-event emission, not the application database factory. | Record every installed unit and paired timer as a credential-input consumer, then confirm its deployed command remains non-PostgreSQL before excluding it from the `at_app` client matrix. Remove a legacy application credential from its inherited application input even when the current source does not open a database connection. |
| B0X lane-event templates | The six `job-kickoff-b0x-{harvest,nudge-crypto,nudge-kr,nudge-us,table-kr,table-us}` templates and `b0x-lane-event-consumer.service.in` are explicitly non-operational until installer rendering. Their tracked commands invoke lane-event helpers with a template environment input; the static entrypoints have no shared or direct PostgreSQL factory import. | Do not treat a template as installed. If an approved operator finds one rendered, add its exact command, credential-input name, database classification, and timer cadence to the signed inventory before legacy-credential retirement. Do not activate or retime it for this design. |
| MCP watchdog timer | `at-mcp-watchdog.service` runs an external `/root/at-run` watchdog path rather than a repository Python module. Repository source cannot establish whether that external path opens a database or restarts a credential-bearing MCP process. | Include every installed watchdog unit in the deployment/restart inventory and classify its actual process and credential inputs before completion; do not presume that a timer without a repository database import is harmless. |

The unit names above are tracked-template evidence, not an assertion that each one is
installed. Their common wrapper is [ops/ncp/bin/at-job.sh:L153-L187]; representative
unit bindings are [ops/ncp/systemd/job-toss-warnings-sync.service:L7-L16,
ops/ncp/systemd/job-us-market-valuation-snapshots.service:L7-L16], the fill-handoff
binding is [ops/ncp/systemd/fill-event-handoff.service:L7-L16], and the backup service
and client are [ops/ncp/systemd/at-pg-backup.service:L6-L15,
ops/ncp/pg-backup.sh:L57-L104, L149-L167]. The scheduled modules are transitive
shared-factory consumers rather than new connection factories; for example,
`app.jobs.toss_warnings` and `app.jobs.market_valuation_snapshots` import
`AsyncSessionLocal` [app/jobs/toss_warnings.py:L1-L25,
app/jobs/market_valuation_snapshots.py:L1-L20, L100-L127].

The remaining tracked service families are included above rather than silently
discarded: a representative resident kickoff unit supplies its two environment inputs
and runs `scripts.lane_event_kickoff` [ops/ncp/systemd/job-kickoff-0905.service:L7-L21],
the B0X kickoff template is explicitly render-gated
[ops/ncp/systemd/job-kickoff-b0x-harvest.service.in:L6-L20], the B0X consumer template
names its binding and state-db input [ops/ncp/systemd/b0x-lane-event-consumer.service.in:L6-L17],
and the watchdog runs a non-repository path [ops/ncp/systemd/at-mcp-watchdog.service:L6-L12].
`scripts.lane_event_kickoff` imports the lane-event API at its entrypoint
[scripts/lane_event_kickoff.py:L1-L20]. The inventory command for an operator record
is `rg --files ops/ncp/systemd | sort`, paired by service/timer basename and reconciled
with the installed-unit query in [Open operator questions](#open-operator-questions).

#### Full static CLI/research consumer audit

The following source files import `AsyncSessionLocal` or the shared `engine`; they are
**candidate rotation targets if deployed**, not proof that they run in production. They
do not create a new connection factory or a different URL alias; when invoked, they use
the shared `DATABASE_URL` path. This exact source audit prevents manual CLI surfaces
from being mistaken for a small, API-only cutover:

```text
research/alpaca_track_seal/registry_cli.py
research/nautilus_scalping/run_rob940_empirical_materializer.py
research/nautilus_scalping/run_rob944_campaign.py
scripts/attribute_daily_spikes.py
scripts/b0x/kr/attribution.py
scripts/b0x/kr/kiwoom_durable_ports.py
scripts/b0x/kr/pending_ledger.py
scripts/backfill_alpaca_paper_execution_asset_class.py
scripts/backfill_news_related_symbols.py
scripts/binance_demo_strategy_loop.py
scripts/binance_futures_demo_smoke.py
scripts/binance_spot_demo_d2_remediation.py
scripts/binance_spot_demo_smoke.py
scripts/cleanup_toss_manual_holdings.py
scripts/diagnose_calendar_coverage.py
scripts/diagnose_invest_screener_snapshots.py
scripts/diagnose_invest_screener_toss_parity.py
scripts/diagnose_kr_dividend_conditions.py
scripts/diagnose_negative_class_recording.py
scripts/diagnose_research_reports.py
scripts/fill_event_handoff.py
scripts/fill_handoff_bundle.py
scripts/fix_overseas_exchange_codes.py
scripts/forecast_legacy_rule_migration.py
scripts/ingest_freqtrade_report.py
scripts/ingest_market_events.py
scripts/ingest_research_reports.py
scripts/ingest_validated_run_card.py
scripts/invest_reports_us_schedule.py
scripts/krb1_p0_liquidity_selector.py
scripts/list_recent_fill_events.py
scripts/list_recent_watch_events.py
scripts/list_rob900_toss_projection_backfill_candidates.py
scripts/news_issue_lab.py
scripts/news_quality_baseline.py
scripts/policy_table/adapters/crypto.py
scripts/policy_table/adapters/kr.py
scripts/policy_table/adapters/us.py
scripts/precompute_spike_attribution.py
scripts/probe_us_earnings_coverage.py
scripts/quote_parity_shadow_probe.py
scripts/reconcile_execution_ledger.py
scripts/record_strategy_learning_event.py
scripts/remote_debug_audit_smoke.py
scripts/rob1284_resting_rung_sweep.py
scripts/rob178_smoke.py
scripts/rob278_kr_dryrun.py
scripts/rob837_reconcile_upbit_proposal.py
scripts/retrospective_action_cutover.py
scripts/run_market_close_digest.py
scripts/run_research_run_refresh.py
scripts/seed_execution_ledger_opening_lots.py
scripts/shadow_new_candidates_report.py
scripts/shadow_replay.py
scripts/smoke/alpaca_paper_fill_reconcile_smoke.py
scripts/smoke/alpaca_paper_sell_close_smoke.py
scripts/snapshot_bundle_smoke.py
scripts/sync_journal_counterfactuals.py
scripts/sync_journal_verdicts.py
scripts/sync_mock_roundtrip_journals.py
scripts/sync_toss_symbol_master.py
scripts/sync_watch_follow_up_items.py
```

There are also direct, non-shared research factories that must be explicitly excluded
or given a dedicated non-production/read-only plan rather than silently receiving an
application credential:

| Direct research factory | Alias / guard visible in source | Evidence and cutover treatment |
| --- | --- | --- |
| KR backfill baseline | `DATABASE_URL`; it sets the session read-only. | `research/kr_backfill/baseline.py:L1-L12, L38-L74`; no production-role assumption. |
| KR backfill collect and preflight | `DATABASE_URL`; asyncpg pools. | `research/kr_backfill/collect.py:L295-L330, L1383-L1386, L1596-L1599`; `research/kr_backfill/preflight.py:L25-L34, L160-L171`. A separate approved write/read plan is required. |
| Screener bakeoff panel | `BAKEOFF_DATABASE_URL` falls back to `DATABASE_URL`; source declares SELECT-only behavior. | `research/screener_bakeoff/panel.py:L1-L36`; candidate future reporting-role consumer only after a named manifest. |
| Toss Phase 2 research loader | Reads a dedicated non-production-named dotenv in its own guarded code, then opens asyncpg. | `research/toss_phase2/load.py:L676-L688, L795-L800`; do not open or rotate an env file as part of this design. |

The research campaign CLIs in the list above defer to the shared factory only after
their own gates; for example, the ROB-940 runner imports and opens it inside the
execution path [research/nautilus_scalping/run_rob940_empirical_materializer.py:L239-L271].
They are repository evidence, not authorization to attach a production role.

### Alembic

Alembic ignores the placeholder URL in `alembic.ini` for normal execution: offline
mode uses `settings.DATABASE_URL`, and online mode passes that setting to an async
engine with `NullPool` [alembic/env.py:L24-L61]. The tracked compose migration service
also performs an asyncpg connection check before `alembic current` and
`alembic upgrade head` [docker-compose.prod.yml:L1-L24].

Additional repository migration entrypoints are tracked configuration, not confirmed
production wiring: `docker-compose.migration.yml` supplies an env file and a separate
inline asyncpg/Alembic sequence [docker-compose.migration.yml:L1-L26];
`scripts/migrate.sh` sources its configured dotenv and invokes psql/Alembic
[scripts/migrate.sh:L14-L32, L64-L105]; and `scripts/migration-check.sh` reads its
ambient `DATABASE_URL` for psql checks [scripts/migration-check.sh:L17-L52]. None was
run or supplied an environment file during this design work. Any of them that remains
an operator production path must be added to the migration-runner credential rotation
inventory.

**Role result:** after cutover, Alembic must run as `current_user =
at_migration_owner` through the reviewed migration-runner procedure. It must never run
as `at_app` merely because both URLs are named `DATABASE_URL` in existing source.
Changing that identity is future operational configuration, outside this PR.

### Tests

The test suite is intentionally different from production. It selects a per-run
database name before a database connection [tests/conftest.py:L172-L185], creates and
marks only its exact run-owned databases through an admin asyncpg connection
[tests/_run_owned_database.py:L196-L258], then drops only a database bearing its
matching ownership marker [tests/_run_owned_database.py:L261-L306]. Its schema barrier
performs DDL and uses the shared app engine only against that throwaway target
[tests/conftest.py:L874-L980].

This design does **not** change tests. The test administrator must remain a separate,
non-production identity allowed to create and drop only validated run-owned test
databases. Do not point test URLs at a production `at_app` login and do not remove its
isolated database lifecycle merely to make a role test pass.

For completeness, the static search for `create_async_engine`,
`async_engine_from_config`, `asyncpg.connect`, and sync `create_engine` found these
test-only paths. They are not production entrypoints and none is a reason to grant
production application roles database-create authority:

| Test factory class | Paths |
| --- | --- |
| Run-owned lifecycle / schema infrastructure | `tests/_run_owned_database.py`; `tests/infra/test_schema_barrier.py` |
| Isolated integration or SQLite engines | `tests/integration/test_paper_positions_market_filter.py`; `tests/integration/test_strategy_event_db_roundtrip.py`; `tests/test_us_symbol_universe_sync.py`; `tests/services/test_order_preview_session_service.py`; `tests/services/test_pending_order_sync_service.py`; `tests/services/test_legacy_stock_analysis_adapter.py`; `tests/services/test_us_symbol_universe_names.py`; `tests/services/brokers/binance/demo_scalping_exec/test_executor_reservation_race.py` |
| Disposable migration databases | `tests/services/invalid_sample_eligibility/test_migration.py`; `tests/services/order_proposals/callback_inbox/test_migration_chain.py`; `tests/services/paper_cohort/test_migration.py`; `tests/services/paper_evaluation/test_migration.py` |
| NHPLUG / callback worker fixtures | `tests/services/nhplug_mock/claim_worker.py`; `tests/services/nhplug_mock/dispatch_test_worker.py`; `tests/services/order_proposals/callback_inbox/test_lock_cleanup_cancellation.py`; `tests/services/order_proposals/callback_inbox/test_gates_and_registration.py` |
| Guard/source-fixture references rather than a production connection path | `tests/infra/test_database_guard_completeness.py`; `tests/research/test_screener_bakeoff_parity.py` |

The source-to-document audit uses this constructor-pattern family and confirms that
this list is complete for the cited patterns at this design revision.

## Target privileges and ownership

### General database and schema policy

The target database should grant `CONNECT` only to the reviewed group roles and their
needed deployment logins. `at_app` gets `USAGE`, never `CREATE`, on each schema in its
approved manifest. The initial repository evidence points to `public`, `review`, and
`research`, but production catalog output decides the exact schema list. `PUBLIC`
must not retain unreviewed `CREATE` on an application schema, and `at_app` must not
own any table, schema, sequence, or function.

Do not use `GRANT ... ON ALL TABLES IN SCHEMA ...` as the final application policy.
That would include future objects and can accidentally give the application DML on a
new control table. Instead, keep an object-level manifest generated from catalog
output and static call-site review, then apply explicit grants. The source contains
real normal-domain `DELETE` paths, so a universal "read/insert/update only" policy
would be incorrect; `DELETE` is granted only to named normal tables whose reviewed
call paths require it. `TRUNCATE`, `REFERENCES`, `TRIGGER`, `MAINTAIN`, and ownership
remain absent from every application grant.

### #711 review-schema manifest

The #711 migration creates the control and ledger tables, the identity-backed ledger,
and the `review.nhplug_consume_authorization` SECURITY DEFINER function
[alembic/versions/20260926_task711_nhplug_dispatch.py:L45-L130, L286-L305]. Its design
requires application/MCP `SELECT` only on the operator authorization row and reserves
control-row insertion for an operator role
[docs/design/711-nhplug-dispatch-state-machine.md:L625-L634]. The actual app service
also needs account-reference/binding inserts [app/services/nhplug_mock/account_identity.py:L84-L169]
and ledger reads/inserts/updates [app/services/nhplug_mock/ledger.py:L114-L160,
L162-L264, L632-L661]. The resulting target manifest is intentionally explicit:

| Object | `at_app` | `nhplug_operator` | Owner / reason |
| --- | --- | --- | --- |
| `review.nhplug_mock_key_version` | `SELECT` | `SELECT, INSERT` | `nhplug_security_owner`; application reads retained versions, operator controls new versions. |
| `review.nhplug_success_proof_code` | `SELECT` | `SELECT, INSERT` | `nhplug_security_owner`; operator controls admissible success evidence. |
| `review.nhplug_no_order_proof_code` | `SELECT` | `SELECT, INSERT` | `nhplug_security_owner`; operator controls admissible no-order evidence. |
| `review.nhplug_mock_operator_authorization` | `SELECT` only | `SELECT, INSERT` only | `nhplug_security_owner`; application cannot create, alter, or erase an authorization. |
| `review.nhplug_mock_account_ref` | `SELECT, INSERT` | no grant by default | `at_migration_owner`; account-reference creation is an application identity operation. |
| `review.nhplug_mock_account_binding` | `SELECT, INSERT` | no grant by default | `at_migration_owner`; binding creation is application identity work. |
| `review.nhplug_mock_order_ledger` | `SELECT, INSERT, UPDATE` | `SELECT` only | `at_migration_owner`; app must advance constrained state through the trigger, but neither role receives `DELETE` or `TRUNCATE`. |

The table is a proposal for a post-cutover ACL reconciliation, not evidence that
production currently has these ACLs. `nhplug_operator` deliberately gets no direct
ledger `UPDATE`: the app update that binds an approved candidate causes the database
trigger to consume the authorization [app/services/nhplug_mock/ledger.py:L632-L661].
The tracked migration currently gives `nhplug_operator` `SELECT, UPDATE` on the
ledger [alembic/versions/20260926_task711_nhplug_dispatch.py:L545-L557]; that is a
source fact to reconcile, not a reason to preserve excess update power.

The proposed new `nhplug_security_owner` exists because the tracked migration assigns
several control objects and the two security-definer functions to `nhplug_operator`
[alembic/versions/20260926_task711_nhplug_dispatch.py:L545-L556]. An owner inherently
has power beyond a table ACL. Leaving the operator writer as the object owner defeats
the intended split. A later approved ownership transition should therefore move the
protected tables and functions to `nhplug_security_owner`, then make
`nhplug_operator` a non-owner DML group. This document does not perform that
transition.

### ROB-1340 Kiwoom authority-evidence exception

Two other `review` tables are not ordinary generic-DML objects:
`review.kiwoom_authority_attempts` and
`review.kiwoom_authority_cessation_receipts`. The runtime readiness projection requires
the effective application role to have exactly `SELECT = true`, `INSERT = true`, and
`UPDATE = DELETE = TRUNCATE = false` on both
[app/services/brokers/kiwoom/coordination_store.py:L141-L158, L279-L421]. The store
commits append-only start and terminal evidence then independently reads it back
[app/services/brokers/kiwoom/coordination_store.py:L455-L503, L534-L628]. The migration
creates immutable update/delete/truncate triggers
[alembic/versions/20260902_rob1340_authority_cessation.py:L191-L220], but trigger
rejection is defense in depth rather than a reason to grant excess capability.

| Object | `at_app` | `nhplug_operator` | Owner / reason |
| --- | --- | --- | --- |
| `review.kiwoom_authority_attempts` | `SELECT, INSERT` only | no grant by default | `at_migration_owner`; append-only authority-attempt evidence. |
| `review.kiwoom_authority_cessation_receipts` | `SELECT, INSERT` only | no grant by default | `at_migration_owner`; append-only terminal-receipt evidence. |

Each identity-backed `id` sequence needs the same catalog-discovered `USAGE`-only
application treatment as the #711 ledger sequence. Do not grant `SELECT` or `UPDATE`
on either sequence. The owner batch must also revoke `PUBLIC` direct execute on
`review.reject_kiwoom_authority_evidence_mutation()`; no application or operator path
needs to invoke that trigger function directly.

`nhplug_security_owner` also needs the narrowly scoped `SELECT, UPDATE` privilege on
`review.nhplug_mock_order_ledger` after ownership is separated: the SECURITY DEFINER
guard locks/reads ledger rows while enforcing transitions. That grant is for the
non-login function owner only; it is not an operator or application grant.

The intended #711 ownership map is also explicit: `at_migration_owner` owns the
`review` schema plus `nhplug_mock_account_ref`, `nhplug_mock_account_binding`,
`nhplug_mock_order_ledger`, `nhplug_body_field`, and `nhplug_body_digest_v1`.
`nhplug_security_owner` owns `nhplug_mock_key_version`, both proof-code tables,
`nhplug_mock_operator_authorization`, `nhplug_consume_authorization`,
`nhplug_order_guard`, `nhplug_append_only`, `nhplug_key_registry_insert`, and
`nhplug_auth_immutable`. The reviewed owner batch should use named `ALTER ... OWNER TO`
statements for only those objects after confirming their current owners; it must not
use a blanket ownership transfer or change extension-owned members.

`at_migration_owner` also owns both ROB-1340 authority-evidence tables and
`review.reject_kiwoom_authority_evidence_mutation()`. They remain ordinary
non-login-owner objects, not #711 operator-control objects, but their ACL matrix is
protected and must not be folded into a broad review-schema grant.

### Proposed #711 schema/table/function ACL batch

After the preflight confirms that all named schemas and objects exist, the following is
the explicit **proposal** for the protected-object portion of the cutover. It is not a
blind production script: object ownership must be changed first in the reviewed owner
batch, and any existing non-target grantee must be evaluated rather than silently
removed. The normal `public`/`research` DML manifest remains a separately reviewed,
object-by-object list.

```sql
-- Apply only after the catalog has confirmed each named schema/object.
GRANT USAGE ON SCHEMA review TO at_app, nhplug_operator, nhplug_security_owner;
GRANT USAGE ON SCHEMA public, research TO at_app;

REVOKE ALL PRIVILEGES ON TABLE
  review.nhplug_mock_key_version,
  review.nhplug_success_proof_code,
  review.nhplug_no_order_proof_code,
  review.nhplug_mock_operator_authorization,
  review.nhplug_mock_account_ref,
  review.nhplug_mock_account_binding,
  review.nhplug_mock_order_ledger,
  review.kiwoom_authority_attempts,
  review.kiwoom_authority_cessation_receipts
FROM PUBLIC;

REVOKE ALL PRIVILEGES ON TABLE
  review.nhplug_mock_key_version,
  review.nhplug_success_proof_code,
  review.nhplug_no_order_proof_code,
  review.nhplug_mock_operator_authorization,
  review.nhplug_mock_account_ref,
  review.nhplug_mock_account_binding,
  review.nhplug_mock_order_ledger,
  review.kiwoom_authority_attempts,
  review.kiwoom_authority_cessation_receipts
FROM at_app, nhplug_operator;

GRANT SELECT ON TABLE review.nhplug_mock_key_version,
  review.nhplug_success_proof_code,
  review.nhplug_no_order_proof_code,
  review.nhplug_mock_operator_authorization
TO at_app;
GRANT SELECT, INSERT ON TABLE review.nhplug_mock_key_version,
  review.nhplug_success_proof_code,
  review.nhplug_no_order_proof_code,
  review.nhplug_mock_operator_authorization
TO nhplug_operator;
GRANT SELECT, INSERT ON TABLE review.nhplug_mock_account_ref,
  review.nhplug_mock_account_binding
TO at_app;
GRANT SELECT, INSERT, UPDATE ON TABLE review.nhplug_mock_order_ledger TO at_app;
GRANT SELECT ON TABLE review.nhplug_mock_order_ledger TO nhplug_operator;
GRANT SELECT, UPDATE ON TABLE review.nhplug_mock_order_ledger
TO nhplug_security_owner;
GRANT SELECT, INSERT ON TABLE review.kiwoom_authority_attempts,
  review.kiwoom_authority_cessation_receipts
TO at_app;

REVOKE ALL ON FUNCTION
  review.nhplug_body_field(text,text),
  review.nhplug_body_digest_v1(text,text,text,bigint,bigint,text,text,text),
  review.nhplug_consume_authorization(uuid,text,bigint,uuid,date,text,text,bigint),
  review.nhplug_order_guard(),
  review.nhplug_append_only(),
  review.nhplug_key_registry_insert(),
  review.nhplug_auth_immutable(),
  review.reject_kiwoom_authority_evidence_mutation()
FROM PUBLIC, at_app, nhplug_operator;
```

The final `GRANT CONNECT ON DATABASE ...` is database-name specific and must use the
already verified target identifier, not a URL or a guessed default. Login-role creation,
membership, and authentication provisioning are separate reviewed actions; application
login membership in `at_app` must be inheritable, while migration and desk memberships
are intentionally role-switched as described above.

### Sequence policy

For application-owned/used identity or serial sequences, grant `USAGE` only when an
insert path needs to obtain a generated value. `USAGE` permits the underlying
`nextval` operation and also `currval`; that is the unavoidable narrow capability for
an identity insert. Do **not** grant `SELECT` merely for inserts (it adds direct
sequence-state/read capability), and do **not** grant `UPDATE` (it permits `setval`,
as well as `nextval`). The source scan found no application call to `currval`,
`nextval`, or `setval`; the #711 ledger uses `INSERT ... RETURNING` rather than a
direct sequence call [app/services/nhplug_mock/ledger.py:L213-L248]. PostgreSQL
distinguishes these three sequence permissions; validate `USAGE` against the actual
identity-insert path. See the PostgreSQL [GRANT reference](https://www.postgresql.org/docs/current/sql-grant.html)
and [sequence-function reference](https://www.postgresql.org/docs/current/functions-sequence.html).

The #711 ledger and the two ROB-1340 append-only-evidence sequences must be discovered
rather than assumed from conventional names:

```sql
BEGIN READ ONLY;
SELECT pg_get_serial_sequence('review.nhplug_mock_order_ledger', 'id') AS ledger_sequence,
       pg_get_serial_sequence('review.kiwoom_authority_attempts', 'id')
         AS authority_attempts_sequence,
       pg_get_serial_sequence('review.kiwoom_authority_cessation_receipts', 'id')
         AS authority_receipts_sequence;
ROLLBACK;
```

After the operator reviews that output, grant `USAGE` on precisely each returned
sequence to `at_app`, and revoke `SELECT, UPDATE` from `at_app` and
`nhplug_operator` on those same names. This gives the authority-evidence insert path
the exact sequence capability needed by its generated primary key, while keeping the
runtime's required no-UPDATE contract intact. Repeat the same catalog-driven process
for normal application sequences. Never grant a blanket `ALL SEQUENCES IN SCHEMA`
privilege before comparing it with the object manifest.

### Functions, SECURITY DEFINER, and RLS

The tracked #711 functions are especially sensitive:

- `review.nhplug_consume_authorization(...)` is `SECURITY DEFINER`, locks and consumes
  an authorization, and uses `SET search_path = pg_catalog, review`
  [alembic/versions/20260926_task711_nhplug_dispatch.py:L286-L303].
- `review.nhplug_order_guard()` is also `SECURITY DEFINER` and invokes the consume
  function while enforcing order transitions [alembic/versions/20260926_task711_nhplug_dispatch.py:L304-L350,
  L520-L543].
- The migration contains no explicit `REVOKE ... FROM PUBLIC` or explicit function
  `GRANT EXECUTE`; PostgreSQL normally grants `PUBLIC` EXECUTE for newly created
  functions. The live `proacl` must be checked before relying on any restriction.
  PostgreSQL recommends revoking `PUBLIC` and selectively granting execution for a
  SECURITY DEFINER function in the same transaction as its creation; see
  [CREATE FUNCTION](https://www.postgresql.org/docs/current/sql-createfunction.html).

The target function policy is:

1. Revoke `PUBLIC` execute on every #711 helper and trigger/consume function after
   verifying the current catalog state.
2. Give **no direct execute** on `nhplug_consume_authorization` or
   `nhplug_order_guard` to `at_app` or `nhplug_operator`; the ledger trigger is the
   approved path. The non-login security owner retains its owner authority.
3. Decide whether `at_app` needs `EXECUTE` on the deterministic body-digest helpers
   only through a staging transaction that exercises an application ledger insert.
   Static application code does not directly call them, but generated/default
   expression privilege checks are a database-version fact. Grant only the exact
   helper functions if that test proves it necessary.
4. Ensure `nhplug_security_owner` is `NOLOGIN`, `NOSUPERUSER`, `NOBYPASSRLS`, has no
   untrusted role memberships, and has no `CREATE` privilege on a schema that an
   untrusted role can populate. Preserve and inspect the fixed function
   `search_path`; do not casually rewrite a SECURITY DEFINER function during an ACL
   cutover.

The source audit found no tracked `ENABLE ROW LEVEL SECURITY`, `FORCE ROW LEVEL
SECURITY`, or `CREATE POLICY` DDL. That does **not** prove absence in production. Table
owners commonly have different RLS behavior from ordinary roles, so ownership and any
existing policy must be catalog-reviewed before the cutover. Do not introduce, relax,
or rely on RLS as part of this role change.

### Default privileges by object creator

Default privileges apply only to objects created later by the *current creating role*;
they do not repair existing ACLs and are not inherited from a role membership. This is
why migrations must establish `current_user = at_migration_owner`, not merely connect
as a member login. PostgreSQL documents both constraints in
[ALTER DEFAULT PRIVILEGES](https://www.postgresql.org/docs/current/sql-alterdefaultprivileges.html).

For each future object-creator (`at_migration_owner` and `nhplug_security_owner`), the
approved DDL procedure should establish global function safety and explicit schema
defaults. The following is proposed mutation SQL for an operator-reviewed migration
batch, not SQL to execute from this PR:

```sql
-- Run as a role permitted to alter the named creator's defaults.
ALTER DEFAULT PRIVILEGES FOR ROLE at_migration_owner
  REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC;
ALTER DEFAULT PRIVILEGES FOR ROLE nhplug_security_owner
  REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC;

ALTER DEFAULT PRIVILEGES FOR ROLE at_migration_owner IN SCHEMA public, review, research
  REVOKE ALL ON TABLES FROM PUBLIC;
ALTER DEFAULT PRIVILEGES FOR ROLE at_migration_owner IN SCHEMA public, review, research
  REVOKE ALL ON SEQUENCES FROM PUBLIC;
ALTER DEFAULT PRIVILEGES FOR ROLE nhplug_security_owner IN SCHEMA review
  REVOKE ALL ON TABLES FROM PUBLIC;
ALTER DEFAULT PRIVILEGES FOR ROLE nhplug_security_owner IN SCHEMA review
  REVOKE ALL ON SEQUENCES FROM PUBLIC;
```

There is intentionally no default grant of broad DML to `at_app`. Every migration that
creates an application object must add an explicit reviewed ACL entry to the manifest;
the owner procedure then applies it in the same reviewed change. A schema-qualified
function default cannot undo a global public execute default, hence the global function
revokes above. Existing objects require separate, catalog-reviewed reconciliation.

### Migration compatibility blocker: #711's `current_user` grant

At the end of the tracked #711 migration, `app_role := current_user` receives `SELECT`
on four control tables [alembic/versions/20260926_task711_nhplug_dispatch.py:L545-L548].
That works only when the Alembic runner happens also to be the application role. After
this design's split, it would grant those reads to `at_migration_owner`, not `at_app`.
It also leaves the account-reference/binding and ledger ownership/grant behavior tied
to the historical runner.

**Blocker:** do not run #711 for the first time with the new migration identity, and do
not claim the role split complete on a database where #711 may be applied later, until
a separately approved forward migration or controlled DBA ACL transition explicitly:

1. grants the table manifest above to `at_app` and `nhplug_operator`;
2. moves protected ownership to the approved non-login owners;
3. revokes legacy owner and `PUBLIC` function access after all replacement grants are
   verified; and
4. proves the trigger-mediated authorization consumption path in a staging clone.

Do not edit migration history to solve this. This Phase 1 design identifies the
required Phase 2 work; it does not authorize or create it.

There is a second historical-bootstrap constraint. #711 conditionally creates the
cluster-wide `nhplug_operator` role and then transfers object ownership to it
[alembic/versions/20260926_task711_nhplug_dispatch.py:L45-L59, L545-L556]. A final
`at_migration_owner` deliberately has no `CREATEROLE`; a non-superuser changing an
owner also needs the controlled ability to become the target owner, as described in
the PostgreSQL [privileges reference](https://www.postgresql.org/docs/current/ddl-priv.html).
The recommended default is therefore to cut roles over only after the target database
is already at its approved Alembic head. For a new database that still needs #711,
require a separately approved one-time bootstrap procedure that pre-creates/authorizes
the legacy target role, records the temporary ownership capability, and immediately
applies the forward ACL/ownership reconciliation. Do not grant persistent `CREATEROLE`
or a broad owner membership to `at_app` to make history run.

## Migration identity, extensions, and tests

### Migration execution identity and object ownership

The normal migration connection must be an auditable runner that establishes
`SET ROLE at_migration_owner` before Alembic creates objects. The runner's preflight
must record both `session_user` and `current_user`; only the latter determines newly
created object ownership and default privileges. Application services cannot reuse
this URL, credential, membership, or role-switch facility.

Existing objects require a reviewed per-object ownership map. Do not use blanket
`REASSIGN OWNED` or `DROP OWNED`: they can touch unrelated objects and make it hard to
exclude extension members or non-application schemas. For each approved object, the
operator should review catalog output and execute a named `ALTER ... OWNER TO ...` in
a transactional catalog batch, then verify the owner/ACL result. `nhplug_security_owner`
owns only the protected #711 functions/control objects; `at_migration_owner` owns the
ordinary application schemas and objects. The login that performs a role switch is not
itself an object owner.

### Extensions

The repository references `gen_random_uuid()` in multiple migrations/models and
`sha256(...)` in the #711 body-digest function
[alembic/versions/20260926_task711_nhplug_dispatch.py:L107-L116]. The static scan found
no tracked `CREATE EXTENSION` statement, but that does **not** mean extensions are
optional. The tracked TimescaleDB migration rejects a missing `timescaledb` extension
[alembic/versions/87541fdbc954_add_kr_candles_timescale.py:L35-L51], creates a
hypertable and continuous aggregate [alembic/versions/87541fdbc954_add_kr_candles_timescale.py:L79-L99],
and registers policy/refresh behavior [alembic/versions/87541fdbc954_add_kr_candles_timescale.py:L139-L178].
Other tracked revisions likewise use hypertables, continuous aggregates, and retention
policies [alembic/versions/e5df7fbd9803_add_crypto_candles_1m.py:L89-L100,
alembic/versions/e7a5b7c9d1f2_add_us_candles_timescale.py:L200-L278,
alembic/versions/d31f0a2b4c6d_add_kr_candles_retention_policy.py:L21-L65]. A database
that has successfully passed the mandatory migration is therefore expected to have
TimescaleDB; production catalog evidence remains authoritative for version, owners,
installed jobs, and managed-service restrictions.

Extensions remain DBA/platform-owned. Do not reassign extension members, grant the
application `CREATE EXTENSION`, or try to make the application role own an extension to
fix a migration failure. If a future migration needs an extension, split its approved
DBA prerequisite from ordinary application DDL and record the exact supported version.

#### TimescaleDB ownership and background-job boundary

Timescale objects must not be treated as ordinary relations selected only by
`pg_class.relkind`. Continuous aggregates can appear as `relkind = 'v'` in the tracked
migration [alembic/versions/20260804_toss_phase2_corpus.py:L42-L45]; their materialization
hypertables, chunks, and policy jobs have additional extension-managed ownership
relationships. Before ownership or credential retirement, the operator must inventory
the hypertable, continuous aggregate, materialization hypertable, chunk, and
Timescale-policy-job owners. The approved owner transition uses the supported
version-specific Timescale/PostgreSQL commands: `ALTER TABLE` for a hypertable and
`ALTER MATERIALIZED VIEW` for a continuous aggregate, never a relkind-driven generic
`ALTER VIEW`. The mechanism to reassign a policy-job owner is a version/platform fact
that the DBA must record and prove after the move; this design does not guess or run it.

Do not retire an old login while it owns a Timescale job, chunk, continuous aggregate,
or materialization hypertable. The staging proof for a future Phase 2 role transition
must include one continuous-aggregate refresh and one retention-policy job under the
proposed owner. These are existing extension jobs, not authorization to create or
schedule another job.

### Tests and throwaway databases

Keep the test administrator and run-owned DB behavior unchanged. Integration tests
need database-creation/drop authority by design; production application and migration
roles do not acquire that authority merely to satisfy a test. A post-cutover test plan
may add a separate disposable role/cluster fixture, but that is a future test change
and is outside this document.

## Operator-desk cutover

This is a runbook design, not a production command authorization. It assumes an
approved maintenance window, an operator with the required DBA/platform authority, and
an explicit production facts record. No step below may be executed from this PR.

### 0. Freeze, backup, and a written go/no-go record

1. Freeze unrelated schema/credential changes and identify the accountable operator.
2. Take a verified, restorable database backup **before** role or ownership mutation;
   separately preserve the global roles/ACL record if the platform supports it. Record
   backup identifier, completion time, restore-test result, database name, and expected
   recovery objective without placing credentials in source control.
3. Capture the exact image digests and service inventory before rotation. The tracked
   deployment script shows blue/green API and MCP behavior but singleton worker,
   scheduler, and websocket units [scripts/deploy-ncp-pull.sh:L130-L170]; confirm the
   live inventory rather than treating the template as fact. Include every installed
   unit from [Tracked NCP systemd consumers and backup client](#tracked-ncp-systemd-consumers-and-backup-client),
   its credential-input name, its login, and its longest existing timer cadence.
4. Stop new manual production CLIs and identify queued/background work that must not
   execute under an old role during the transition. Coordinate an existing host-unit
   pause/drain with the responsible operator where needed; do not add, register, or
   retime a scheduler as part of this activity.

### 1. Read-only preflight SQL

Run the following as an approved DBA or catalog-reader against the intended production
database. It is read-only and deliberately contains no password, hostname, or secret.
Save its result in the operator change record.

```sql
BEGIN READ ONLY;

-- Identity, server, migration state, and extension inventory.
SELECT current_database() AS database_name,
       current_user AS current_role,
       session_user AS session_role,
       current_setting('server_version') AS server_version;
SELECT to_regclass('alembic_version') AS alembic_version_relation;
SELECT extname, extversion, pg_get_userbyid(extowner) AS owner
FROM pg_extension
ORDER BY extname;

-- Existing and proposed roles plus all direct memberships.
SELECT rolname, rolsuper, rolinherit, rolcreaterole, rolcreatedb,
       rolcanlogin, rolreplication, rolbypassrls, rolconnlimit
FROM pg_roles
WHERE rolname IN ('at_migration_owner', 'at_app', 'nhplug_security_owner',
                  'nhplug_operator', 'at_reporting')
   OR left(rolname, 3) = 'at_'
ORDER BY rolname;
-- Record every login/superuser candidate before selecting the legacy credential
-- in the operator change record. Do not include password data.
SELECT rolname, rolsuper, rolcanlogin, rolinherit, rolcreaterole,
       rolbypassrls, rolreplication
FROM pg_roles
WHERE rolcanlogin OR rolsuper
ORDER BY rolname;
SELECT parent.rolname AS granted_role, member.rolname AS member_role,
       m.admin_option
FROM pg_auth_members AS m
JOIN pg_roles AS parent ON parent.oid = m.roleid
JOIN pg_roles AS member ON member.oid = m.member
ORDER BY parent.rolname, member.rolname;
SELECT r.rolname, r.rolcanlogin, r.rolinherit, r.rolsuper,
       r.rolcreaterole, r.rolbypassrls
FROM pg_roles AS r
JOIN pg_auth_members AS m ON m.member = r.oid
JOIN pg_roles AS parent ON parent.oid = m.roleid
WHERE parent.rolname IN ('at_app', 'at_migration_owner', 'nhplug_operator')
ORDER BY parent.rolname, r.rolname;

-- Schemas, table-like objects, sequence ownership/ACLs, and default ACLs.
SELECT n.nspname, pg_get_userbyid(n.nspowner) AS owner, n.nspacl
FROM pg_namespace AS n
WHERE n.nspname IN ('public', 'review', 'research')
ORDER BY n.nspname;
SELECT n.nspname, c.relname, c.relkind,
       pg_get_userbyid(c.relowner) AS owner, c.relacl,
       c.relrowsecurity, c.relforcerowsecurity
FROM pg_class AS c
JOIN pg_namespace AS n ON n.oid = c.relnamespace
WHERE n.nspname IN ('public', 'review', 'research')
  AND c.relkind IN ('r', 'p', 'v', 'm', 'S')
ORDER BY n.nspname, c.relkind, c.relname;
SELECT pg_get_userbyid(d.defaclrole) AS creator,
       COALESCE(n.nspname, '<database>') AS schema_name,
       d.defaclobjtype, d.defaclacl
FROM pg_default_acl AS d
LEFT JOIN pg_namespace AS n ON n.oid = d.defaclnamespace
ORDER BY creator, schema_name, d.defaclobjtype;

-- #711 and ROB-1340 protected objects, their identity sequences,
-- function owners/config/ACLs, and any PUBLIC execute inherited from a NULL/default proacl.
SELECT to_regclass('review.nhplug_mock_order_ledger') AS ledger,
       to_regclass('review.nhplug_mock_operator_authorization') AS authorization,
       pg_get_serial_sequence('review.nhplug_mock_order_ledger', 'id') AS ledger_sequence,
       to_regclass('review.kiwoom_authority_attempts') AS authority_attempts,
       to_regclass('review.kiwoom_authority_cessation_receipts') AS authority_receipts,
       pg_get_serial_sequence('review.kiwoom_authority_attempts', 'id') AS authority_attempts_sequence,
       pg_get_serial_sequence('review.kiwoom_authority_cessation_receipts', 'id') AS authority_receipts_sequence,
       to_regprocedure('review.nhplug_consume_authorization(uuid,text,bigint,uuid,date,text,text,bigint)') AS consume_function,
       to_regprocedure('review.nhplug_order_guard()') AS guard_function;
SELECT n.nspname, p.proname, pg_get_function_identity_arguments(p.oid) AS identity_args,
       pg_get_userbyid(p.proowner) AS owner, p.prosecdef, p.proconfig, p.proacl,
       CASE WHEN acl.grantee = 0 THEN 'PUBLIC' ELSE acl.grantee::regrole::text END AS grantee,
       acl.privilege_type
FROM pg_proc AS p
JOIN pg_namespace AS n ON n.oid = p.pronamespace
LEFT JOIN LATERAL aclexplode(COALESCE(p.proacl, acldefault('f', p.proowner))) AS acl
  ON true
WHERE n.nspname = 'review'
  AND (left(p.proname, 7) = 'nhplug_'
       OR p.proname = 'reject_kiwoom_authority_evidence_mutation')
ORDER BY p.proname, identity_args, grantee, acl.privilege_type;
SELECT schemaname, tablename, policyname, permissive, roles, cmd, qual, with_check
FROM pg_policies
WHERE schemaname IN ('public', 'review', 'research')
ORDER BY schemaname, tablename, policyname;

-- Do not transfer extension-owned members as ordinary application objects.
SELECT e.extname, d.classid::regclass AS catalog, d.objid, d.objsubid
FROM pg_depend AS d
JOIN pg_extension AS e ON e.oid = d.refobjid
WHERE d.deptype = 'e'
ORDER BY e.extname, catalog, d.objid, d.objsubid;

-- Active clients prove whether an old credential/session still exists.
SELECT usename, application_name, client_addr, state,
       count(*) AS connections, min(backend_start) AS oldest_backend_start
FROM pg_stat_activity
WHERE datname = current_database() AND pid <> pg_backend_pid()
GROUP BY usename, application_name, client_addr, state
ORDER BY usename, application_name, client_addr, state;

ROLLBACK;
```

If the extension inventory above contains `timescaledb`, run this **separate**
read-only block. Keeping it separate means an unexpected absent extension does not
abort the generic role/ACL preflight. Its output is the mandatory ownership and
background-job manifest for the Timescale transition:

```sql
BEGIN READ ONLY;
SELECT extname, extversion, pg_get_userbyid(extowner) AS extension_owner
FROM pg_extension
WHERE extname = 'timescaledb';
SELECT job_id, proc_name, owner, hypertable_schema, hypertable_name, scheduled
FROM timescaledb_information.jobs
ORDER BY job_id;
SELECT hypertable_schema, hypertable_name, owner
FROM timescaledb_information.hypertables
ORDER BY hypertable_schema, hypertable_name;
SELECT view_schema, view_name, view_owner,
       materialization_hypertable_schema, materialization_hypertable_name
FROM timescaledb_information.continuous_aggregates
ORDER BY view_schema, view_name;
SELECT n.nspname AS schema_name, pg_get_userbyid(c.relowner) AS owner,
       count(*) AS relation_count
FROM pg_class AS c
JOIN pg_namespace AS n ON n.oid = c.relnamespace
WHERE n.nspname LIKE E'\\_timescaledb%' ESCAPE E'\\'
GROUP BY n.nspname, c.relowner
ORDER BY schema_name, owner;
ROLLBACK;
```

If the current database role cannot see a needed catalog field, stop rather than
substituting a guessed answer. Obtain the narrowest approved catalog-reader/DBA view.
If the `alembic_version_relation` result is non-null, the operator may run this separate
read-only query to record its revision; if it is null, record absence without causing
the generic preflight to fail:

```sql
BEGIN READ ONLY;
SELECT version_num FROM alembic_version;
ROLLBACK;
```

If #711 objects are absent, record that state; it changes the order of Phase 2 but does
not waive the #711 compatibility blocker.

### 2. Create group roles and apply reviewed grants

After a reviewed name-conflict check, the DBA may create **non-login group roles** with
the stated attributes. Login authentication material is provisioned separately by the
approved secret mechanism. The following illustrates the required hard attributes;
it is proposed mutation SQL, not a command to run now:

```sql
CREATE ROLE at_migration_owner NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE
  NOREPLICATION NOBYPASSRLS;
CREATE ROLE at_app NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE
  NOREPLICATION NOBYPASSRLS;
CREATE ROLE nhplug_security_owner NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE
  NOREPLICATION NOBYPASSRLS;
CREATE ROLE nhplug_operator NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE
  NOREPLICATION NOBYPASSRLS;
```

The platform's role-creation semantics and existing `nhplug_operator` identity must
be checked before this batch. In particular, #711 conditionally creates that role at
cluster scope [alembic/versions/20260926_task711_nhplug_dispatch.py:L45-L59]. Never
drop, replace, or silently repurpose an existing role with that name. If it exists,
its ownership and grants require the explicit reconciliation described above.

Once roles exist and an object manifest is signed off, execute database/schema/table/
sequence/function grants in a catalog transaction where PostgreSQL supports the chosen
commands. Separate that transactional catalog batch from secret-manager writes,
container rollout, connection draining, and client termination: those external steps
are not made atomic by SQL transaction boundaries. Preserve a matching inverse ACL and
ownership batch before beginning.

Every API, worker, scheduler, MCP, monitor, host-job, and fill-handoff application
login must inherit only `at_app`; there is no generic application startup `SET ROLE`
step. PostgreSQL 16 or later membership rows must record `inherit_option = true`,
`set_option = false`, and `admin_option = false`; earlier supported versions require
an inheriting login and no owner-role membership. This is an application-login
requirement, not a license to give any login membership in an owner role.

At minimum, the approved batch must:

1. grant `CONNECT` and only required schema `USAGE` to the group roles;
2. revoke unexpected `PUBLIC` schema `CREATE` and function `EXECUTE` only after
   catalog review and replacement grants exist;
3. apply the exact normal-table DML manifest to `at_app`, not a schema-wide future
   grant;
4. apply the #711 table matrix, sequence `USAGE`, and function policy above;
5. change object ownership one reviewed object at a time, excluding extension members;
   and
6. install/review default privileges for each actual object creator.

### 3. Staged credential rotation and deploy

Create the new login credentials before changing any old credential, then rotate every
identified process in this order:

1. **Migration runner:** prove it reaches the intended database with
   `session_user` equal to its runner login and `current_user` equal to
   `at_migration_owner`; run no new migration as part of this role-only change unless
   it is separately approved.
2. **API:** deploy a new API instance with its dedicated `at_app` login; perform only
   health/readiness and approved database verification before routing traffic.
3. **Workers and scheduler:** start a new worker with its own login, drain/stop the
   old worker, then restart the singleton scheduler. Account for queued tasks and do
   not let both identities continue executing unknown work indefinitely.
4. **Every MCP unit:** rotate the default/blue-green unit and each named profile unit.
   Long-lived MCP requests and the tracked long drain behavior require explicit
   connection confirmation before any old grant is revoked.
5. **Websocket/other monitors:** restart every deployed monitor that imports the
   shared factory; confirm its process command rather than relying on a source filename.
6. **Tracked host jobs and fill handoff:** rotate every installed application-job and
   fill-handoff unit, then record a successful run under its new application login.
   A timer that can still launch a process from an old credential input is an active
   cutover failure, even if it had no session during the last snapshot.
7. **Backup client:** inventory the separate backup identity and its database/cluster
   scope. Keep it outside `at_app`; rotate it only through a separately approved
   backup/DR procedure, never by copying an application credential.
8. **Approved operational CLIs:** inventory separately. Explicit-URL tools such as
   protected-position inspection, campaign execution, and dev-seed export must not be
   accidentally pointed at a privileged production identity.

The repository's tracked deploy script supports API/MCP blue-green routing, but worker,
scheduler, and monitors are singleton/restart paths [scripts/deploy-ncp-pull.sh:L130-L170].
Consequently this is not a true zero-downtime credential cutover. Old pools retain old
authenticated sessions; the scheduler/monitor restarts can have a gap; and long MCP
drains may overlap new traffic. Plan a maintenance window or explicitly accepted
overlap. Do not revoke old privileges until catalog evidence shows the old login has no
active sessions and the replacement process has passed its approved checks. Do not
retire the old credential until it is absent from every inventoried application input
and repeated client evidence covers at least the longest installed host timer (daily
when the tracked backup timer is installed).

### 4. Exact post-deploy verification SQL

Run as an approved catalog reader after every process rotation. Replace the explicit
login values only from the change record; do not paste a connection URL or secret into
the query log.

```sql
BEGIN READ ONLY;

-- Role hard attributes and membership must match the reviewed plan.
SELECT rolname, rolsuper, rolcreaterole, rolcreatedb, rolcanlogin,
       rolreplication, rolbypassrls
FROM pg_roles
WHERE rolname IN ('at_migration_owner', 'at_app',
                  'nhplug_security_owner', 'nhplug_operator')
ORDER BY rolname;
SELECT parent.rolname AS granted_role, member.rolname AS member_role,
       m.admin_option
FROM pg_auth_members AS m
JOIN pg_roles AS parent ON parent.oid = m.roleid
JOIN pg_roles AS member ON member.oid = m.member
WHERE parent.rolname IN ('at_migration_owner', 'at_app',
                         'nhplug_security_owner', 'nhplug_operator')
ORDER BY parent.rolname, member.rolname;
SELECT r.rolname, r.rolcanlogin, r.rolinherit, r.rolsuper,
       r.rolcreaterole, r.rolbypassrls
FROM pg_roles AS r
JOIN pg_auth_members AS m ON m.member = r.oid
JOIN pg_roles AS parent ON parent.oid = m.roleid
WHERE parent.rolname IN ('at_app', 'at_migration_owner', 'nhplug_operator')
ORDER BY parent.rolname, r.rolname;

-- PostgreSQL 16+ only: verify the membership semantics, not just the member
-- role's rolinherit default. Every at_app login must inherit but not SET/admin it.
SELECT parent.rolname AS granted_role, member.rolname AS member_role,
       m.admin_option, m.inherit_option, m.set_option
FROM pg_auth_members AS m
JOIN pg_roles AS parent ON parent.oid = m.roleid
JOIN pg_roles AS member ON member.oid = m.member
WHERE parent.rolname = 'at_app' AND member.rolcanlogin
ORDER BY member.rolname;

SELECT n.nspname,
       has_schema_privilege('at_app', n.oid, 'USAGE') AS app_usage,
       has_schema_privilege('at_app', n.oid, 'CREATE') AS app_create,
       has_schema_privilege('nhplug_operator', n.oid, 'USAGE') AS operator_usage,
       has_schema_privilege('nhplug_operator', n.oid, 'CREATE') AS operator_create
FROM pg_namespace AS n
WHERE n.nspname IN ('public', 'review', 'research')
ORDER BY n.nspname;

-- Object-level checks for the sensitive #711 and ROB-1340 splits.
SELECT c.oid::regclass AS object_name, pg_get_userbyid(c.relowner) AS owner,
       has_table_privilege('at_app', c.oid, 'SELECT') AS app_select,
       has_table_privilege('at_app', c.oid, 'INSERT') AS app_insert,
       has_table_privilege('at_app', c.oid, 'UPDATE') AS app_update,
       has_table_privilege('at_app', c.oid, 'DELETE') AS app_delete,
       has_table_privilege('at_app', c.oid, 'TRUNCATE') AS app_truncate,
       has_table_privilege('nhplug_operator', c.oid, 'SELECT') AS operator_select,
       has_table_privilege('nhplug_operator', c.oid, 'INSERT') AS operator_insert,
       has_table_privilege('nhplug_operator', c.oid, 'UPDATE') AS operator_update,
       has_table_privilege('nhplug_operator', c.oid, 'DELETE') AS operator_delete,
       has_table_privilege('nhplug_operator', c.oid, 'TRUNCATE') AS operator_truncate
FROM pg_class AS c
WHERE c.oid IN (
  'review.nhplug_mock_key_version'::regclass,
  'review.nhplug_success_proof_code'::regclass,
  'review.nhplug_no_order_proof_code'::regclass,
  'review.nhplug_mock_operator_authorization'::regclass,
  'review.nhplug_mock_account_ref'::regclass,
  'review.nhplug_mock_account_binding'::regclass,
  'review.nhplug_mock_order_ledger'::regclass,
  'review.kiwoom_authority_attempts'::regclass,
  'review.kiwoom_authority_cessation_receipts'::regclass
)
ORDER BY object_name;

-- Effective privileges for every login that inherits at_app. Reconcile this
-- list one-for-one with the signed deployed-unit inventory; an unmapped login
-- or a deployed unit absent from this result fails the cutover.
WITH app_login AS (
  SELECT member.rolname AS login_name
  FROM pg_auth_members AS m
  JOIN pg_roles AS parent ON parent.oid = m.roleid
  JOIN pg_roles AS member ON member.oid = m.member
  WHERE parent.rolname = 'at_app' AND member.rolcanlogin
), protected_object AS (
  SELECT c.oid, c.oid::regclass AS object_name
  FROM pg_class AS c
  WHERE c.oid IN (
    'review.nhplug_mock_key_version'::regclass,
    'review.nhplug_success_proof_code'::regclass,
    'review.nhplug_no_order_proof_code'::regclass,
    'review.nhplug_mock_operator_authorization'::regclass,
    'review.nhplug_mock_account_ref'::regclass,
    'review.nhplug_mock_account_binding'::regclass,
    'review.nhplug_mock_order_ledger'::regclass,
    'review.kiwoom_authority_attempts'::regclass,
    'review.kiwoom_authority_cessation_receipts'::regclass
  )
)
SELECT l.login_name, o.object_name,
       has_table_privilege(l.login_name, o.oid, 'SELECT') AS can_select,
       has_table_privilege(l.login_name, o.oid, 'INSERT') AS can_insert,
       has_table_privilege(l.login_name, o.oid, 'UPDATE') AS can_update,
       has_table_privilege(l.login_name, o.oid, 'DELETE') AS can_delete,
       has_table_privilege(l.login_name, o.oid, 'TRUNCATE') AS can_truncate
FROM app_login AS l
CROSS JOIN protected_object AS o
ORDER BY l.login_name, o.object_name;

SELECT s.oid::regclass AS sequence_name,
       has_sequence_privilege('at_app', s.oid, 'USAGE') AS app_usage,
       has_sequence_privilege('at_app', s.oid, 'SELECT') AS app_select,
       has_sequence_privilege('at_app', s.oid, 'UPDATE') AS app_update
FROM pg_class AS s
WHERE s.oid IN (
  pg_get_serial_sequence('review.nhplug_mock_order_ledger', 'id')::regclass,
  pg_get_serial_sequence('review.kiwoom_authority_attempts', 'id')::regclass,
  pg_get_serial_sequence('review.kiwoom_authority_cessation_receipts', 'id')::regclass
)
ORDER BY sequence_name;

SELECT p.oid::regprocedure AS function_name,
       pg_get_userbyid(p.proowner) AS owner, p.prosecdef, p.proconfig,
       has_function_privilege('at_app', p.oid, 'EXECUTE') AS app_execute,
       EXISTS (
         SELECT 1
         FROM aclexplode(COALESCE(p.proacl, acldefault('f', p.proowner))) AS acl
         WHERE acl.grantee = 0 AND acl.privilege_type = 'EXECUTE'
       ) AS public_execute
FROM pg_proc AS p
WHERE p.oid IN (
  'review.nhplug_consume_authorization(uuid,text,bigint,uuid,date,text,text,bigint)'::regprocedure,
  'review.nhplug_order_guard()'::regprocedure,
  'review.reject_kiwoom_authority_evidence_mutation()'::regprocedure
)
ORDER BY function_name;

-- Each listed process should now show its new, expected login; no old role may remain.
SELECT usename, application_name, client_addr, state, count(*) AS connections
FROM pg_stat_activity
WHERE datname = current_database() AND pid <> pg_backend_pid()
GROUP BY usename, application_name, client_addr, state
ORDER BY usename, application_name, client_addr, state;

-- Replace only this literal with the legacy application login recorded during
-- preflight. Its final state must be NOLOGIN and NOSUPERUSER before completion.
SELECT rolname, rolcanlogin, rolsuper, rolinherit, rolcreaterole,
       rolbypassrls, rolreplication
FROM pg_roles
WHERE rolname = '<legacy_application_login_from_change_record>';

ROLLBACK;
```

Treat any unexpected `app_execute`, `public_execute`, operator update/delete, owner,
role attribute, old client, or absent required application privilege as a failed
cutover. The static check is not enough: run the #711 authorization-consumption path
only in an approved staging clone with rollback-safe data, because a production insert
can advance a sequence even if its surrounding transaction rolls back.

For each deployed login, compare the effective protected-object result with the
required #711/ROB-1340 matrix above, and run the same effective-login query for one
named normal table from that deployment class's signed DML manifest. A group-role
success alone does not prove an `INHERIT` login works. On PostgreSQL 16 or later, a
false `inherit_option`, true `set_option`, or true `admin_option` for an `at_app`
login fails verification even if a group-role ACL check succeeds.

### 4a. Legacy-credential retirement evidence

After every installed application consumer has successfully run under its new login,
remove the legacy application credential from every inventoried application input and
prevent fresh authentication. The approved operator chooses the safe mechanism from
the preflight record: rotate/revoke the credential in the secret system, or set the
specific legacy login `NOLOGIN NOSUPERUSER` if it is not shared by another approved
consumer. Never apply a blanket role change, and do not retire a Timescale owner or
backup identity under this step.

This is deliberately later than new-service deploy and earlier than completion. Repeat
the active-client query after the credential-retirement action for an observation
window at least as long as the longest installed host timer (daily when the backup
timer is present). The cutover is incomplete if a timer can still launch an old-login
process, even if one point-in-time session query was empty.

### 5. Drain and rollback

If verification fails before old access is revoked:

1. stop routing new work to the new process, retain the backup/change record, and
   restore the prior service image/secret mapping under the existing deployment
   procedure;
2. drain or terminate only the specifically inventoried new-role sessions after DBA
   review; do not use an unqualified client-kill command;
3. apply the prewritten inverse ownership/ACL batch if and only if its catalog targets
   still match the recorded preflight; and
4. restart the prior services and verify their role/session inventory before reopening
   work.

If old permissions have already been revoked, do not treat a service redeploy as a SQL
rollback. Restore old grants/ownership first from the captured catalog manifest, rotate
the old credential only through the approved secret workflow, then redeploy and drain.
Use the verified backup for data recovery only when data integrity requires it; ACL
rollback and backup restore solve different problems.

An old superuser session is not made safe by revoking ordinary grants: it still bypasses
ordinary ACLs. Identify and end/re-authenticate old privileged sessions under a DBA
maintenance procedure before declaring the cutover complete. The role split is complete
only after the legacy credential is retired, the observation window is clean, and no
old application/migration/superuser session can continue the old access pattern.

## Risks requiring explicit assessment

| Risk | Why it matters | Required response |
| --- | --- | --- |
| Missing normal-table grant | API, task, MCP, monitor, or manual CLI fails after a new connection is opened. | Build/review an object-level DML and sequence manifest from source plus production catalog; stage it before revocation. |
| Existing ownership | An owner bypasses ordinary ACL intent; historical migration runners may own arbitrary objects. | Inventory owners; prohibit blanket reassignment; move named objects only. |
| #711 historical ACLs | The migration grants reads to `current_user` and makes `nhplug_operator` an owner/update role. | Resolve the compatibility blocker with a forward approved transition before activating the split. |
| ROB-1340 authority evidence | Runtime readiness demands SELECT/INSERT and rejects any effective UPDATE/DELETE/TRUNCATE on two append-only evidence tables. | Keep the explicit exception and identity-sequence grants outside a broad review-schema manifest. |
| Extension objects | Reassignment or missing function/extension privilege can break migrations or generated defaults. | Inspect `pg_extension` and extension-member dependencies; retain DBA ownership. For TimescaleDB, inventory hypertables, continuous aggregates, chunks, materialization tables, and policy-job owners before any owner or credential transition. |
| PUBLIC function execute | #711 security-definer functions may be publicly executable by default. | Inspect `proacl`/effective ACL, then revoke and selectively grant in an approved transactional change. |
| RLS / SECURITY DEFINER | Table owner and definer behavior can bypass intended policies; a path change can introduce function hijack risk. | Inspect policies, `prosecdef`, `proconfig`, owner attributes, and schema-create ACLs; do not change function body/search path casually. |
| Background work | Old TaskIQ worker/scheduler, monitors, MCP requests, pools, host timers, and fill handoff can retain or later re-open an old credential. | Inventory all active clients and installed units, rotate/drain every unit, retire the old credential, then observe for at least the longest timer cadence. |
| Legacy privileged credential | A superuser or bypassing login ignores ordinary ACL revocation and can return after a timer fires. | Remove it from every application input, retire it with an approved credential/role action, and prove no old sessions during the observation window. |
| TimescaleDB jobs | Retention/refresh policy jobs and extension-owned object graphs can keep an old owner alive or fail after a generic ownership change. | Use the Timescale catalog and supported owner-transition method, then prove a refresh and retention job under the approved owner before legacy retirement. |
| Zero-downtime assumption | The repository shows blue/green only for some units; credentials and sessions are not atomically switched. | Use a maintenance window or documented overlap, with old grants retained until all old sessions are gone. |
| Test-role confusion | Test fixtures intentionally create/drop isolated databases. | Keep a separate test administrator and run-owned DB behavior; do not weaken production roles. |

## Open operator questions

The recommended default is shown with each question. Each query is read-only and must
be run against the intended production database by an approved operator.

1. **Which database, PostgreSQL version, and deployed units are actually in scope?**
   Default: include every unit in the repository inventory until the live service list
   proves otherwise, including installed host-job, fill-handoff, and backup timers.

   ```sql
   BEGIN READ ONLY;
   SELECT current_database(), current_setting('server_version'), current_user, session_user;
   SELECT usename, application_name, client_addr, state, count(*)
   FROM pg_stat_activity
   WHERE datname = current_database()
   GROUP BY usename, application_name, client_addr, state
   ORDER BY usename, application_name, client_addr, state;
   ROLLBACK;
   ```

   The same approved operator should obtain the host-unit inventory without opening
   a credential file:

   ```text
   systemctl list-timers --all
   systemctl list-units --all 'job-*' 'fill-event-handoff*' 'at-pg-backup*' 'b0x-*'
   ```

2. **Are any target role names already present, and who owns/members them?**
   Default: preserve existing roles until ownership and membership are reviewed; do not
   rename or drop `nhplug_operator` opportunistically.

   ```sql
   BEGIN READ ONLY;
   SELECT rolname, rolsuper, rolcanlogin, rolcreaterole, rolbypassrls
   FROM pg_roles
   WHERE rolname IN ('at_migration_owner', 'at_app',
                     'nhplug_security_owner', 'nhplug_operator', 'at_reporting')
   ORDER BY rolname;
   SELECT parent.rolname, member.rolname, m.admin_option
   FROM pg_auth_members AS m
   JOIN pg_roles AS parent ON parent.oid = m.roleid
   JOIN pg_roles AS member ON member.oid = m.member
   ORDER BY parent.rolname, member.rolname;
   ROLLBACK;
   ```

3. **Have #711 and ROB-1340 been applied, and what are their live owners/ACLs/function
   properties?** Default: treat either protected surface as not-safe-to-cut-over until
   its explicit ACL/ownership transition is approved, whether it is already applied or
   pending.

   ```sql
   BEGIN READ ONLY;
   SELECT to_regclass('alembic_version') AS alembic_version_relation;
   SELECT c.oid::regclass, pg_get_userbyid(c.relowner), c.relacl
   FROM pg_class AS c
   WHERE c.oid IN (
     to_regclass('review.nhplug_mock_key_version'),
     to_regclass('review.nhplug_mock_operator_authorization'),
     to_regclass('review.nhplug_mock_order_ledger'),
     to_regclass('review.kiwoom_authority_attempts'),
     to_regclass('review.kiwoom_authority_cessation_receipts')
   )
   ORDER BY c.oid::regclass::text;
   SELECT p.oid::regprocedure, pg_get_userbyid(p.proowner), p.prosecdef, p.proconfig, p.proacl
   FROM pg_proc AS p
   WHERE p.oid IN (
     to_regprocedure('review.nhplug_consume_authorization(uuid,text,bigint,uuid,date,text,text,bigint)'),
     to_regprocedure('review.nhplug_order_guard()'),
     to_regprocedure('review.reject_kiwoom_authority_evidence_mutation()')
   )
   ORDER BY p.oid::regprocedure::text;
   ROLLBACK;
   ```

4. **What exact normal tables, views, sequences, and functions does each deployment
   class require?** Default: grant only a reviewed object-level manifest; retain one
   shared `at_app` group initially rather than guessing an API/worker/MCP split.

   ```sql
   BEGIN READ ONLY;
   SELECT n.nspname, c.relname, c.relkind, pg_get_userbyid(c.relowner), c.relacl
   FROM pg_class AS c
   JOIN pg_namespace AS n ON n.oid = c.relnamespace
   WHERE n.nspname IN ('public', 'review', 'research')
     AND c.relkind IN ('r', 'p', 'v', 'm', 'S')
   ORDER BY n.nspname, c.relkind, c.relname;
   SELECT n.nspname, p.oid::regprocedure, pg_get_userbyid(p.proowner), p.prosecdef, p.proacl
   FROM pg_proc AS p
   JOIN pg_namespace AS n ON n.oid = p.pronamespace
   WHERE n.nspname IN ('public', 'review', 'research')
   ORDER BY n.nspname, p.oid::regprocedure::text;
   ROLLBACK;
   ```

5. **Which extensions and RLS policies exist, and who owns their members and
   TimescaleDB jobs?** Default: platform/DBA retains extensions; defer RLS changes
   entirely, and do not retire an old login until Timescale ownership is resolved.

   ```sql
   BEGIN READ ONLY;
   SELECT extname, extversion, pg_get_userbyid(extowner) FROM pg_extension ORDER BY extname;
   SELECT schemaname, tablename, policyname, roles, cmd
   FROM pg_policies
   WHERE schemaname IN ('public', 'review', 'research')
   ORDER BY schemaname, tablename, policyname;
   SELECT n.nspname, c.relname, c.relrowsecurity, c.relforcerowsecurity
   FROM pg_class AS c JOIN pg_namespace AS n ON n.oid = c.relnamespace
   WHERE n.nspname IN ('public', 'review', 'research')
     AND (c.relrowsecurity OR c.relforcerowsecurity)
   ORDER BY n.nspname, c.relname;
   ROLLBACK;
   ```

   If the first query returns `timescaledb`, run this separate read-only query:

   ```sql
   BEGIN READ ONLY;
   SELECT job_id, proc_name, owner, hypertable_schema, hypertable_name, scheduled
   FROM timescaledb_information.jobs
   ORDER BY job_id;
   SELECT hypertable_schema, hypertable_name, owner
   FROM timescaledb_information.hypertables
   ORDER BY hypertable_schema, hypertable_name;
   SELECT view_schema, view_name, view_owner,
          materialization_hypertable_schema, materialization_hypertable_name
   FROM timescaledb_information.continuous_aggregates
   ORDER BY view_schema, view_name;
   ROLLBACK;
   ```

6. **Is a reporting role actually justified?** Default: do not create one. The source
   contains no separately configured reporting process; introduce it only with a named
   consumer, a read-only view/table manifest, connection identity, retention policy,
   and an operator-approved deployment path.

7. **What is the acceptable outage/overlap, old-credential retirement, and
   client-drain policy?** Default: use a maintenance window, retain old grants until
   new clients are verified, then remove the legacy application credential from every
   inventoried application input and observe a clean interval at least as long as the
   longest installed timer. The template's API/MCP drain timers do not establish
   database-session drain completion.

   ```sql
   BEGIN READ ONLY;
   SELECT rolname, rolcanlogin, rolsuper, rolinherit, rolcreaterole,
          rolbypassrls, rolreplication
   FROM pg_roles
   WHERE rolname = '<legacy_application_login_from_change_record>';
   SELECT usename, application_name, client_addr, state, count(*)
   FROM pg_stat_activity
   WHERE datname = current_database()
     AND pid <> pg_backend_pid()
   GROUP BY usename, application_name, client_addr, state
   ORDER BY usename, application_name, client_addr, state;
   ROLLBACK;
   ```

## MCP lane-contract assessment

There is no lane-contract conflict in this design. It changes no MCP tool,
registration, profile, allowlist, or `tool<TAB>basis` file. The promoted manifests
remain the eleven audited lane files in `config/mcp_lane_allowlists/`, and their
contract test verifies exact bytes/counts plus profile availability
[tests/mcp_server/test_lane_allowlist_contract.py:L1-L113]. The profile snapshot test
separately freezes actual registrations [tests/mcp_server/test_profile_tool_snapshot.py:L1-L46].
The role proposal does not override either contract: an MCP process remains an
application-role consumer until a separately reviewed, explicitly configured
read-only reporting process exists.

## Implementation boundary

The only repository artifact in this Phase 1 change is this design document. No
production activation, NHPLUG dispatch enablement, broker call, schema migration,
scheduler registration, runtime-code change, environment-file read, or credential
change is included. Phase 2 must be proposed and approved independently after the
open questions, #711 compatibility blocker, staging proof, and operator change plan
are resolved.
