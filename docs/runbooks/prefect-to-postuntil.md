# Prefect → postuntil migration map (first pass)

This is the 2026-09-04 inventory from
`/Users/mgh3326/work/herdr-inbox/jobs/pu-migrate-1-20260904-0330/prefect-deployments.txt`
(54 deployments). It is a migration map, not a cutover: no Prefect deployment
was paused, deleted, or changed, and no NCP unit was installed.

## Result: no eligible kick timer in this pass

`app/services/ops_task_kick/registry.py:94-112` has 13 static kick entries, but
no inventory flow invokes one. The seemingly related flows invoke a local CLI
or a `robin_automation` wrapper instead:

- US screener: `invest_screener_snapshots_us.py:88-129` executes
  `scripts.build_invest_screener_snapshots`; its deployment uses
  `dry_run`/`timeout_seconds` at `192-201`, neither of which is an API body
  accepted by the corresponding kick contract.
- KR investor flow: `investor_flow_snapshots.py:75-115` executes
  `scripts.build_investor_flow_snapshots`; its deployment is at `177-186`.
- US valuation: `market_valuation_snapshots_us.py:103-150` executes
  `scripts.build_market_valuation_snapshots`, rather than the TaskIQ task.
- Toss warnings: `toss_warnings_sync.py:54-76` executes
  `scripts.sync_toss_warnings`; its deployment body is
  `{"timeout_seconds": 3600}` at `115-117`, while `warnings.toss.sync` takes
  no arguments (`app/tasks/toss_warnings_sync_tasks.py:15-18`).

Changing those request shapes, adding an adapter, or silently dropping fields
would violate the requirement that a postuntil body be byte-identical to the
Prefect deployment parameters. Therefore **A = 0 and TIMERS = 0**. In
particular, this pass does not manufacture a `commit`/`dry_run` conversion or
weaken the KIS safety gate.

The inventory output truncates long `params=` values, so source declarations,
not rendered CLI columns, are the authority for the line citations below.

## Classification and order

Legend: **A** = direct static kick entry (this pass); **B** = auto_trader
script/CLI or other process, next batch with a dedicated service; **C** =
session kickoff/nudge, panehitch work; **D** = paused/unused or sealed/operator
decision; **E** = external flow source not present under the permitted
`~/services/prefect/flows/` tree, confirm owner before any move.

| # | work pool / deployment | Prefect cron | source evidence | class | next action |
| --- | --- | --- | --- | --- | --- |
| 1 | ncp-process / daily | — | `market_events_ingestion.py:133-168` → `run_market_events_ingest` | B | systemd script batch |
| 2 | ncp-process / daily-freshness | `10 18 * * 1-5` KST (`investor_flow_snapshots.py:177`) | `investor_flow_snapshots.py:75-115` → `scripts.build_investor_flow_snapshots` | B | script batch |
| 3 | ncp-process / daily-kst (crypto screener) | `20 9 * * *` KST | `invest_crypto_screener_snapshots.py:92-129` → `scripts.build_invest_crypto_screener_snapshots` | B | script batch |
| 4 | ncp-process / daily-kst (crypto insight) | `20 9 * * *` KST (`invest_crypto_insight_snapshots.py:251`) | `invest_crypto_insight_snapshots.py:176-194` local command | B | script batch |
| 5 | ncp-process / daily-kst (KR fundamentals) | `0 18 * * *` KST (`invest_kr_fundamentals_snapshots.py:212`) | `invest_kr_fundamentals_snapshots.py:121-152` local command | B | script batch |
| 6 | ncp-process / daily-post-us-close | `30 8 * * 2-6` KST (`market_valuation_snapshots_us.py:219`) | `market_valuation_snapshots_us.py:103-150` → `scripts.build_market_valuation_snapshots` | B | script batch |
| 7 | ncp-process / daily-preopen | `30 7 * * *` KST (`toss_warnings_sync.py:115`) | `toss_warnings_sync.py:54-76` → `scripts.sync_toss_warnings` | B | script batch |
| 8 | ncp-process / every-10m-kst | `*/10 9-16 * * 1-5` KST (`kis_live_reconcile.py:21-22`) | `kis_live_reconcile.py:25-34` → operator reconcile | D | operator decision; never migrate in this pass |
| 9 | ncp-process / manual-dry-run | —, paused | `market_events_ingestion.py:194-207` | D | retired candidate |
| 10 | ncp-process / post-us-close-freshness | `10 6 * * 2-6` KST (`invest_screener_snapshots_us.py:192`) | `invest_screener_snapshots_us.py:88-129` → `scripts.build_invest_screener_snapshots` | B | script batch |
| 11 | ncp-process / weekday-preopen | `20 7 * * 1-5` KST | `toss_symbol_master_sync.py:30-32` → `run_toss_symbol_master_sync` | B | script batch |
| 12 | ncp-process / weekly-sunday | `0 9 * * 0` KST (`us_fundamentals_snapshots.py:200`) | `us_fundamentals_snapshots.py:102-138` → `scripts.build_us_fundamentals_snapshots` | B | script batch |
| 13 | pyri-process / crypto-every-15m | —, paused | `news_ingestion.py:68-87` | D | retired candidate |
| 14 | pyri-process / every-10-minutes (server mode) | — | `health/macbook_server_mode.py:67-74` → `collect_server_mode_status` | B | external-process batch |
| 15 | pyri-process / every-10-minutes (health smoke) | — | `health/macbook_health_smoke_test.py:64-76` → `collect_macbook_health` | B | external-process batch |
| 16 | pyri-process / every-10-minutes (AOE) | — | inventory `prefect-deployments.txt:16`; `flows/aoe/...` absent from permitted tree | E | confirm owner |
| 17 | pyri-process / every-15-minutes | — | `result_freshness_monitor.py:23-75` | B | external-process batch |
| 18 | pyri-process / every-15m (news) | —, paused | `news_ingestion.py:68-87` | D | retired candidate |
| 19 | pyri-process / every-5-min (Binance demo) | — | `binance_demo_scalping.py:38-52` → `run_demo_scalping_tick` | B | separate operator safety review |
| 20 | pyri-process / every-5-minutes (runtime monitor) | — | `auto_trader_runtime_monitor.py:22-41` | B | external-process batch |
| 21 | pyri-process / every-5-minutes (Prefect watchdog) | — | `health/prefect_error_watchdog.py:24-94` | B | external-process batch |
| 22 | pyri-process / every-5-minutes (Kanban monitor) | — | `kanban/kanban_agent_monitor.py:23-86` | B | external-process batch |
| 23 | pyri-process / every-6-hours (Hermes) | — | inventory `prefect-deployments.txt:23`; `flows/hermes/...` absent from permitted tree | E | confirm owner |
| 24 | pyri-process / every-hour (server mode) | — | `health/macbook_server_mode.py:67-74` → `collect_server_mode_status` | B | external-process batch |
| 25 | pyri-process / hourly (US news) | —, paused | `news_ingestion.py:40-49` | D | retired candidate |
| 26 | pyri-process / hourly (crypto news) | —, paused | `news_ingestion.py:54-63` | D | retired candidate |
| 27 | pyri-process / hourly (honcho watchdog) | — | `health/honcho_health_watchdog.py:24-88` | B | external-process batch |
| 28 | pyri-process / hourly (KR news) | —, paused | `news_ingestion.py:30-37` | D | retired candidate |
| 29 | pyri-process / kr-every-15m | —, paused | `news_ingestion.py:68-87` | D | retired candidate |
| 30 | pyri-process / manual (health smoke) | — | `health/macbook_health_smoke_test.py:64-76` → `collect_macbook_health` | B | manual process batch |
| 31 | pyri-process / manual (enter server mode) | — | `health/macbook_server_mode.py:56-64` → `enter_server_mode_actions` | B | manual process batch |
| 32 | pyri-process / manual (watch alert) | —, paused | `watch_alert_router.py:18-31` | D | retired candidate |
| 33 | pyri-process / manual (news) | —, paused | `news_ingestion.py:68-87` | D | retired candidate |
| 34 | pyri-process / manual (OpenHAB) | — | inventory `prefect-deployments.txt:34`; `flows/smarthome/...` absent from permitted tree | E | confirm owner |
| 35 | pyri-process / manual-dry-run (Binance demo) | —, paused | `binance_demo_scalping.py:38-52` → `run_demo_scalping_tick` | D | retired candidate |
| 36 | pyri-process / manual-smoke (KR kickoff) | —, paused | `kr_live_session_kickoff.py:42-76` | C | panehitch scope |
| 37 | pyri-process / nudge-crypto-4h | `0 1,5,9,13,17,21 * * *` KST | `b0x_time_triggers.py:69-76` → nudge | C | panehitch scope |
| 38 | pyri-process / nudge-harvest | `13,43 * * * *` KST | `b0x_time_triggers.py:78-84` → harvest nudge | C | panehitch scope |
| 39 | pyri-process / nudge-kr-0905 | `5 9 * * 1-5` KST | `b0x_time_triggers.py:69-76` → nudge | C | panehitch scope |
| 40 | pyri-process / nudge-us-2235 | `35 22 * * 1-5` KST | `b0x_time_triggers.py:69-76` → nudge | C | panehitch scope |
| 41 | pyri-process / table-kr | `45 7 * * 1-5` KST | `b0x_time_triggers.py:54-66` → table build | D | sealed; C1–C6 first |
| 42 | pyri-process / table-us | `0 22 * * 1-5` KST | `b0x_time_triggers.py:54-66` → table build | D | sealed; C1–C6 first |
| 43 | pyri-process / us-every-15m | —, paused | `news_ingestion.py:68-87` | D | retired candidate |
| 44 | pyri-process / weekday-0905 | `5 9 * * 1-5` KST | `kr_live_session_kickoff.py:42-76` | C | panehitch scope |
| 45 | pyri-process / weekday-1130 | `30 11 * * 1-5` KST | `kr_live_session_kickoff.py:42-76` | C | panehitch scope |
| 46 | pyri-process / weekday-1430 | `30 14 * * 1-5` KST | `kr_live_session_kickoff.py:42-76` | C | panehitch scope |
| 47 | pyri-process / weekday-crypto-0220 | `20 2 * * *` KST | `kr_live_session_kickoff.py:42-76` | C | panehitch scope |
| 48 | pyri-process / weekday-crypto-0820 | `20 8 * * *` KST | `kr_live_session_kickoff.py:42-76` | C | panehitch scope |
| 49 | pyri-process / weekday-crypto-1420 | `20 14 * * *` KST | `kr_live_session_kickoff.py:42-76` | C | panehitch scope |
| 50 | pyri-process / weekday-crypto-2020 | `20 20 * * *` KST | `kr_live_session_kickoff.py:42-76` | C | panehitch scope |
| 51 | pyri-process / weekday-nxt-eve | `50 15 * * 1-5` KST | `kr_live_session_kickoff.py:42-76` | C | panehitch scope |
| 52 | pyri-process / weekday-nxt-open | `55 7 * * 1-5` KST | `kr_live_session_kickoff.py:42-76` | C | panehitch scope |
| 53 | pyri-process / weekday-nxt-prep | `15 7 * * 1-5` KST | `kr_live_session_kickoff.py:42-76` | C | panehitch scope |
| 54 | pyri-process / weekday-us-2235 | `35 22 * * 1-5` KST | `kr_live_session_kickoff.py:42-76` | C | panehitch scope |

Totals: **A=0, B=21, C=16, D=14, E=3, TIMERS=0**.

## Future A-job format and validation

When a future flow calls a static kick entry and its body is byte-identical to
the deployment parameters, add all three files together:

- `ops/ncp/postuntil/<task>.toml`, including `post`, `body`,
  `header_env.X-Ops-Task-Token = "OPS_TASK_KICK_TOKEN"`, `id_path =
  ".task_id"`, `poll = ".../runs/{{.id}}"`, `until = ".state=done"`, and
  `fail_when = ".state=error"`. The exact states are defined by
  `docs/runbooks/ops-task-kick.md:43-47`; `unknown` is never success.
- `ops/ncp/systemd/kick-<task>.service`, with
  `EnvironmentFile=/root/at-secrets/.env.postuntil` and the requested
  `/usr/local/bin/postuntil run -f /root/at-run/ops/ncp/postuntil/<task>.toml`.
- The paired `.timer`, in `Asia/Seoul`; retain `# PrefectCron:` and
  `# PrefectParameters:` comments in the TOML so
  `scripts/postuntil_timers_check.py` can prove the first five occurrences and
  exact body identity. The source shown above establishes that these Prefect
  crons are `Asia/Seoul`, not UTC.

Run `uv run python scripts/postuntil_timers_check.py` in CI. It invokes only
`postuntil run --dry-run`; it performs zero network calls. Its tests include a
one-field parameter mutation and a KST clock-offset mutation, both red.

## Cutover procedure (operator-owned)

For each future A timer, one at a time:

1. `systemctl enable --now kick-<task>.timer`.
2. Wait for its first service run and inspect the final JSON summary. Proceed
   only if `outcome=success`.
3. Pause the matching Prefect deployment, then register its healthcheck ping.
4. Roll back by disabling the timer and unpausing the same Prefect deployment.

`kis_live.reconcile_periodic` remains explicitly excluded. Its task checks
`KIS_LIVE_AUTO_RECONCILE_SAFETY_REVIEW_PASSED` at
`app/tasks/kis_live_reconcile_tasks.py:30-34`; the reported deployment has
failed since 2026-06-11 while that gate is unset. It is **operator decision
pending**, not a timer candidate.
