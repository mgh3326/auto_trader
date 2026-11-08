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

## 0a. Main tier verdict `ci-ok` and the deploy tier (hk 3445, decision 2026-11-07)

`hk:doc decision/2026-11-07/fleet-ci-three-tiers-r2` keeps the PR tier as it is, makes the
main tier (T-POST above) end in one verdict, and adds a heavy **deploy tier** that runs
once the main tier is green. This change only *publishes* the two verdicts. The deploy path
still reads the exact-SHA gate below (`scripts/deploy_ci_gate.py`, unchanged); nothing reads
`ci-ok` or `deploy-ok` until mergewake's `main_checks` / `deploy_checks` switch lands.

| tier | workflow | when | cancelled by a newer head? | verdict |
|---|---|---|---|---|
| PR | `test.yml` | `pull_request` | yes | `lint` + `PR gate` (unchanged) |
| main | `test.yml` | push to main/develop, dispatch | yes | `ci-ok` |
| deploy | `deploy.yml` -> `deploy-checks.yml` | after a **successful** main-push `Test` run | **never**; one run per repository, GitHub keeps the newest pending run | `deploy-ok` |

### Main tier and `ci-ok`

Nothing moved: the main tier is still every job that ran on a main push, with the six gate
jobs (`lint`, `taskiq-smoke`, `test (3.13, 1..4)`) and their full-suite `if:` untouched. The
move rule was: a job leaves the main tier only if it is none of the six gate jobs, is not a
basic check (lint, types, security, frontend build) and the measured main wall time is above
ten minutes. Measured on the last three green main runs, the wall time was 616 s, 577 s and
593 s (median 593 s, two of three at or under ten minutes). The longest job in each run was a
gate job (`test (3.13, 3)` 586 s, `test (3.13, 3)` 527 s, `test (3.13, 1)` 498 s). The only jobs that
could move (`alpaca-track-walkforward-tests` 248-266 s, `alpaca-track-fast-tests`,
`intraday-harness-v2-tests`, `kiwoom-dual-surface-smoke`) finish long before the shards, so moving
them would not shorten the wall time. The decision also keeps regression mutants on main. If main grows past ten minutes, measure again and
move a non-gate job to `deploy-checks.yml`; record the move and its measured time in the PR.

`ci-ok` (job name `ci-ok`, `test.yml`) needs every main-tier job: `lint`, `change-classifier`,
`test`, `taskiq-smoke`, `security`, `alpaca-track-fast-tests`, `intraday-harness-v2-tests`,
`kiwoom-dual-surface-smoke`, `alpaca-track-walkforward-tests`, `frontend`. It is not listed in
the PR path: `PR gate`, the classifier and `affected-tests` are unchanged, and `ci-ok` never runs
on a `pull_request`. The rule is the one from `auto_trader-operator` `ci-post.yml` (hk 2924):

| results of the needed jobs | `ci-ok` |
|---|---|
| all `success` | runs, green |
| any `failure` (or a skipped / unknown result) | runs, red |
| only `success` and `cancelled` (a newer push superseded the run) | **skipped**, so the check stays unknown for that head |

A cancelled main run is "unknown", not green and not red (§0, "Only the newest main head runs").

### Deploy tier

`deploy.yml` calls `deploy-checks.yml` after `build-and-push`, for the same SHA, **only** when
the triggering `Test` run's conclusion is `success` (a cancelled or red run gets no deploy tier)
and passes the digest `build-and-push` pushed (`ghcr.io/<repo>@sha256:...`). Nothing is rebuilt.
`build-and-push`, its tags, `promote-moving-tags` and `notify` behave as before and none of them
waits for the deploy tier; a red deploy tier never unpublishes or untags an image.

| job | what it checks |
|---|---|
| `plan` | resolves the candidate SHA and the deployed-schema base (below) |
| `migration-path` | TimescaleDB service (`timescale/timescaledb:2.22.1-pg17`, the image `dev-kit.md` §3 pins; the migrations need the extension and fail on plain postgres): `alembic upgrade head` at the base checkout, then the candidate's `alembic upgrade head` on the same database |
| `install-smoke` | `docker run` the image digest against the CI TimescaleDB and Redis services with placeholder settings (no secrets): `alembic upgrade head` from the image, `uvicorn app.main:app` answers `/healthz` 200 within 30 attempts x 2 s, the scheduler, TaskIQ worker and `websocket_monitor.py` entrypoints import; the last 60 container log lines print on failure |
| `deploy-ok` | posts the `deploy-ok` commit status and uploads the evidence artifact |

**Base revision.** The pinned commit production runs, `DEPLOYED_BASE_SHA` in `deploy-checks.yml`
(`0c416a3df`, deploy record `deploy/auto_trader/20261106T230040Z`; `scripts/ci/select_deploy_base.py`).
`deploy-ok` never moves it: that status says CI validated an image, and production deploys are run by an
operator and publish nothing GitHub can read. Move the pin only in a PR that names the deploy record of
the new production commit, never to make a run green. A pin older than production still runs every
migration production has not applied. A pin that is not a commit, or not an ancestor of the candidate,
fails the run; it never falls back to the candidate itself.

**Trusted helpers.** `plan` and `deploy-ok` run `select_deploy_base.py` and `deploy_ok.py` from a sparse
checkout of the workflow revision (`github.sha`, path `.deploy-tier-tools`), not from the candidate. The
candidate may predate the helpers, and `deploy-ok` holds `statuses: write`, so candidate code never runs
with it.

**`deploy-ok`** (`scripts/ci/deploy_ok.py`, `statuses: write` on that job only) is `success` only when
`plan`, `migration-path` and `install-smoke` all reported `success`; anything else (failure, cancelled,
skipped, absent) is `failure`, with the run URL as the target. It is posted only for a commit that is an
ancestor of `origin/main`. The evidence artifact `deploy-evidence` (JSON) holds `repo`, `sha`,
`policy_revision` (git blob sha of `.mergewake/policy.yaml` at that SHA, `absent` if none), `suite`
(`deploy`), `environment`, `run_id`, `result` and `image_digest`.

**Any ref.** `gh workflow run deploy-checks.yml --ref <branch> -f ref=<ref> -f suite=deploy` (the only
suite) builds the image locally (load, no push, no registry login, no cache write) and runs `plan`,
`migration-path` and `install-smoke` on that ref. It posts `deploy-ok` only if the ref resolves to a main
ancestor and writes the evidence either way. `deploy.yml`'s own dispatch (`allow_unverified`) is unchanged.
`workflow_dispatch` only registers once the workflow file is on the default branch.

**Concurrency.** `deploy-tier-<repo>` with `cancel-in-progress: false`, set once in `deploy-checks.yml`.
Do not add a group with the same name to the calling job in `deploy.yml`: a caller and a callee that share a
group deadlock.

### Only the newest main head runs

`concurrency` groups a PR by its number and a push by its ref, and cancels in progress for
both. A newer push to main cancels the run of the previous push, so T-POST tests the newest
head only. A `workflow_dispatch` run is a deliberate request for one SHA (the deploy gate, a
mergewake bisect), so its group includes `github.run_id` and `github.run_attempt`: a later push
never shares its group, and a re-run never cancels the attempt it re-runs.

The concurrency table, before and after hk 3487 (`cancel-in-progress` became the literal `true`,
because mergewake `ci-policy` cannot verify an expression; the groups are the same except the
dispatch key now carries the attempt):

| Event | Group (before) | Cancels (before) | Group (after) | Cancels (after) |
|---|---|---|---|---|
| `push` to main | `Test-refs/heads/main` | older main push runs | same | same |
| `push` to develop | `Test-refs/heads/develop` | older develop push runs | same | same |
| `pull_request` #N | `Test-<N>` | older runs of PR N | same | same |
| `workflow_dispatch` run R | `Test-dispatch-<R>` (`cancel-in-progress: false`) | nothing; a re-run of R queued behind the running attempt | `Test-dispatch-<R>-<A>` (literal `true`) | nothing: no other run shares the group; a re-run is attempt A+1 with its own group, so it runs beside A and does not cancel it |
| `workflow_call` from `test-any-ref.yml` run R, attempt A | (did not exist) | — | `Test-dispatch-<R>-<A>` (the event is `workflow_dispatch`) | nothing (as for dispatch) |

The one visible change: a re-run of a dispatch no longer waits for the running attempt of the same
run; the two attempts run side by side. No push, PR or other dispatch is affected.

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

### Any-ref CI for a cancelled main commit (hk 3487)

A main commit whose push run a newer push cancelled has no watched result. When main is red,
mergewake's bisect needs a result for each commit it reads, so it dispatches
`.github/workflows/test-any-ref.yml` for that commit (`policy.any_ref_workflow` in
`.mergewake/policy.yaml`). Two workflows and one artifact do the work:

1. **`test-any-ref.yml`** (`Test (any ref)`, `workflow_dispatch`, input `ref` only):
   - `validate` (first job, first steps): `ref` must be exactly 40 hex characters, and
     `compare/main...<ref>` must report `behind` or `identical`. Anything else stops the run before
     a checkout.
   - `suite` calls `test.yml` with `ref`. Every checkout in `test.yml` reads `inputs.ref`, so the
     jobs are the push run's jobs on that exact commit. Their names become `suite / <job>`; `if:`
     conditions are unchanged. Secrets are inherited.
   - `record` (after `validate`): uploads the artifact `any-ref-target` with `sha.txt` (the commit)
     and `expected-checks.txt` (the watched names this run must have published). It holds no
     statuses permission.
2. **`post-any-ref-status.yml`** (`Post any-ref statuses`, `workflow_run` of `Test (any ref)`,
   completed): this is the only file that can post statuses (`statuses: write` on its `post` job).
   - GitHub loads a `workflow_run` workflow from the **default branch**, whichever branch the
     triggering run used. The code that holds `statuses: write` is therefore main's, and a branch
     cannot change it.
   - It posts only when the triggering run was a `workflow_dispatch` with `head_branch == main`, from
     this repository. A dispatch from any other branch runs that branch's files, and its
     `head_branch` is that branch, so it is skipped.
   - It checks out the default branch by name, downloads `any-ref-target`, refuses a non-SHA, and
     refuses if the recorded check names differ from the names it publishes. Then
     `.github/scripts/any_ref_statuses.py` judges each watched check by its job (`suite / lint`,
     `suite / taskiq-smoke`, `suite / test (3.13, N)`): one completed job with conclusion `success` is
     green, anything else red, and it posts one commit status per name on the commit.

Why the split: before hk 3487's review, the statuses were posted by a job in the dispatched file. A
dispatch from a branch runs that branch's files, so such a job could be given `statuses: write` by
the branch and post green statuses on a main ancestor. Now no branch-run workflow holds
`statuses: write`; the only place that can is a default-branch workflow that refuses every run not
dispatched from main.

What the any-ref path does **not** do, by design:

- It posts no image, no `deploy-ok`, no `ci-ok`, and neither workflow is named `Test`. `deploy.yml`
  builds only on a `Test` `workflow_run` whose event is `push`, and `scripts/deploy_ci_gate.py` reads
  only `Test` runs. A called workflow does not create a `Test` run of its own; after the first
  dispatch, confirm it (`gh api repos/<repo>/actions/workflows/test.yml/runs?head_sha=<sha>` lists only
  the runs that existed before the dispatch).
- The poster's job name is not a watched check, so a same-name check run and status cannot be read as
  ambiguous. The called jobs appear as `suite / <name>`.
- A cancelled watched check plus a `success`/`failure` status of the same name reads as that status
  (`internal/github/client.go` `foldChecks`), so a status beats the cancelled push run's check.
- If the validation did not pass, nothing is recorded and nothing is posted. Mergewake then holds the
  commit as unknown, which is the safe outcome.

Known limit: the called `test.yml` is the **default branch's** definition, while the code under test
is the commit's. For a commit whose own `test.yml` differed, the job definitions are main's. The
watched names and job bodies match since the hk 2939 tiers.

Verify with a known-green main commit, not a bisect result:

```bash
SHA=$(git ls-remote origin refs/heads/main | cut -f1)   # or a commit you have confirmed green
gh workflow run test-any-ref.yml -f ref="$SHA"
RUN=$(gh run list --workflow test-any-ref.yml --limit 1 --json databaseId -q '.[0].databaseId')
gh run watch "$RUN" --exit-status
POST=$(gh run list --workflow post-any-ref-status.yml --limit 1 --json databaseId -q '.[0].databaseId')
gh run watch "$POST" --exit-status
gh api "repos/mgh3326/auto_trader/commits/$SHA/statuses" --jq '.[] | "\(.context) \(.state)"' | sort -u
```

Expect `lint`, `taskiq-smoke` and the four `test (3.13, N)` statuses, all `success`. A run that fails
`validate` records nothing and posts nothing; a run whose `suite` job fails posts `failure` for that job.

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
| `root_python` | top-level `*.py` (except shared infrastructure) | same; none forces `run_all` |
| `migrations` | `alembic/**` | same |
| `config` | `config/**` | same |
| `tests` | `tests/**` | the file itself; a helper or nested `conftest.py` selects its directory and importers; fixtures also select tests mentioning their basename, including in relative paths; none forces `run_all` |
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
- **The `*.md` and `*.py` suffix rules are top-level only.** The markdown rule exists for `README.md`,
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
the gate's truth table and the superseded rule. `tests/ci/test_any_ref_workflow_contract.py` (hk 3487)
pins the literal `cancel-in-progress`, the groups per event kind, the ref validation that runs before
any checkout (run under bash), the record's watched names against the poster's, that no branch-run
workflow holds `statuses: write` and that only `post-any-ref-status.yml`'s `post` job does, that its
`workflow_run` gate passes only a dispatch from `main` (evaluated with `gh_expr`), its default-branch
checkout, and that the any-ref path posts no image or deploy-ok. `tests/ci/test_deploy_gate_workflow.py` pins
that `deploy.yml` skips a cancelled `Test` run. `tests/ci/test_deploy_tier_workflow_contract.py`
pins `ci-ok` (its `needs`, its `if:` and its step run as shell) and the deploy tier's wiring (called only
after a successful main-push `Test`, on the pushed digest, never cancelled, never pushes, `deploy-ok` judged over
every deploy-tier job); `tests/ci/test_deploy_tier_scripts.py` runs the three helpers under `scripts/ci/`
against a throwaway git repository and fake `gh`, `docker` and `curl`.

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

## 6. Toolchain cache (hk 3559, decision 2026-11-08)

Every job that installs Python calls `./.github/actions/setup-python-uv`; the Node job calls
`./.github/actions/setup-node-cached`. The exact versions are pinned once, in the `inputs` defaults of
those two `action.yml` files: Python `3.13.18` and uv `0.13.7` in `setup-python-uv`, Node `22.23.4` in
`setup-node-cached`. Each toolchain directory is restored from `actions/cache` before its setup action, keyed
by runner OS, arch and the exact version, and a miss retries the setup up to three times. To bump a version,
change that one default, then check that `tests/ci/test_toolchain_cache_contract.py` still passes (the Python
matrix minor must match the pin). Pin every external `uses:` to a full commit SHA with the tag as a comment,
and keep one SHA per action across the repo. The cache-hit proof is the `Cache restored from key:` line plus
the setup action finding the version in the tool cache, read from a `lint` job log. Docker image builds cache to the GHCR `:buildcache` tag (`type=registry`), never to the Actions cache (`type=gha`), because image layers there filled the 10GB cache and evicted these toolchain entries (hk 3578).
