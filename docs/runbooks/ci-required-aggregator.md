# PR gate and change classifier (ROB-1294, ROB-1454, hk 2939)

**Status: live.** A PR is checked on what it changed and merges on `lint` + `PR gate`.
The full suite runs on main, where the mergewake watch reverts a culprit, and has to be
green on the exact SHA before a deploy.

## 0. Three tiers (hk 2939, operator decision 2026-11-05)

| tier | when | what | merge blocked? |
|---|---|---|---|
| T-PR | `pull_request` | `lint`, `change-classifier`, `affected-tests`, `PR gate`; the shards and `taskiq-smoke` only when the classifier says `run_all`; `frontend` only when the PR touches `frontend/` | `lint` + `PR gate` + review |
| T-POST | push to main/develop, `workflow_dispatch` | the full suite (`lint`, `taskiq-smoke`, `test (3.13, 1..4)`), plus `security`, `alpaca-track-fast-tests`, `intraday-harness-v2-tests`, `kiwoom-dual-surface-smoke`, `alpaca-track-walkforward-tests`, `frontend` | no; red means the mergewake watch bisects and reverts |
| T-DEPLOY | before an image or a forward deploy | `scripts/deploy_ci_gate.py`: the exact SHA's full suite must be green | the deploy |

`security` and the four research/smoke jobs are not part of any required or watched check, so
moving them off the PR costs no gate. Their failures show on the main run.

### Only the newest main head runs

`concurrency` groups a PR by its number and a push by its ref, and cancels in progress for
both. A newer push to main cancels the run of the previous push, so T-POST tests the newest
head only. A `workflow_dispatch` run is a deliberate request for one SHA (the deploy gate, a
mergewake bisect), so its group includes `github.run_id` and `cancel-in-progress` is false:
a later push never cancels it.

A cancelled main run is **not** evidence about its SHA. Consequences:

- `PR gate` of a run whose only non-passing children are `cancelled` is **skipped**, not red
  (§3, "Superseded runs"). mergewake reads a skipped check as unknown and holds.
- `deploy.yml`'s automatic `workflow_run` build skips a `Test` run whose conclusion is
  `cancelled` (the job is skipped, not failed). Manual dispatch and release builds are
  unchanged, and so is the exact-SHA gate.
- A superseded run usually leaves its full-suite jobs cancelled, and then its SHA has no
  full-suite evidence. The exact-SHA gate (`scripts/deploy_ci_gate.py`) and the §1 command read
  the six full-suite **job** conclusions, not the run conclusion: if all six finished green before
  the newer push cancelled the run, they count and the SHA passes. The automatic image build
  still skips that run (it reads the run conclusion), so build it by manual dispatch.
- 🔴 To deploy or build a SHA whose full-suite jobs were cancelled, run the suite on it first:

  ```bash
  gh workflow run test.yml --ref <sha>
  ```

  Dispatch takes a branch or tag ref; for a bare commit SHA push a throwaway branch at it and
  dispatch that ref. The exact-SHA gate accepts the newest push or dispatch run for the SHA.

### Runner selection

Every job that can run on push or dispatch declares

```yaml
runs-on: ${{ github.event_name != 'pull_request' && vars.AUTO_TRADER_POST_LINUX_RUNNER || 'ubuntu-latest' }}
```

With the repository variable `AUTO_TRADER_POST_LINUX_RUNNER` unset, everything runs on
`ubuntu-latest` as before. The desk sets the variable (Settings → Secrets and variables →
Actions → Variables) to the label of a Linux self-hosted runner to move T-POST there. The
variable is one runner label; it has no effect on a pull request.

🔴 The repository is public. A `pull_request` run executes unmerged code, so no job reachable
from `pull_request` may resolve `runs-on` to a variable or a `self-hosted` label. The
`github.event_name != 'pull_request'` guard in every `runs-on` keeps this repository's own
workflow on hosted runners for a PR, and `tests/ci/test_pr_gate_workflow_contract.py` evaluates
every job's `runs-on` on a pull request with the variable set.

🔴 The guard is a **routing default, not a security boundary.** A `pull_request` run reads the
workflow files from the PR head, so a PR can delete the guard, write `runs-on: self-hosted`, or
add a new workflow file. Before the desk sets `AUTO_TRADER_POST_LINUX_RUNNER`, enforce the
boundary outside the workflow files and confirm each item:

1. Actions settings: require approval for workflow runs from all outside contributors (fork
   PRs), so no fork workflow starts without a maintainer.
2. Runner settings: the self-hosted runner (or its runner group) must not run jobs for this
   repository's `pull_request` events. If the runner group cannot express that, do not register
   the runner on this public repository.
3. Optionally put the T-POST jobs behind a protected environment whose deployment branch rule
   allows only the default branch, so a `pull_request` run cannot start them.

Until all of these hold, leave the variable unset; T-POST then runs on `ubuntu-latest`. PR-only jobs (`affected-tests`, `write-fence-guard`) keep the literal
`ubuntu-latest`. There is no macOS job: the service runs on Linux.

The `test` shards and `taskiq-smoke` declare `services:` (Postgres, Redis), which need
**Docker on the runner**. A self-hosted Linux runner must have
Docker, one concurrent job per runner, and no other workload that binds ports 5432 or 6379.

## 1. What runs where

| event | lint | tests | `test (3.13, 1..4)` + `taskiq-smoke` | `PR gate` |
|---|---|---|---|---|
| pull request, `run_all=false` | yes | `affected-tests`: the classifier's file list, if any | skipped | runs |
| pull request, `run_all=true` | yes | the shards | yes | runs |
| push to main or develop | yes | the shards | yes | runs, or skipped when superseded |
| `workflow_dispatch` | yes | the shards | yes | runs, or skipped when superseded |

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

A SHA whose main run was superseded (cancelled by a newer push) has full-suite evidence only if
all six jobs above finished green before the cancel; the command checks job conclusions, not the
run conclusion. If any of them was cancelled it prints `DO NOT DEPLOY`; dispatch the suite on the
SHA first (see §0).

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
| `frontend` | `frontend/**` | none; the `frontend` job runs on a PR when this lane is present (its `if:` reads the classifier's `lanes` output) |
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

Two jobs carry the verdict. `gate-verdict` declares `if: always()` and
`needs: [lint, change-classifier, affected-tests, test, taskiq-smoke]`, runs
`scripts/ci/aggregate_required.py --pr-gate --allow-superseded` on `toJSON(needs)`, and
publishes `superseded`. `PR gate` (the check name mergewake and branch protection know)
needs only `gate-verdict` and goes red unless its result is `success`. `always()` is
load-bearing on both: a job with no `if:` is *skipped* when a `needs:` child fails, and
mergewake counts a skipped required check as unknown.

The classifier's own outputs decide which skips are legitimate:

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

### Superseded runs (T-POST only)

hk 2924 rule: a cancellation is not a failure. On `push` and `workflow_dispatch`, when every
non-passing required child is `cancelled` (no failure, no missing, no unauthorized skip, no
undeclared or unexpected result), `aggregate_required.py` reports `superseded`, exits 0 and
writes `superseded=true` to `$GITHUB_OUTPUT`. `gate-verdict` then succeeds and **`PR gate` is
skipped** (the verdict chosen over an explicit "superseded" success, because a success would
read as green). `PR gate` is also skipped when `gate-verdict` itself was cancelled with its
run. A failure next to cancellations is still red.

| event | only `cancelled` children | `PR gate` |
|---|---|---|
| `push`, `workflow_dispatch` | superseded | skipped (unknown to mergewake, which holds) |
| `push`, `workflow_dispatch` | plus any failure, missing child, unauthorized skip | red |
| `pull_request` | any cancelled child | **red**, unchanged |

🔴 On `pull_request` the rule is never applied: `PR gate` is a required check, and GitHub
treats a skipped required check as passing, so a skipped gate would be a green merge button.
The flag is ignored for that event inside the script, and the workflow's `if:` on `PR gate`
has no skip branch for it.

## 4. Verifying

```bash
uv run pytest tests/ci/ -q -ra
```

`tests/ci/test_pr_gate_workflow_contract.py` pins the `if:` on the full-suite jobs, the
verdict job's `needs:` and `--required` lists, the tiers (which jobs a pull request can reach,
the concurrency groups, `runs-on` on every event with and without the runner variable) and that
every check named in `.mergewake/policy.yaml` is one the workflow displays; it evaluates the
workflow expressions with `tests/ci/gh_expr.py`. `tests/ci/test_required_aggregator.py` holds
the gate's truth table and the superseded rule. `tests/ci/test_deploy_gate_workflow.py` pins
that `deploy.yml` skips a cancelled `Test` run.

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
