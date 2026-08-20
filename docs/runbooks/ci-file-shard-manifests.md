# Deterministic file-shard manifests + exact-cover guard (ROB-1312)

**Status: live.** The four core `test (3.13, N)` matrix jobs and the weekly
duration-refresh workflow both run committed file manifests instead of a
runtime `pytest-split --splits/--group` selection. Branch protection is
unchanged — the required check names and count are identical to before this
change.

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
| planner/validator | `scripts/ci/file_shard_plan.py` | `generate` (duration refresh만 전면 재생성) and `check` (validate exact cover) |
| manifests | `ci_shards/shard-{1..4}.txt` | committed, one test file path per line, sorted |
| audit report | `ci_shards/weights.json` | predicted per-shard weight/spread, not consumed by `check` |
| duration merge contract | `scripts/merge_test_durations.py`, `scripts/call_durations.py` | disjoint-shard-collection validation for the weekly refresh |
| workflow wiring | `.github/workflows/test.yml` (`test`, `taskiq-smoke`), `.github/workflows/test-durations-refresh.yml` (`collect-authoritative`, `measure`, `merge`) | consumes the manifests |

## 3. Weight model and the LPT algorithm

`generate` sums per-file weight from `.call_durations.json` (ROB-1295
call-phase telemetry) and assigns files to shards via a deterministic
largest-processing-time-first (LPT) bin-pack:

- **Measured node**: its `durations[node_id]` value.
- **`not_called` node** (its `setup` phase skipped before `call`): weight
  `0.0` — there is no call-phase cost to attribute, by construction
  (`NOT_CALLED_FALLBACK_SECONDS`).
- **Unmeasured node** (collected today, absent from the artifact — a new
  test added since the last refresh, or drift): weight equal to the **mean
  of every measured duration in that same artifact** — a deterministic,
  reproducible function of the committed input, not a hand-tuned constant
  (`_unmeasured_fallback_seconds`). If the artifact has zero measured
  durations (empty/bootstrap), the fallback is
  `UNMEASURED_FALLBACK_DEFAULT_SECONDS` (`1.0`).
- File weight = sum of its nodes' weights, accumulated by iterating the
  authoritative node list **in sorted order** — float summation order is
  fixed, so regenerating from the same inputs is byte-for-byte identical.

Assignment: files are visited in `(-weight, path)` order (largest first, path
tie-break); each file goes to whichever shard currently has the smallest
running total, ties broken by the lowest shard index. Both tie-breaks are
encoded directly in sort/min keys — determinism does not depend on
dict/set iteration order or Python version.

`ci_shards/weights.json`'s `max_min_spread_pct` is
**`(max_total - min_total) / min_total * 100`** — deliberately divided by the
**smaller** total, not the larger. Dividing by the larger number understates
the imbalance (e.g. `max=110, min=90` reads as `18.2%` via `/max` but
`22.2%` via `/min`), which could pass a real >10% imbalance under a naive
reading of the acceptance bar. `weights.json` is an audit artifact only —
`check` does not read or depend on it.

## 4. Normal test-file change: minimal manifest entry

Any normal PR that adds a `tests/**/*.py` file must make the corresponding
**minimal** change in the same PR: add that path in exactly one
`ci_shards/shard-N.txt`, at its `LC_ALL=C` sorted position. For a removal,
delete its existing entry; for a rename, do that deletion plus one sorted
addition. Do not run `file_shard_plan generate` for these cases.

`generate` has no incremental mode. It rebuilds LPT assignment from scratch
using `.call_durations.json` plus a fresh collection and overwrites every
manifest. If the local duration artifact differs from the one that produced
the committed manifests, a one-file add can move the whole suite (observed:
`+1184/-1183`). Exact-cover checks membership exactly once; it does **not**
check weight balance. Therefore the minimal entry is sufficient for normal
test-file work and avoids unrelated duration-refresh churn.

Choose the existing shard with the smallest current total weight when that
measurement is available; otherwise use the shard with the fewest entries
and state that basis in the PR. Then verify the exact cover:

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

### 4.1 Duration refresh: full regeneration only

The weekly duration-refresh workflow is the only context that runs
`python3 -m scripts.ci.file_shard_plan generate`. It has the fresh
`.call_durations.json` and authoritative collection from the same workflow,
so it intentionally rewrites all manifests and `weights.json` together.

### 4.2 Provenance guard: coherence, not correspondence

Before computing any weight, `generate` calls
`scripts/call_durations.py::validate_artifact_provenance` on the loaded
`.call_durations.json`, which requires:

- `source_commit_sha` is a non-empty, non-whitespace string;
- `collection_hash` is a string equal to
  `compute_collection_hash(durations.keys() | not_called)` — i.e. it
  actually hashes *this artifact's own* recorded node set.

This is a **coherence** check (do the artifact's fields agree with each
other?), not a **correspondence** check (does `source_commit_sha` actually
name the tree that was measured?). No predicate computed purely from the
artifact's own content can answer the second question — a tamperer who
edits `durations` can equally edit `source_commit_sha` and recompute
`collection_hash` to match. Correspondence requires an expectation from an
authority *outside* the artifact, which is exactly what
`validate_freshness`'s `expected_source_commit_sha`/`collected_nodes`
parameters provide for the one caller that has it (the weekly refresh,
comparing against the `github.sha` it just measured — see §6). The
provenance guard is **not** chained into `validate_freshness`, since
`validate_freshness` already strictly implies it.

Deliberately absent, by design (§6): comparing `source_commit_sha` against
today's HEAD, comparing `collection_hash` against today's authoritative
collection, or requiring the artifact's own node set to equal today's
collection. A stale-but-internally-consistent artifact (correct
`source_commit_sha` for *some* past tree, `collection_hash` that correctly
describes its own — outdated — `durations`/`not_called`) is valid `generate`
input; that staleness is what the mean-duration fallback above exists to
absorb. What this guard *does* catch: a blank/whitespace/non-string
`source_commit_sha`, and a `collection_hash` that is empty, non-string, or
does not actually hash the artifact's own content (e.g. a leftover
placeholder, a copy-paste from a different artifact, or accidental
hand-editing) — any of which previously let `generate` silently produce a
manifest from data with no real provenance behind it.

Threat model: accidental corruption and hand-editing of a *committed file
in a branch-protected repo* — not an adversarial telemetry feed. There is
no signature to verify and none is warranted. `check` never reads
`.call_durations.json` at all, so none of this guards the runtime CI
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
**exactly once per `test.yml` run**, inside the `taskiq-smoke` job (steps
"Record authoritative test collection" / "Validate file-shard manifests
(exact-cover)"), not redundantly in each of the four `test` matrix jobs.

Two reasons this lives in `taskiq-smoke` specifically, not a new job:

1. **No new required check.** `taskiq-smoke` is already one of the six
   required checks named directly in branch protection
   (`lint`, `taskiq-smoke`, `test (3.13, 1..4)`). A failure here fails a
   required check for real — it cannot be green-merged past. A *new*,
   non-required job doing this check would let a PR merge with a broken
   manifest as long as the six existing required checks stayed green,
   which is not fail-closed.
2. **Avoiding the very redundancy this change removes.** A fresh full-suite
   collect-only measured ~68–71s per shard in CI. Running it in all four
   `test` matrix jobs would burn back most of the time saved by moving off
   pytest-split's duplicate-collection design. `taskiq-smoke` already runs
   `uv sync --group test` + env setup for its own purposes and does not
   already collect the suite, so the marginal cost is one collect-only, once.

The `test` matrix jobs themselves do not independently validate exact cover
— they trust the committed manifest and run it directly via `mapfile`. If a
manifest is badly broken (nonexistent file, empty), `pytest` itself will
fail loudly; the actionable diagnosis lives in `taskiq-smoke`'s output.

The weekly `test-durations-refresh.yml` performs its own, independent
version of the same guarantee — see §6 — and, unlike `test.yml`, *does* call
the planner's `check` directly, twice: once in `collect-authoritative`
(`needs: preflight` only, so it runs in parallel with the four `measure`
shards, not before them — the DAG is correct for wall-clock, not for
front-loading the check; a failure there fails the job and, via `merge`'s
`needs: [measure, collect-authoritative]`, blocks the merge/regenerate step
fail-closed even though the `measure` shards' own CI time was already
spent), and once in `merge` as a self-check on manifests it just
regenerated from that run's fresh `.call_durations.json` (§6.1).

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

`ci_shards/*.txt` is a deterministic function of `.call_durations.json`
(weight source, §3) plus the current authoritative collection. If the
weekly refresh only rebuilt `.call_durations.json` without also
regenerating the manifests from it, LPT balance would silently drift out of
sync with reality every week between scheduled duration-refresh `generate`
runs — exact cover
would still hold (file membership doesn't depend on weight), but the "keep
predicted shard spread low" purpose this planner exists for would quietly
rot with nobody noticing.

So the `merge` job's "Regenerate file-shard manifests" step re-runs
`python3 -m scripts.ci.file_shard_plan generate` from that same run's
freshly-built `.call_durations.json` and the `collect-authoritative`
capture, immediately self-checked by "Validate regenerated file-shard
manifests (exact-cover)". If anything changed (`git status --porcelain --
ci_shards/`), the refreshed `ci_shards/shard-{1..4}.txt` +
`ci_shards/weights.json` are staged as an artifact and installed by
`pull-request` alongside `.test_durations` and `.call_durations.json` —
one PR, `add-paths` covering all of it, not a separate easy-to-forget
follow-up. `tests/ci/test_file_shard_plan_workflow_entrypoint.py` locks the
`merge` job's `ci_shards_changed` output, the `pull-request` job's `if:`
condition, and its `add-paths` list.

## 7. Verifying locally

```bash
uv run pytest tests/ci/test_file_shard_plan.py \
  scripts/merge_test_durations_test.py \
  scripts/call_durations_test.py \
  tests/ci/test_file_shard_plan_workflow_entrypoint.py -q -ra

# Duration-refresh maintainers only: regenerate TWICE from the same inputs
# and diff the two runs directly (not against committed manifests):
python3 -m scripts.ci.file_shard_plan generate --call-durations .call_durations.json \
  --collected /tmp/collected.txt --manifest-dir /tmp/ci_shards_run1 --shard-count 4
python3 -m scripts.ci.file_shard_plan generate --call-durations .call_durations.json \
  --collected /tmp/collected.txt --manifest-dir /tmp/ci_shards_run2 --shard-count 4
diff -r /tmp/ci_shards_run1 /tmp/ci_shards_run2   # must be empty

# Duration-refresh maintainers only: then confirm the regenerated manifests
# match the fresh duration-refresh inputs. Normal PRs must use §4's
# minimal-entry + `check` procedure instead:
python3 -m scripts.ci.file_shard_plan generate --call-durations .call_durations.json \
  --collected /tmp/collected.txt --manifest-dir ci_shards --shard-count 4
git diff --exit-code -- ci_shards/
```

`mapfile` (used to load a manifest into `pytest`'s argv in both workflows)
is a bash 4+ builtin, absent from macOS's stock bash 3.2 — local
verification of the exact shell snippet needs `bash -c '...'` with a
`while read` loop instead, or a modern bash from Homebrew. GitHub Actions'
`ubuntu-latest` runners ship bash 5.x, where `mapfile` works as written.

## 8. Explicitly unchanged

- Required check names/count: `lint`, `taskiq-smoke`, `test (3.13, 1..4)`.
- Matrix shard count (4) and `-n 4` xdist workers per shard.
- PostgreSQL/Redis service definitions, coverage combine/upload, socket-guard
  evidence.
- `pytest-split` remains a dependency — `--store-durations`/
  `--durations-path`/`--clean-durations` still populate `.test_durations` in
  the weekly refresh. Only the **runtime shard-selection** use
  (`--splits`/`--group`) was removed; removing the dependency itself is out
  of scope.
- `ci-required`/`change-classifier` shadow aggregator (ROB-1294): untouched,
  still not a required check.
