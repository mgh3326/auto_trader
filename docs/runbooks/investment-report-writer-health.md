# investment_report_* writer health (ROB-1488)

`review.investment_reports` / `_items` / `_item_decisions` have not been written since
**2026-07-28T00:32:03Z**. The watch surface kept working the whole time
(`investment_watch_events_list_recent` 245 calls / 30d), which made the gap look like a broken
writer. The report-writing services and MCP/HTTP entrypoints exist in this repo, but
no internal scheduled producer calls them. Recent watch events use the separate direct-watch path.

This runbook is the read-only measurement and the restart-vs-retire decision. Nothing here
creates a report, and no report may ever be inferred from a watch read.

## 1. Measured state (2026-11-02, read-only aggregates from the active API container)

| surface | rows | last write | last 30d |
|---|---:|---|---:|
| `review.investment_reports` | 68 | 2026-07-28T00:32:03Z | 0 rows / 0 days |
| `review.investment_report_items` | 335 | 2026-07-28T00:32:03Z | 0 |
| `review.investment_report_item_decisions` | 62 | 2026-07-28T00:32:25Z | 0 |
| `review.investment_watch_alerts` | 721 (76 active) | 2026-11-02T02:38:02Z | 44 |
| `review.investment_watch_events` | 499 | 2026-10-30T05:05:00Z | 29 delivered |

Report status: `draft=49`, `published=14` (last 2026-06-10), `superseded=5`. Every report's
`valid_until` is in the past (max 2026-05-30T11:00Z), so none is current context.

Report `created_by_profile` spans 19 out-of-process profiles, e.g. `HERMES_ADVISOR` n=27
(last 2026-06-10), `CLAUDE_ADVISOR` n=10 (last 2026-07-28), `schedule` n=4 (all 2026-05-30),
`HERMES_POST_DEPLOY_SMOKE` n=3 (2026-05-27). Several are one-off smoke runs.

### Watch↔report linkage is mostly absent by design

`review.investment_watch_alerts` / `..._events` carry `source_report_uuid` / `source_item_uuid`,
but three classes exist and only one is a link:

- `null` — direct watch (`investment_watch_create`, ROB-768). No report/item by design.
- `resolved` — matches `review.investment_reports.report_uuid` (activated from a report item).
- `dangling` — non-null legacy **UUIDv5 placeholders** from the pre-ROB-1103 direct-watch path.
  The ROB-1103 migration (`20260728_rob1103_watch_source_links`) deliberately preserved them with
  `NOT VALID` foreign keys. They look like links and never resolve.

Measured: active alerts `null=76, resolved=0, dangling=0`; 30-day events
`null=29, resolved=0, dangling=0`; all-time dangling alerts 169 / events 132, none active.
The last report-linked event fired 2026-08-24 from an alert created on or before 2026-07-28 —
the active alerts and recent events in this measurement are direct watches. **These watch
observations do not establish that a report should exist**, and a non-null `source_report_uuid` must never be counted
as a link without the `EXISTS` check above.

## 2. Root cause

Writer entrypoints, all of them:

| entrypoint | in-repo schedule | state |
|---|---|---|
| `investment_report_create` (MCP) | none — out-of-process caller | 0 calls / 30d |
| `investment_report_generate_from_bundle` | none | `SNAPSHOT_BACKED_REPORT_GENERATOR_ENABLED` default **false** |
| Hermes HTTP ingest (`/trading/api/investment-reports/hermes/`) | none | gate default off + `HERMES_INGEST_TOKEN`; `review.investment_snapshot_*` tables **empty** in this measurement; prior runs are not proved |
| `scripts/invest_reports_us_schedule.py` | none — default-disabled manual CLI (`INVEST_REPORTS_US_SCHEDULE_ENABLED`) | not referenced by any TaskIQ/Prefect flow or `ops/ncp/systemd/*.timer` |

There is no TaskIQ task, Prefect flow, or desk timer that writes these tables. The 68 rows are
the trace of sessions that called the tool from 2026-05-19 to 2026-07-28; the last one
(`CLAUDE_ADVISOR`, 2026-07-28) is also the commit date of ROB-1103, after which direct watch
creation became the standard path and the report+activate watch flow stopped being used.

**Verdict: dormant report generation.** No recent external report-create calls or internal
scheduled producer are observed. The watch activity is independent of report generation.
This measurement does not exercise every writer or prove that all handlers are healthy.

## 3. Diagnostic

```bash
ENV_FILE=<env> uv run python -m scripts.diagnose_investment_reports          # human summary
ENV_FILE=<env> uv run python -m scripts.diagnose_investment_reports --json    # payload
```

- SELECTs only; the session opens with `SET TRANSACTION READ ONLY` and `statement_timeout=8000`.
  No insert/update/delete, no broker call, no report creation — enforced by
  `tests/scripts/test_diagnose_investment_reports.py`.
- Link counts use `EXISTS` against `review.investment_reports`, so legacy placeholders are
  reported as `dangling`, never as links.
- Exit code `0` = last report write within `STALL_THRESHOLD_DAYS` (14); `1` = stale or never
  written. The 14-day threshold is a visibility device — no report cadence is declared in the
  repo, so a desk-declared cadence should replace it before this gates anything.
- `watch_linkage.verdict = no_report_linked_events` means no report-linked event was
  observed in the selected window. It is not proof of the absence of every report dependency.

## 4. Approved disposition (operator-desk, 2026-11-03)

H4 takes A: preserve `investment_report_create`, `investment_report_list`, and
`investment_report_get`. Retire the dormant Hermes composition/write pipeline in
R4, using this SELECT-only diagnostic as its evidence. This PR does not remove
those paths. No writer activation or backfill is authorized.

The 68 historical rows and their legacy watch links remain unchanged. R4 must
preserve the report item contract for the three retained tools and must not
manufacture reports from watch activity.

## 5. After-deploy verification (read-only)

Run from the active API container (`ssh ncp 'C=at-api-$(cat /root/at-run/api-active-color); docker exec $C ...'`):

```sql
-- 1. writer heartbeat + 30-day cadence (diagnostic deployment alone must not move last_created or create rows)
select count(*)                                            as rows,
       max(created_at)                                     as last_created,
       count(*) filter (where created_at >= now() - interval '30 days') as rows_30d,
       count(distinct (created_at at time zone 'Asia/Seoul')::date)
         filter (where created_at >= now() - interval '30 days')         as days_30d
from review.investment_reports;

-- 2. item history remains unchanged by this diagnostic
select count(*) as rows, max(created_at) as last_created
from review.investment_report_items;

-- 3. watch linkage truth: resolved links only (dangling = ROB-1103 placeholders, not links)
select case
         when source_report_uuid is null then 'null'
         when exists (select 1 from review.investment_reports r
                      where r.report_uuid = a.source_report_uuid) then 'resolved'
         else 'dangling'
       end                       as link_class,
       status,
       count(*)                  as rows,
       max(created_at)           as last_created
from review.investment_watch_alerts a
group by 1, 2
order by 1, 2;

-- 4. recent events must not be read as report dependencies when link_class = 'null'
select case
         when source_report_uuid is null then 'null'
         when exists (select 1 from review.investment_reports r
                      where r.report_uuid = e.source_report_uuid) then 'resolved'
         else 'dangling'
       end                       as link_class,
       delivery_status,
       count(*)                  as rows,
       max(delivered_at)         as last_delivered
from review.investment_watch_events e
where created_at >= now() - interval '30 days'
group by 1, 2
order by 1, 2;
```

Re-run `scripts.diagnose_investment_reports` to compare the measured state. The diagnostic
continues to exit `1` while historical report writes remain outside its visibility threshold;
shipping this diagnostic does not restart them.

## 6. Limits

- The diagnostic does not read env gate values (that is not SQL); the desk reads
  `SNAPSHOT_BACKED_REPORT_GENERATOR_ENABLED` / `INVEST_REPORTS_US_SCHEDULE_ENABLED` itself.
- Absence of writes is not absence of code paths; §2 lists the entrypoints statically.
- Legacy dangling links are preserved on purpose by the ROB-1103 migration. Do not delete or
  rewrite them to "clean up"; count them as `dangling`.
- Scheduling, retirement, and any migration remain desk decisions. This PR adds a read-only
  diagnostic and this runbook only.
