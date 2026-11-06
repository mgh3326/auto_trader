# Deterministic file-shard manifests + exact-cover guard (ROB-1312)

**Status: live.** The four core `test (3.13, N)` matrix jobs and the weekly
duration-refresh workflow both run committed file manifests instead of a
runtime `pytest-split --splits/--group` selection. Since ROB-1454 those jobs
run on main pushes, dispatch and `run_all` PRs only; other PRs run
`affected-tests` (`docs/runbooks/ci-required-aggregator.md`).

## 1. Why this exists

Before ROB-1312, every `test (3.13, N)` job ran:

```
pytest tests/ -m "not live" --splits 4 --group N --durations-path .test_durations -n 4 ...
```

`pytest-split` collects the **whole** suite in every one of the four jobs
(collection is not split, only execution is), then deselects everything
outside group `N` at runtime. That means the full ~21k-node suite was
collected four times per run, and *which* tests landed in which shard was an
opaque function of `.test_durations` plus pytest-split's own balancing
algorithm — not something you could read off a committed file.

ROB-1312 replaces that with four version-controlled file manifests
(`ci_shards/shard-1.txt` … `shard-4.txt`), each a plain sorted list of test
file paths. Every `test (3.13, N)` job now runs exactly its manifest's files,
nothing else:

```
mapfile -t SHARD_FILES < "ci_shards/shard-${N}.txt"
pytest "${SHARD_FILES[@]}" -m "not live" -n 4 ...
```

## 2. The pieces

| piece | file | what it is |
|---|---|---|
| planner/validator | `scripts/ci/file_shard_plan.py` | `generate` (full rebalance — duration refresh only) and `check` (validate exact cover) |
| manifests | `ci_shards/shard-{1..4}.txt` | committed, one test file path per line, sorted |
| audit report | `ci_shards/weights.json` | per-shard weight, `--dist=loadfile` predicted pytest time and spread; not consumed by `check` |
| weight artifact | `.test_durations` (setup + call + teardown per test), produced by the weekly refresh | the only `generate` weight input |
| duration merge contract | `scripts/merge_test_durations.py`, `scripts/call_durations.py` | disjoint-shard-collection validation for the weekly refresh |
| workflow wiring | `.github/workflows/test.yml` (`test`, `taskiq-smoke`), `.github/workflows/test-durations-refresh.yml` (`collect-authoritative`, `measure`, `merge`) | consumes the manifests |

## 3. Weight model and the LPT algorithm

`generate` sums per-file weight from `.test_durations` — the pytest-split
`--store-durations` map the same weekly refresh already produces, which
measures the **whole per-test protocol (setup + call + teardown)** — and
assigns files to shards via a deterministic largest-processing-time-first
(LPT) bin-pack:

- **Measured node**: its `.test_durations` value.
- **Unmeasured node** (collected today, absent from the artifact — a new
  test added since the last refresh, or drift): weight equal to the **mean
  of every measured duration in that same artifact** — a deterministic,
  reproducible function of the committed input, not a hand-tuned constant
  (`_unmeasured_fallback_seconds`). If the artifact has zero measured
  durations (empty/bootstrap), the fallback is
  `UNMEASURED_FALLBACK_DEFAULT_SECONDS` (`1.0`).
- File weight = sum of its nodes' weights, accumulated by iterating the
  authoritative node list **in sorted order** — float summation order is
  fixed, so regenerating from the same inputs is byte-for-byte identical
  (`0.1 + 0.2 + 0.3` is `0.6000000000000001`; `0.3 + 0.2 + 0.1` is `0.6`).

Assignment: files are visited in `(-weight, path)` order (largest first, path
tie-break); each file goes to whichever shard currently has the smallest
running total, ties broken by the lowest shard index. Both tie-breaks are
encoded directly in sort/min keys — determinism does not depend on
dict/set iteration order or Python version.

### 3.1 Why the whole protocol, not the call phase (hk-3336)

Until hk-3336 the weight source was `.call_durations.json` (ROB-1295). That
artifact records only the `call` phase, and on the 2026-11-03 refresh
artifact (run `41556077676`) that is **3,268.2 s against 3,703.7 s of real
per-test pytest time — 435.5 s (13.3 %) invisible to the weight**, almost all
of it session/worker bootstrap landing on whichever test ran first
(`whole ≈ 5.2–20.8 s`, `call ≈ 0.001 s`). A shard pays that time; only a
whole-protocol measurement attributes it.

`.test_durations` also covers every authoritative node (including nodes whose
`setup` skips, which is what `.call_durations.json` had to record separately
as `not_called`), and it is what `pytest -n 4` actually spends.

### 3.2 Weight sum is not wall time: the `--dist=loadfile` floor

`test.yml` and the refresh both run
`pytest "${SHARD_FILES[@]}" -n 4 --dist=loadfile`: the **file** is the
scheduling unit, so a shard's pytest time is a *makespan*, and no packing can
finish a shard before its largest single file. On the 2026-11-03 artifact the
largest file is `tests/scripts/test_deploy_ncp_pull_h3_crypto_paper.py` at
**301.0 s**, against a **231.5 s** fair share per worker — so the equal-total
LPT packing that reports `max_min_spread_pct: 0.0` cannot be realised by any
job.

`weights.json` therefore also reports, per shard, `predicted_pytest_seconds`
from `predict_shard_pytest_seconds`: replay xdist's own hand-out — visit the
manifest's files in its (sorted) argv order and give each to the worker with
the smallest accumulated total — and take the maximum. `predicted_*` is the
number to look at when judging balance; `total_weight_seconds` /
`max_min_spread_pct` are kept for continuity, and `check` reads neither.

Also measured: each xdist worker collects **all** of its shard's files before
running (`Record shard collection manifest` took 55/40/60/64 s for
521/504/526/528 files ≈ 0.10 s/file, single process). That is a per-shard
constant when file counts are equal — which is why packing must keep file
counts equal too, and why the "starve the shard that holds the giant file"
minimax packing (4.5 % predicted spread, but **1 file in one shard and 1,958
in another**) is not used.

### 3.3 Measured spread, before and after

Same 2026-11-03 measurement run, same collection (this repo's
`pytest --collect-only -m "not live" tests/`, 33,723 nodes / 2,084 files):

| assignment | shard 1 | shard 2 | shard 3 | shard 4 | predicted spread |
|---|---|---|---|---|---|
| committed manifests (not regenerated since 2026-10-19) | 344.0 | 229.9 | 309.8 | 271.7 | **49.65 %** |
| `generate` on call-phase weights | 333.7 | 301.9 | 229.4 | 232.7 | **45.46 %** |
| `generate` on whole-protocol weights (current) | 321.5 | 320.8 | 236.9 | 250.6 | **35.70 %** |

`Measure shard` on that run took 371 / 295 / 396 / 355 s. The residual
35.7 % is close to the arithmetic floor for an equal-weight packing —
`(301.0 − 231.5) / 231.5 = 30.0 %` — because the single 301 s file alone
exceeds a worker's fair share; the extra ~5 points are the dispatch delay the
two giant files pay in sorted order. Getting genuinely below the floor needs
one of: splitting the four `tests/scripts/test_deploy_ncp_pull_*.py` files
(≈1,010 s, 27 % of the suite) into smaller files, or dropping
`--dist=loadfile` so one heavy file's tests can span workers. Both are out of
scope here.

`ci_shards/weights.json`'s `max_min_spread_pct` is
**`(max_total - min_total) / min_total * 100`** — deliberately divided by the
**smaller** total, not the larger. Dividing by the larger number understates
the imbalance (e.g. `max=110, min=90` reads as `18.2%` via `/max` but
`22.2%` via `/min`), which could pass a real >10% imbalance under a naive
reading of the acceptance bar. `predicted_max_min_spread_pct` uses the same
formula on `predicted_pytest_seconds`. `weights.json` is an audit artifact
only — `check` does not read or depend on it.

## 4. Normal test-file change: minimal manifest entry

Any normal PR that adds a `tests/**/*.py` file must make the corresponding
**minimal** change in the same PR: add that path in exactly one
`ci_shards/shard-N.txt`, at its `LC_ALL=C` sorted position. For a removal,
delete its existing entry; for a rename, do that deletion plus one sorted
addition. Do not run `file_shard_plan generate` for these cases.

`generate` has no incremental mode. It rebuilds LPT assignment from one global
`(-weight, path)` ordering and overwrites every manifest. A non-destructive
measurement using the committed duration artifact and a collection derived
from it (`1666` files) still moved `1192/1666` shared files (`71.5%`)
between shards; the cause of that churn is not asserted here. A one-file add is
therefore not a use case for full regeneration. Exact-cover checks membership
exactly once; it does **not** check weight balance. The minimal entry is
sufficient for normal test-file work and avoids unrelated duration-refresh churn.

Choose the existing shard with the fewest entries and state that basis in the
PR. Then verify the exact cover:

```bash
uv run pytest tests/ --collect-only -q --no-cov -m "not live" \
  2>/dev/null | grep '^tests/.*::' | LC_ALL=C sort \
  > /tmp/collected.txt

# NOTE: `sort`, never `sort -u` — see §6.

uv run --no-sync python -m scripts.ci.file_shard_plan check \
  --collected /tmp/collected.txt \
  --manifest-dir ci_shards \
  --shard-count 4

git diff --stat -- ci_shards/
```

The diff must name only the intended path(s); a new file normally means one
manifest and one added line. The next duration refresh will measure its
weight and perform the only permitted full rebalance (§6.1).

If local `check` differs because its collection does not match CI's
authoritative collection, treat the PR's `affected-tests` result as authoritative.
Do not run `generate` to make a local check pass.

### 4.1 Duration refresh: full regeneration only

Full regeneration belongs only to the duration-refresh workflow described in
§6.1, where its fresh inputs and CI self-check live.

### 4.2 Input validation: structure here, provenance upstream

`generate` reads `.test_durations` through
`load_test_durations`, which fails closed on anything a corrupted or
hand-edited map could smuggle in:

- a duplicate JSON key (a bare `json.loads` keeps the last value silently) —
  rejected by the shared `scripts/call_durations.py::load_json_no_duplicate_keys`;
- a non-object top level, an empty/non-string node id, or a duration that is
  a bool, a non-number, negative, or non-finite (`Infinity`/`NaN`).

It deliberately does **not** require the artifact to describe today's tree: a
stale-but-well-formed map is valid input, and a node it lacks falls back to
the measured mean (§3). That is what makes a normal PR's one-line manifest
edit survivable between refreshes, and what lets this PR's manifests be built
from the last measured artifact while the tree has moved on.

The old `.call_durations.json` `source_commit_sha`/`collection_hash`
coherence guard (ROB-1312 R1) has no analogue here and is not replaced by a
weaker one, because **provenance is established upstream, in the same job**:
the refresh's `merge` job runs `scripts/merge_test_durations.py` over the four
measured shards against the same `authoritative-collected-nodes.txt` handed
to `generate`, which proves the map is a complete, disjoint-union function
of that capture; the step ordering is pinned by
`test_duration_refresh_regenerate_feeds_whole_protocol_durations`.
`.call_durations.json` keeps its own guard and its own `validate` step — it
is still produced, validated and committed; it is just no longer a weight
source.

Threat model: accidental corruption and hand-editing of a *committed file in
a branch-protected repo* — not an adversarial telemetry feed. `check` never
reads a duration artifact at all, so none of this guards the runtime CI
execution path (`test.yml`'s `test` matrix jobs just run whatever
`ci_shards/*.txt` already says) — it guards only the manifest-*generation*
path.

To only check (no write), e.g. to reproduce a CI failure locally:

```bash
python3 -m scripts.ci.file_shard_plan check \
  --collected /tmp/collected.txt \
  --manifest-dir ci_shards \
  --shard-count 4
```

Both subcommands must be invoked as `python3 -m scripts.ci.file_shard_plan
...` (module form), never `python3 scripts/ci/file_shard_plan.py ...`
(direct-script form) — the module imports `scripts.call_durations`, which
only resolves when the repo root is on `sys.path` (same class of failure as
ROB-1308's `scripts/call_durations.py`; see
`tests/ci/test_file_shard_plan_workflow_entrypoint.py`).

## 5. Where the exact-cover check runs

Exact cover — every file the authoritative
`pytest --collect-only -m "not live" tests/` run finds is in **exactly one**
manifest, no duplicates, no stale entries, no empty shard — is validated
**exactly once per `test.yml` run that needs it**, in one of two places:

- `taskiq-smoke` on a full run (main push, dispatch, `run_all` PR);
- `affected-tests` on a narrow PR that edits `ci_shards/shard-N.txt`.

A narrow PR cannot skip the check by forgetting the manifest: a changed test file
that no manifest lists selects nothing, which forces `run_all` and therefore
`taskiq-smoke`.

The check is not repeated in each of the four `test` matrix jobs. A fresh
full-suite collect-only measured ~68–71s per shard in CI, so running it four times
would burn back most of the time saved by moving off pytest-split's
duplicate-collection design.

The `test` matrix jobs themselves do not independently validate exact cover
— they trust the committed manifest and run it directly via `mapfile`. If a
manifest is badly broken (nonexistent file, empty), `pytest` itself will
fail loudly; the actionable diagnosis lives in the exact-cover step's output.

The weekly `test-durations-refresh.yml` performs its own, independent
version of the same guarantee — see §6 — and, unlike `test.yml`, *does* call
the planner's `check` directly, twice: once in `collect-authoritative`
(`needs: preflight` only, so it runs in parallel with the four `measure`
shards, not before them — the DAG is correct for wall-clock, not for
front-loading the check; a failure there fails the job and, via `merge`'s
`needs: [measure, collect-authoritative]`, blocks the merge/regenerate step
fail-closed even though the `measure` shards' own CI time was already
spent), and once in `merge` as a self-check on manifests it just
regenerated from that run's fresh `.test_durations` (§6.1).

## 6. The duration-refresh disjoint-shard contract

Before ROB-1312, all four `measure` shards in `test-durations-refresh.yml`
collected the **whole** suite (`pytest tests/ --collect-only ...`,
identical across shards by construction, since `--splits/--group` only
affected which tests actually *ran*) and `scripts/merge_test_durations.py`
enforced "all four collected-node manifests must be identical."

Now each shard runs only its own `ci_shards/shard-N.txt` manifest, so its own
collected-node capture is a disjoint *subset*, not a copy of the whole suite.
The contract changed to:

- each shard's own `durations` (+ `not_called`, for `.call_durations.json`)
  must equal **that shard's own collected-node manifest exactly** — a
  measurement for a node the shard did not collect is a **wrong-shard
  measurement**, rejected even if some other shard did collect that node;
- the four shards' collected-node sets must be **pairwise disjoint**;
- their union must equal a fifth, **independently captured** authoritative
  collection (`collect-authoritative` job, run once, in parallel with the
  four `measure` shards — never derived from the shards' own union, which
  would make a shard that quietly under-collects invisible).

`scripts/merge_test_durations.py` and `scripts/call_durations.py build` /
`validate` both take a required `--authoritative <path>` argument now.
`call_durations.py validate` checks a call-duration artifact's freshness
against that single authoritative collection only — unrelated to how the
artifact happened to be sharded when it was built.

Collected-node captures (both per-shard and authoritative, in both
workflows) are captured with `LC_ALL=C sort`, **never `sort -u`**: a
duplicated pytest collection is a real bug the parser must see and reject
(`ValueError: ... duplicate node id(s) in manifest ...`), not something a
shell pre-dedupe silently erases before it reaches the code that checks for
it.

### 6.1 Same-PR manifest regeneration (durability)

`ci_shards/*.txt` is a deterministic function of `.test_durations`
(weight source, §3) plus the current authoritative collection. If the weekly
refresh rebuilt `.test_durations` without also regenerating the manifests,
no duration-refresh workflow would correct the LPT balance. Exact cover would
still hold (file membership does not depend on weight), but the "keep predicted
shard spread low" purpose of the planner would quietly rot.

That rot is not hypothetical: the refresh's `pull-request` job has been
failing since 2026-10-26 (expired `DURATIONS_REFRESH_TOKEN`), so the committed
manifests were last regenerated on 2026-10-19 and had drifted to 49.65 %
predicted spread by 2026-11-03 while `weights.json` still said 0.0 (§3.3) —
three weeks of per-PR one-line additions (§4) with no rebalance.

So the `merge` job's "Regenerate file-shard manifests" step re-runs
`python3 -m scripts.ci.file_shard_plan generate` from that same run's
freshly-merged `.test_durations` and the `collect-authoritative`
capture, immediately self-checked by "Validate regenerated file-shard
manifests (exact-cover)". If anything changed (`git status --porcelain --
ci_shards/`), the refreshed `ci_shards/shard-{1..4}.txt` +
`ci_shards/weights.json` are staged as an artifact and installed by
`pull-request` alongside `.test_durations` and `.call_durations.json` —
one PR, `add-paths` covering all of it, not a separate easy-to-forget
follow-up. `tests/ci/test_file_shard_plan_workflow_entrypoint.py` locks the
`merge` job's `ci_shards_changed` output, the `pull-request` job's `if:`
condition, its `add-paths` list, and that the regenerate step is fed
`--durations .test_durations` rather than the call-phase artifact.

## 7. Verifying locally

```bash
uv run pytest tests/ci/test_file_shard_plan.py \
  scripts/merge_test_durations_test.py \
  scripts/call_durations_test.py \
  tests/ci/test_file_shard_plan_workflow_entrypoint.py -q -ra
```

Full-regeneration idempotence is locked by the unit tests listed above,
including `test_cli_generate_twice_is_byte_identical`. The duration-refresh
workflow's `Validate regenerated file-shard manifests (exact-cover)` step
separately confirms that the manifests it just wrote form an exact cover (§6.1).
Neither requires a local full regeneration.

`mapfile` (used to load a manifest into `pytest`'s argv in both workflows)
is a bash 4+ builtin, absent from macOS's stock bash 3.2 — local
verification of the exact shell snippet needs `bash -c '...'` with a
`while read` loop instead, or a modern bash from Homebrew. GitHub Actions'
`ubuntu-latest` runners ship bash 5.x, where `mapfile` works as written.

## 8. Explicitly unchanged

- Check names on a full run: `lint`, `taskiq-smoke`, `test (3.13, 1..4)`.
- Matrix shard count (4) and `-n 4` xdist workers per shard.
- PostgreSQL/Redis service definitions, coverage combine/upload, socket-guard
  evidence.
- `pytest-split` remains a dependency — `--store-durations`/
  `--durations-path`/`--clean-durations` still populate `.test_durations` in
  the weekly refresh. Only the **runtime shard-selection** use
  (`--splits`/`--group`) was removed; removing the dependency itself is out
  of scope.
