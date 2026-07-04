# A′ shadow replay harness (ROB-697, M1)

This runbook covers the "A′ shadow replay" harness: replaying past
bundle-backed report-item decisions through a headless `claude -p`
process to measure self-consistency and (when a reference decision
exists) fidelity to what was actually decided. M1 builds the read-only
corpus-selection layer only (`app/services/shadow_replay/`); the replay
driver itself is a later task.

> **Boundary**: this harness is read-only analysis tooling. No writes,
> no broker/order/watch mutation, no in-process LLM (ROB-501). The
> `claude -p` driver (later task) is an out-of-process subprocess, not
> an in-process provider.

---

## P0 census (operator)

**Why this exists:** the design initially assumed the reference corpus
would be "my (Claude-authored) decisions with a frozen evidence
bundle." Grounding found these are in tension: `investment_report_create`
(plain human/API authoring) never sets `snapshot_bundle_uuid`; non-null
bundles are only produced by `generate_from_bundle` / Hermes ingest. So
`created_by_profile='CLAUDE_ADVISOR' AND snapshot_bundle_uuid IS NOT NULL`
may be nearly empty in production. This section is the read-only census
an operator runs against the **real** production DB (not the test DB)
to decide which corpus source `select_replay_corpus` should actually
return in practice.

`app/services/shadow_replay/corpus.py` implements two of the three
possible sources as executable code paths (`claude_bundle`,
`hermes_bundle`); it raises `CorpusUnavailable` when neither has
buy+sell+watch coverage, deferring to this census instead of guessing.
The third source (`operator_audit`) is **not** implemented as a code
path — see rule 3 below.

### Step 1 — run the census SELECTs by hand (read-only)

Point `DATABASE_URL` at the real DB (or a read replica / local mirror
per your usual `docs/runbooks/` conventions) and run:

```sql
-- Census: bundle-backed items by authorship, minus auto_emit machine items
SELECT r.created_by_profile,
       COUNT(*) FILTER (WHERE r.snapshot_bundle_uuid IS NOT NULL)                                   AS with_bundle,
       COUNT(*) FILTER (WHERE r.snapshot_bundle_uuid IS NOT NULL
                        AND COALESCE(it.evidence_snapshot->>'source','') <> 'auto_emit'
                        AND COALESCE(it.evidence_snapshot->>'proposer','') NOT LIKE 'auto_emit/%')  AS with_bundle_non_autoemit,
       COUNT(*) FILTER (WHERE it.side IN ('buy','sell'))                                             AS actionable
FROM review.investment_report_items it
JOIN review.investment_reports r ON r.id = it.report_id
GROUP BY r.created_by_profile
ORDER BY with_bundle_non_autoemit DESC;

-- Also count the truer "operator actually shipped this" signal
SELECT decision, COUNT(*)
FROM review.investment_report_item_decisions
WHERE decision IN ('approve','partial_approve','reprice')
GROUP BY decision;
```

### Step 2 — pick the corpus source (decision gate)

Decision rule (record the chosen source + the census numbers in this
file once run):

1. If `CLAUDE_ADVISOR` has ≥ (3×`min_per_kind`) `with_bundle_non_autoemit`
   rows spanning buy+sell+watch → **source = `claude_bundle`** (closest
   to "my decision"). `select_replay_corpus` already returns this
   automatically once the data exists — no code change needed.
2. Else if `HERMES_ADVISOR` has enough such rows → **source =
   `hermes_bundle`**. This still directly serves ROB-644's stated goal
   ("different LLM models should reach the same result"): A′ then
   measures headless-Claude vs. Hermes consistency on identical frozen
   evidence. `select_replay_corpus` also already returns this
   automatically as the fallback.
3. Else → **source = `operator_audit`**: join
   `investment_report_item_decisions`
   (`decision IN ('approve','partial_approve','reprice')`,
   `approved_payload_snapshot` is the verbatim shipped params) back to
   items with a non-null bundle. This is the truest "shipped" signal,
   but it requires the report to have a bundle. If operator-audit rows
   also lack bundles, **STOP** — report to the user that no replayable
   corpus exists yet; A′ can't run until bundle-backed decisions
   accumulate. `operator_audit` is intentionally **not** implemented as
   a `corpus.py` code path today (`select_replay_corpus` raises
   `CorpusUnavailable` in this case) — implement it as a follow-up once
   the census confirms it's the live path, rather than building a third
   untested branch against a corpus that may not exist.

**Status:** not yet run. `select_replay_corpus` will raise
`CorpusUnavailable` against a fresh/empty DB (including the test DB)
until real bundle-backed, non-auto_emit `CLAUDE_ADVISOR` or
`HERMES_ADVISOR` items with buy+sell+watch coverage exist.

---

## Corpus selection (M1, implemented)

`app/services/shadow_replay/corpus.py`:

- `select_replay_corpus(session, *, min_per_kind=1, limit=40) -> CorpusSelection`
  — tries `CLAUDE_ADVISOR` bundle-backed items first, then
  `HERMES_ADVISOR`; returns whichever first covers `action` + `watch`
  kinds at `min_per_kind` each. Raises `CorpusUnavailable` otherwise
  (see Step 2 rule 3).
- `_bundle_items_for_profile(session, profile, limit)` — the lower-level
  query: bundle-backed items for one `created_by_profile`, filtered
  through `_non_autoemit` (drops `evidence_snapshot.source == "auto_emit"`
  and `evidence_snapshot.proposer` starting with `auto_emit/`, plus the
  `intraday_floor` proposer). No coverage gate — useful directly when you
  just want "what does this profile have," independent of the
  action+watch minimum.
- Each `CorpusItem.reference_decision` is `extract_decision(item)` from
  `app/services/shadow_replay/scoring.py` (Task 1, already shipped) —
  the pure-function decision scorer used later to compare replay output
  against what was actually decided.

Tests: `tests/services/shadow_replay/test_corpus.py`. The DB-touching
test (`test_autoemit_item_excluded`) is `@pytest.mark.integration` and
exercises `_bundle_items_for_profile` directly (not
`select_replay_corpus`) — seeding just one `action` item can't satisfy
`select_replay_corpus`'s action+watch coverage gate, so testing the
auto_emit exclusion has to go through the lower-level function. The
pure-predicate tests (`_non_autoemit`, `_covers_kinds`) are
`@pytest.mark.unit` and need no DB.

---

## P1 probe (operator)

**Why this exists:** A′ replay is only meaningful if the same bundle
yields the same decision-bearing context across calls. `get_hermes_
context` emits no verbatim timestamp; `stage_inputs` + `cited_snapshots`
+ `policy_version` + `market` + `market_session` + `coverage_summary`
are frozen from the persisted bundle. `dimension_evidence` /
`dimension_reports` read LIVE tables and can drift between calls even
for the same bundle.

`scripts/shadow_replay_probe.py` calls
`investment_report_get_hermes_context_impl` twice for one
`bundle_uuid` and diffs the result:

- `compare_frozen(a, b) -> dict` — pure function (dict in, dict out,
  no I/O). Compares the frozen keys via
  `json.dumps(..., sort_keys=True)` equality and lists which of
  `dimension_evidence` / `dimension_reports` differ. Unit-tested in
  `tests/test_shadow_replay_probe_cli.py` with no DB.
- `probe(bundle_uuid) -> int` — the async two-call round trip. Requires
  a live DB, a real `bundle_uuid` (from the Task 0 corpus), and
  `SNAPSHOT_BACKED_REPORT_GENERATOR_ENABLED=true` on the process. Not
  exercised by the test suite — run it manually as an operator step.

Read-only: no stage_run / artifact rows are persisted, no broker /
order / watch / order-intent mutation, no in-process LLM provider
(ROB-501).

### Run it

```bash
SNAPSHOT_BACKED_REPORT_GENERATOR_ENABLED=true \
    uv run python -m scripts.shadow_replay_probe <bundle_uuid>
```

Expected for two calls seconds apart:

```json
{"frozen_identical": true, "live_section_drift": []}
```

Exit code `0` means the frozen sections matched; `1` means they
drifted (investigate before trusting the K-replay batch, Task 4); `2`
means the first call itself failed (`success=False` — bad
`bundle_uuid`, or the feature flag isn't set).

If `live_section_drift` is non-empty, record it here — it means the
K-replay batch (Task 4) must run tight in time, and the decision
prompt (Task 4) must instruct the agent to base its call on
`stage_inputs` / `cited_snapshots` (the frozen sections), not on
`dimension_evidence` / `dimension_reports`.

**Status:** not yet run. Requires a real `bundle_uuid` from the P0
corpus census above; run once that census has produced a usable
corpus source.
