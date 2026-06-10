# Snapshot Reviewer Handoff (ROB-269 Phase 4)

How a reviewer agent (Claude / Codex / Gemini) consumes the ROB-269
snapshot bundle context produced in Phase 1+2+3. This is the explicit
"reviewer handoff" surface — there is no dedicated reviewer service; the
existing Phase 2 MCP tools serve as the contract, and Phase 3's
``InvestmentReportResponse`` already exposes the 6 snapshot metadata
fields that a reviewer needs.

## What a reviewer sees on an investment report

The HTTP read surface ``GET /invest/api/investment-reports/{report_uuid}``
returns an ``InvestmentReport`` with six Phase 3 fields (all nullable;
legacy reports serialise them as JSON ``null``):

| Field | Meaning |
|---|---|
| ``snapshot_bundle_uuid`` | Identity of the snapshot bundle this report was generated against. ``null`` for legacy reports. |
| ``snapshot_policy_version`` | The frozen policy version (e.g. ``intraday_action_report_v1``). |
| ``snapshot_coverage_summary`` | ``{"required": {...}, "optional": {...}}`` with per-kind freshness status. |
| ``snapshot_freshness_summary`` | ``{"overall": ..., "<kind>": {"status": ..., "as_of": ...}}``. DB CHECK rejects ``published`` rows whose ``overall`` is not one of ``fresh / soft_stale / partial``. |
| ``source_conflicts`` | Optional advisory: any cross-source discrepancies the generator noticed. |
| ``unavailable_sources`` | Optional advisory: sources that came back ``unavailable`` / ``확인 불가``. |

The ``/invest`` UI renders a ``SnapshotBundleFreshnessChip`` on the
report header that mirrors this shape — what the user sees, the reviewer
agent gets via the API.

## MCP tools available to reviewers

All four are read-mostly (snapshot-table appends only, no broker mutation):

### 1. ``investment_snapshot_bundle_get``

Fetches a bundle by UUID, including per-item linkage. Use this to
reconstruct the full data context behind a report.

```
investment_snapshot_bundle_get(bundle_uuid=<from report>, include_payload_preview=False)
```

Set ``include_payload_preview=True`` for a 2KB-truncated preview of each
linked snapshot's ``payload_json`` (useful for "why does this number
look like that?" reviewer questions).

### 2. ``investment_snapshot_list``

Recent snapshot metadata, filtered by market / symbol / kind / source.
Use when a reviewer wants to see "what other portfolio snapshots have we
captured for this account today?" without committing to a specific
bundle.

### 3. ``investment_snapshot_bundle_list``

Recent bundle headers, filtered by purpose / market / account_scope /
status. Use when a reviewer wants to see "what bundles have been built
today for ``kr_action_report``?"

### 4. ~~``investment_snapshot_refresh_request``~~ (retired, ROB-488)

The refresh-request tool was retired from the MCP surface in ROB-488: no
scheduler ever consumed the rows it inserted, so it was an honest no-op.
A reviewer that needs fresher data should ask the operator to rebuild the
bundle (``investment_report_prepare_bundle`` on a Hermes-enabled deployment,
or the snapshot build CLI). The implementation function remains in
``investment_snapshots_tools.py`` for a future Phase 5+ scheduler track.

## Interpreting ``snapshot_freshness_summary``

The Decision 4 three-layer stale gate consumes this shape:

* Layer (i) — DB CHECK ``ck_investment_reports_no_published_on_hard_stale``
  rejects ``published`` rows whose ``overall`` is in
  ``{hard_stale, failed, unavailable}``.
* Layer (ii) — ``derive_generator_constraints`` returns
  ``allow_action_language=False`` when ``overall`` is failed/stale_fallback
  OR any critical kind (portfolio / journal / watch_context / market) is
  ``hard_stale / unavailable / failed``.
* Layer (iii) — ``lint_action_language`` rejects report text containing
  executable action verbs (매수 / 매도 / buy / sell / ...) when layer (ii)
  would reject.

**A reviewer must not recommend executable action language** for any
report whose layer (ii) check returns ``allow_action_language=False``.
The stale-gate output is attached to ``report.metadata.stale_gate`` for
every ingested report (whether the flag is on or off — flag enabled just
turns advisory into raise-before-insert).

## What a reviewer must NOT do

* No broker / order / watch-intent mutation. All four MCP tools above
  are snapshot-domain only.
* No bypassing the stale gate. If layer (ii) returned ``False``, the
  report's no-action language is the contract; recommending a trade
  anyway is a safety violation.
* No flipping feature flags (``INVESTMENT_SNAPSHOTS_MCP_ENABLED``,
  ``ACTION_REPORT_BUNDLE_BASED_GENERATION_ENABLED``,
  ``ACTION_REPORT_BUNDLE_UI_ENABLED``). Note:
  ``ACTION_REPORT_BUNDLE_UI_ENABLED`` is scaffold-only in Phase 4 — no
  HTTP endpoint surfaces it to the SPA and the frontend chip does not
  read it. The chip's default-off behaviour comes from data-presence
  gating (legacy report → null freshness summary → no chip).
* No deploys / Prefect deployment registrations. The Phase 4 scheduler
  flow at ``app/flows/investment_snapshots_refresh_flow.py`` is
  importable only **when Prefect is installed** — Prefect is not
  currently a project dependency in ``pyproject.toml``. Adding it and
  registering a deployment are both deferred ops changes.

## Cross-references

* Phase 1 pre-plan: ``docs/superpowers/plans/2026-05-19-rob-269-pre-plan.md``
* Phase 3 plan + Decision 4 layers: ``docs/superpowers/plans/2026-05-19-rob-269-phase-3-report-generator.md``
* Phase 4 plan (this PR): ``docs/superpowers/plans/2026-05-19-rob-269-phase-4-ui-and-scheduler.md``
