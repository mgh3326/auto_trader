# ROB-61 Review Report

**Reviewer:** Claude Opus
**Branch:** `feature/ROB-61-kr-news-prefect-push-readiness`
**Date:** 2026-04-30
**Scope:** Documentation + test-only PR. No app/router/model changes.

## Verdict: PASS

This is a tightly scoped, low-risk PR that codifies the readiness contract and operational procedure for KR news scheduled push without touching any code path that could mutate production state. Hermes verification (ruff, ruff format, ty, pytest 37/37) is green.

## 1. Safety — Pass

- No `app/` source modifications. Bulk ingest, readiness, preopen, broker, watch, order-intent paths untouched.
- Both Prefect deployments (`kr-core/hourly`, `pending-push/manual`) remain `paused=true`; runbook is explicit that this PR does **not** unpause and gates activation behind an out-of-repo follow-up (OOR-1) plus a 6-item unpause checklist.
- Smoke script is GET-only against `127.0.0.1:8000`, `set -euo pipefail`, depends only on `curl` + `jq`, `--max-time 10`, no headers/tokens/body emitted.
- Dry-run posture (`execute=false`) preserved as default in proposed schedule.
- No secrets, connection strings, or tokens appear in any new artifact.
- ROB-62/63 explicitly out of scope.

## 2. Runbook Correctness — Pass with minor nit

Runbook accurately reflects observed deployment state (paused, NOT_READY, tags, entrypoints, params `{limit:25, execute:false}`). Readiness contract (`status ∈ {success, partial}`, 180 min, non-empty `source_counts`) matches the implementation lock-in tests.

**Nit (not blocking):** §Rollback references `news-ingestor-pending-push/scheduled` for the pause command, but that deployment doesn't exist until OOR-1 lands; only `…/manual` exists today. The rollback ordering is correct *post-unpause*; a one-line note ("`scheduled` slug applies after OOR-1; pre-unpause, pause `…/manual`") would remove ambiguity. Acceptable as-is given OOR-1 is the gating prerequisite.

Slug convention `news-ingestor-kr-core/hourly` is conventional Prefect flow/deployment slug form for the observed display names.

## 3. Smoke Script — Pass

- Read-only, on-demand operator script. No POST, no ingest endpoint, no `--execute`.
- `curl 2>/dev/null` masks transport-layer chatter — minor diagnostic loss but no leak risk and consistent with the documented "ERROR" summary line contract.
- `PREOPEN_BASE_URL` env override is sensibly defaulted to localhost; the runbook's intent (target `current` symlinked deploy on `127.0.0.1:8000`) is preserved.
- Acceptance criteria (`READY` / `WARN: …` / `ERROR`) match runbook §3.

## 4. Tests — Pass

`tests/test_news_readiness_contract.py`:
- Pure-unit, no DB, no network. Mocks `db.execute.side_effect` for the two-query path in `get_news_readiness`.
- Pins the exact contract Prefect side depends on: `success`/`partial` whitelist, `finished_at=None` → `news_run_unfinished`, empty `source_counts` → `news_sources_empty`, no run → `news_unavailable`, default `max_age_minutes=180`, override honored, boundary case (30 min stale under 180-min default is ready), warning dedup.
- Coupling to private `_news_readiness_payload` is intentional contract lock-in, not a smell.
- `test_max_age_minutes_default_is_180` via `inspect.signature` is slightly brittle if the default later moves to a config constant; acceptable trade-off for explicit pinning.
- The two-query mock ordering (`run_result`, `article_result`) is implementation-coupled but matches current code.

`tests/test_preopen_dashboard_service.py`:
- One additive case verifying `news_unavailable` only demotes the `news` slot of `source_freshness` and preserves kis/upbit signals untouched. Asserts non-news warnings are not injected. Confirms fail-open semantics. Existing tests untouched.

## 5. Other Notes

- Plan doc and runbook duplicate §3 content; this is intentional (plan as audit trail, runbook as operator-facing). No drift detected between the two.
- AOE status footer in plan duplicated at top and bottom — cosmetic, not blocking.
- OOR-1/OOR-2 follow-ups in `robin-prefect-automations` are clearly delineated and properly out-of-scope for auto_trader.

## Minor Suggestions (non-blocking, can be addressed in OOR-1 or a follow-up doc PR)

1. Add one line to runbook §Rollback noting the `…/scheduled` slug applies only after OOR-1; pre-OOR-1 rollback uses `…/manual`.
2. Consider parameterizing `max_age_minutes` default into a single named constant in `app/services/llm_news_service.py` to make the contract test less brittle. (Out of scope here.)

---

PR is ready to ship as a documentation/test lock-in. Activation of any Prefect schedule remains correctly gated on OOR-1 + the §3.7 unpause checklist.

AOE_STATUS: review_passed
AOE_ISSUE: ROB-61
AOE_ROLE: reviewer
AOE_REPORT_PATH: docs/plans/ROB-61-review-report.md
AOE_NEXT: create_pr
