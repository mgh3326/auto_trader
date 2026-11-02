# PR gate and change classifier (ROB-1294, ROB-1454)

**Status: live.** A PR is checked on what it changed and merges on `lint` + `PR gate`.
The full suite runs on main, where the mergewake watch reverts a culprit, and has to be
green on the exact SHA before a deploy.

## 1. What runs where

| event | lint | tests | `test (3.13, 1..4)` + `taskiq-smoke` | `PR gate` |
|---|---|---|---|---|
| pull request, `run_all=false` | yes | `affected-tests`: the classifier's file list, if any | skipped | runs |
| pull request, `run_all=true` | yes | the shards | yes | runs |
| push to main or develop | yes | the shards | yes | runs |
| `workflow_dispatch` | yes | the shards | yes | runs |

Merging is not deploying: orders, ledger and migrations take effect at deploy. So
`blocking` paths in `.mergewake/policy.yaml` get no exception here. A broker change runs its
affected tests like any other path. That list still means two review passes and no
auto-revert.

### Force the full suite on a PR

```bash
gh workflow run test.yml --ref <branch>
```

This runs the full suite on the branch head and reports its checks on that commit. It does
not change the PR's own gate verdict, so read the dispatched run's result yourself before
merging.

### Deploy requires the full suite on the exact SHA

🔴 Do not deploy a SHA until all six full-suite checks are green on that SHA, in one run of
the `Test` workflow:

```bash
SHA=<deploy sha>
REPO=mgh3326/auto_trader
RUN=$(gh api "repos/$REPO/actions/workflows/test.yml/runs?head_sha=$SHA&per_page=100" \
  --jq '[.workflow_runs[] | select(.event == "push" or .event == "workflow_dispatch")]
        | sort_by(.run_started_at) | last | .id // empty')
GREEN=$(test -n "$RUN" && gh api --paginate "repos/$REPO/actions/runs/$RUN/jobs?filter=latest" \
  --jq '.jobs[] | select(.conclusion == "success") | .name')
FAILED=$(test -n "$RUN" && echo 0 || echo 1)
for NAME in lint taskiq-smoke 'test (3.13, 1)' 'test (3.13, 2)' 'test (3.13, 3)' 'test (3.13, 4)'; do
  grep -qxF "$NAME" <<<"$GREEN" || { echo "NOT GREEN: $NAME"; FAILED=1; }
done
test "$FAILED" = 0 && echo "FULL-SUITE-GREEN run=$RUN" || echo "DO NOT DEPLOY $SHA"
```

The command checks each name, so a rerun cannot make a duplicate entry stand in for a missing
check. It reads the latest attempt of the newest push or dispatch run. A `pull_request` run does
not count, because it tests a merge commit and not this SHA.

`DO NOT DEPLOY` means exactly that. A SHA that never ran the suite, such as a `production` merge
commit, needs `gh workflow run test.yml --ref production` first. A PR's green `PR gate` is not
this evidence, because the shards may have been skipped.

This is a manual step today. Enforcing it in the deploy path is hk task 2586.

## 2. Change classifier contract

Invoked as `python3 scripts/ci/classify_changes.py` (stdlib only, no
dependency install). It reads `$CLASSIFY_BASE_SHA` / `$CLASSIFY_HEAD_SHA` (or
`--base-sha` / `--head-sha`, or a `--name-status-file` for offline use) and
emits a JSON report plus `$GITHUB_OUTPUT` keys. The workflow reads `run_all`,
`run_affected_tests`, `check_manifests` and `affected_tests` (a JSON array).

The diff is `base...head`, three dots, so it holds what the PR changed.
`pull_request.base.sha` is the base branch tip, not the merge base, so a two-dot diff also
reports what main gained after the branch point. A file added on main then reads as a
delete and forces `run_all`.

There are exactly three outcomes, and none of them is "run less because
something went wrong":

| outcome | exit | when |
|---|---|---|
| `classified` | 0 | every changed path mapped to a known lane, and every change was an add or a modify |
| `run_all` | 0 | any unknown path, any rename/copy/delete/type-change/unmerged path, any shared CI/config/test-infrastructure file, an empty change set, an absent base SHA, a path that selects no test, or a selection over 500 files |
| `error` | **1** | a head SHA that was not supplied, a supplied SHA that stays unresolvable after `git fetch` (shallow/incomplete history, or an object that is not a commit), a failing `git` invocation, or a malformed `--name-status` record |

**Resolution order matters.** A supplied head is resolved *before* the
absent-base branch is taken. Otherwise an unresolvable head plus a missing or
all-zero base returned a conservative green `run_all`, making an invalid head
indistinguishable from a legitimate first-push one. The two cases must stay
distinct:

| base | head | outcome |
|---|---|---|
| absent / `0*40` | resolvable | `run_all`, exit 0 — a real first push |
| absent / `0*40` | unresolvable or not a commit | **`error`, exit 1** |
| present | unresolvable | **`error`, exit 1** |

(`--name-status-file` remains a pure offline path and resolves no SHA at all.)

Lanes and what each one runs on a PR:

| lane | example paths | selected tests |
|---|---|---|
| `docs` | `docs/**`, `*.md` | none |
| `frontend` | `frontend/**` | none; the unconditional `frontend` job covers it |
| `research` | `research/**`, `research_contracts/**` | references + mirror directory; none is fine, because research suites run in their own jobs |
| `app` | `app/**` | references + mirror directory; none forces `run_all` |
| `scripts` | `scripts/**` (except `scripts/ci/**`) | same |
| `migrations` | `alembic/**` | same |
| `config` | `config/**` | same |
| `tests` | `tests/**` | the file itself; a helper or nested `conftest.py` selects its directory and importers; none forces `run_all` |
| `manifests` | `ci_shards/shard-N.txt` | none, but runs the exact-cover check in `affected-tests` |
| `ci_shared` | `.github/**`, `scripts/ci/**`, `pyproject.toml`, `uv.lock`, `Makefile`, `.test_durations`, `tests/conftest.py`, `tests/_socket_guard*.py`, `env.example`, `scripts/setup-test-env.sh`, `alembic.ini`, `codecov.yml` | **forces `run_all`** |
| `unknown` | anything unmatched, including `.mergewake/**` and `ci_shards/weights.json` | **forces `run_all`** |

**How a path selects tests.** The universe is the union of `ci_shards/shard-*.txt`, exactly
the files the full suite runs. A changed path selects the files that name it (dotted module,
`from parent import name`, or repo path) plus the `tests/` directory that mirrors it:
`app/services/foo/bar.py` selects `tests/services/foo/bar/` and `tests/services/foo/`. The
selection does not follow imports transitively: on recorded merges that reached 1169 of 2024
files. A regression it misses reaches main, where the full suite catches it and the watch
reverts the change.

On the last 40 merges this gave 7 lint-only, 22 narrow (median 10 files, maximum 464) and 11
full-suite runs.

Design points that look over-conservative and are meant to be:

- A rename inside a single lane still forces `run_all`. The pre-image path is
  part of the blast radius and `--name-status` gives no guarantee the two
  sides belong to the same lane's test surface.
- The error path still writes `run_all=true` into `$GITHUB_OUTPUT` before
  exiting 1, so a consumer reading a partially written output file cannot
  infer reduced coverage from a crashed classifier.
- **Paths are matched literally.** Nothing is stripped or normalized first.
  Git tracks `" docs/only.md"` (leading space) as a file in a directory named
  `" docs"`, and it is emitted verbatim under `--name-status -z`; trimming it
  would answer "docs-only, no jobs needed" for a path that is not under
  `docs/` at all. Unmatched literals are `unknown` → `run_all`.
- **The `*.md` suffix rule is top-level only.** It exists for `README.md`,
  `CLAUDE.md`, `AGENTS.md`. A bare suffix rule would swallow every unmatched
  *nested* markdown path — including a fixture some test reads — and answer
  "docs". Nested markdown under a directory the prefix table does not know is
  `unknown` → `run_all`.

### NUL stream and status grammar

`--name-status -z` output is parsed strictly, because a parser that
resynchronises quietly is a parser that reports the wrong lane confidently:

- The stream must be NUL-terminated; a truncated one is red.
- Exactly one trailing empty field (the terminator) is permitted. Any *other*
  empty field means an embedded NUL, so record boundaries cannot be trusted →
  red. (Previously such fields were filtered out, silently re-pairing statuses
  with the wrong paths.)
- The status token is validated as a whole *before* its first character is
  used: a single letter from `A C D M R T U X B`, optionally followed by a
  1–3 digit score for `R`, `C` and `M`. Anything else (`AA`, `Z`, `A1`,
  `R1000`, `m`) is red rather than truncated into a status git never emitted.
- A **scored `M`** (`M100`) is git's dissimilarity score for a *rewrite*,
  emitted under `-B`. It is valid git output, so it is not red — but it is not
  an ordinary modify either, so the whole token is kept, which places it
  outside `CLASSIFIABLE_STATUSES` and forces `run_all`.

## 3. `PR gate` contract

`PR gate` declares `if: always()` and
`needs: [lint, change-classifier, affected-tests, test, taskiq-smoke]`. `always()` is
load-bearing: a job with no `if:` is *skipped* when a `needs:` child fails, and mergewake
counts a skipped required check as unknown.

`scripts/ci/aggregate_required.py --pr-gate` evaluates `toJSON(needs)`. The classifier's own
outputs decide which skips are legitimate:

| run | may be skipped |
|---|---|
| not a pull request | `affected-tests` |
| PR, `run_all=true` | `affected-tests` |
| PR, narrow, tests selected or manifests changed | `test`, `taskiq-smoke` |
| PR, narrow, nothing to test | `test`, `taskiq-smoke`, `affected-tests` |

Everything else must report `success`:

| child result | verdict |
|---|---|
| `success` | pass |
| `skipped` | pass **only** when the table above allows it; red otherwise |
| `failure` | red |
| `cancelled` | red |
| absent from the payload | red (`missing`) |
| any other string, `""`, `null`, a missing `result` key | red (`unexpected`) |
| present in the payload but not `--required` | red (`undeclared`) |

Malformed input is red: non-JSON, a non-object payload, an unset env var, a child whose value
is neither a string nor an object with `result`, or classifier outputs that are not exactly
`true` or `false`. A non-PR run ignores a narrow verdict, so main can never skip into green.

The declaration is validated before the payload is read: an empty `--required` list, a blank
or duplicated name, an `--authorize-skip` name that is not required, or `--pr-gate` combined
with `--authorize-skip` is red.

The `test` matrix collapses into one `needs.test.result`, which is `failure` if any shard
failed.

## 4. Verifying

```bash
uv run pytest tests/ci/ -q -ra
```

`tests/ci/test_pr_gate_workflow_contract.py` pins the `if:` on the full-suite jobs, the gate's
`needs:` and `--required` lists, and that every check named in `.mergewake/policy.yaml` is one
the workflow displays. `tests/ci/test_required_aggregator.py` holds the gate's truth table.

## 5. Branch protection (operator-only)

GitHub's required list is separate from `.mergewake/policy.yaml` and only the operator desk
edits it. It must be exactly:

```
lint
PR gate
```

A skipped matrix job does not report `test (3.13, N)`, so a narrow PR cannot merge while those
names are required. The edit follows the landing of ROB-1454, not the other way round: `PR gate`
does not report until the workflow is on main. An open PR reports `PR gate` only after it merges
main. Keep `lint` required throughout so the enforced set is never empty.
