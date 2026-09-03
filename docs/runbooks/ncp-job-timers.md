# NCP job timers

This runbook moves only the safe, static-argv part of the 2026-09-04 Prefect
class-B inventory to NCP `systemd` timers. It does not change, pause, or delete
a Prefect deployment. It does not access NCP during development.

## Contract

`ops/ncp/bin/at-job.sh` reads `at-api`'s configured image with `docker
inspect`, rejects anything other than `ghcr.io/mgh3326/auto_trader@sha256:…`,
and uses that exact digest for the transient job container. It has no tag
fallback. Each service uses both root-owned deployment environment files:
`/root/at-secrets/.env.api` and `/root/at-secrets/.env.scheduler`.

The unit itself names only `/root/at-secrets/.env.jobs`. Define optional
healthcheck endpoints there by module-derived uppercase key, for example:

```
HC_PING_URL_BUILD_INVESTOR_FLOW_SNAPSHOTS=https://hc-ping.example/uuid
```

The wrapper holds a non-blocking `flock` for its module, preserves the child
exit code (including timeout), and always emits one final JSON summary:
`{"module", "rc", "elapsed_s", "image_digest"}`. A duplicate exits `75`;
an unresolved/non-digest serving image exits `78`.

`scripts/ncp_job_timers_check.py` parses every checked-in `job-*` pair. It
proves the `ExecStart` argv captured from the source flow, module import,
timeout inheritance, five next KST occurrences, timer no-catchup/no-jitter,
and absence of duplicate schedules. It makes no network calls.

## Included static jobs

| unit | Prefect evidence | Prefect cron (KST) | argv after `python -m` |
| --- | --- | --- | --- |
| `job-kr-investor-flow-snapshots` | `investor_flow_snapshots.py:75-129,177-201` | `10 18 * * 1-5` | `scripts.build_investor_flow_snapshots --market kr --days 5 --batch-size 100 --concurrency 4 --all --commit` |
| `job-toss-warnings-sync` | `toss_warnings_sync.py:54-76,111-117` | `30 7 * * *` | `scripts.sync_toss_warnings` |
| `job-us-invest-screener-snapshots` | `invest_screener_snapshots_us.py:80-129,188-202` | `10 6 * * 2-6` | `scripts.build_invest_screener_snapshots --market us --batch-size 200 --concurrency 4 --all --common-stocks-only --commit` |

The cited cron declarations specify `timezone="Asia/Seoul"`; timers therefore
use `Asia/Seoul`, not host-local or UTC time.

## Explicitly excluded B inventory

These are not omissions. A static systemd `ExecStart` would not make the same
subprocess argv/env/timeout as its flow, so moving one would violate the
migration contract.

| Prefect deployment(s) | Evidence | exclusion reason |
| --- | --- | --- |
| daily market events | `market_events_ingestion.py:133-168` | one flow runs US and KR target subprocesses plus status notifications; no single module argv preserves it |
| crypto screener snapshots | `invest_crypto_screener_snapshots.py:85-128` | `--commit` is selected by runtime `INVEST_SCREENER_SNAPSHOTS_COMMIT_ENABLED` |
| crypto insight snapshots | `invest_crypto_insight_snapshots.py:157-200` | `python -c RUNNER_CODE` plus runtime env projections; not a module invocation |
| KR fundamentals snapshots | `invest_kr_fundamentals_snapshots.py:111-156` | `--commit` is selected by runtime `INVEST_SCREENER_SNAPSHOTS_COMMIT_ENABLED` |
| US valuation snapshots | `market_valuation_snapshots_us.py:96-151` | `--commit` is selected by runtime `MARKET_VALUATION_SNAPSHOTS_COMMIT_ENABLED` |
| Toss symbol master sync | `toss_symbol_master_sync.py:25-68`; `toss_symbol_master.py:46-85` | one flow starts independent KR and US subprocesses, each with its own 900s timeout |
| US fundamentals snapshots | `us_fundamentals_snapshots.py:89-147` | `--commit` is selected by runtime `MARKET_VALUATION_SNAPSHOTS_COMMIT_ENABLED` |
| Mac server mode (10m, hourly, manual), Mac health smoke (10m, manual), result freshness, runtime monitor, Prefect watchdog, Kanban monitor, honcho watchdog | `prefect-to-postuntil.md:29-43` | external Mac/process flows, interval/manual schedule, or non-`scripts.<module>` execution; not an NCP auto_trader container argv |
| Binance Demo scalping | `binance_demo_scalping.py:38-52` | separate operator safety review; no migration while demo execution authority is unresolved |

This is 18 excluded B entries and 3 safe static timer pairs. KIS reconcile,
session kickoff/nudge, b0x policy tables, and all C/D/E items remain out of
scope.

## Cutover (operator-owned)

For each included unit, in this order: Toss warnings → US screener freshness →
KR investor freshness. Do not batch or overlap the change.

1. Install the committed unit files and run `systemctl daemon-reload`.
2. Run `systemctl enable --now job-<name>.timer`.
3. Wait for the first service execution; inspect its final JSON summary and
   proceed only when `rc=0` and the digest is a `sha256` image.
4. Pause the matching Prefect deployment only after that successful run.
5. Add the corresponding `HC_PING_URL_<MODULE>` value to the root-owned jobs
   environment file and reload/restart only the timer as appropriate.

The write-capable snapshot jobs are intentionally after Toss warnings. The
Prefect deployment is paused one unit at a time, never deleted.

Rollback is the inverse for the same single deployment: disable its timer,
then unpause the matching Prefect deployment. Do not change the job argv,
image pinning, gates, host policy, or a broker execution surface as part of a
rollback.
