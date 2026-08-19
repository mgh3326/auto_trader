# `ci-required` shadow aggregator + change classifier (ROB-1294)

**Status: shadow.** Nothing here changes what CI enforces today. The two new
jobs observe and report; the six branch-protection-required checks run exactly
as they did before this PR.

## 1. Why this exists

Branch protection currently names six checks directly:

```
lint
taskiq-smoke
test (3.13, 1)
test (3.13, 2)
test (3.13, 3)
test (3.13, 4)
```

Those names are derived from the `test` job's matrix (`python-version` ×
`group`). Any change to shard count, shard naming, or lane topology therefore
*also* requires a branch-protection edit, and an edit that is forgotten leaves
either a permanently pending required check (the old name never reports) or a
silently unenforced one (the new shard is not required). ROB-1294 builds the
two pieces a later change needs so that topology can move behind one fixed
name:

| piece | file | what it is |
|---|---|---|
| change classifier | `scripts/ci/classify_changes.py` | deterministic changed-path → lane mapping, fail-closed |
| aggregate evaluator | `scripts/ci/aggregate_required.py` | fixed-name gate over the required children's results |
| workflow wiring | `.github/workflows/test.yml` (`change-classifier`, `ci-required`) | runs both on every PR/push run |

## 2. Change classifier contract

Invoked as `python3 scripts/ci/classify_changes.py` (stdlib only, no
dependency install). It reads `$CLASSIFY_BASE_SHA` / `$CLASSIFY_HEAD_SHA` (or
`--base-sha` / `--head-sha`, or a `--name-status-file` for offline use) and
emits a JSON report plus `$GITHUB_OUTPUT` keys.

There are exactly three outcomes, and none of them is "run less because
something went wrong":

| outcome | exit | when |
|---|---|---|
| `classified` | 0 | every changed path mapped to a known lane, and every change was an add or a modify |
| `run_all` | 0 | any unknown path, any rename/copy/delete/type-change/unmerged path, any shared CI/config/test-infrastructure file, an empty change set, or an absent base SHA |
| `error` | **1** | a head SHA that was not supplied, a supplied SHA that stays unresolvable after `git fetch` (shallow/incomplete history), a failing `git` invocation, or a malformed `--name-status` record |

Lanes and the jobs each one implies:

| lane | example paths | jobs |
|---|---|---|
| `docs` | `docs/**`, `*.md` | *(none)* |
| `app` | `app/**` | `lint`, `test`, `taskiq-smoke`, `security` |
| `tests` | `tests/**` | `lint`, `test` |
| `research` | `research/**`, `research_contracts/**` | `lint`, `research` |
| `scripts` | `scripts/**` (except `scripts/ci/**`) | `lint`, `test` |
| `frontend` | `frontend/**` | `frontend` |
| `migrations` | `alembic/**` | `lint`, `test` |
| `config` | `config/**` | `lint`, `test` |
| `ci_shared` | `.github/**`, `scripts/ci/**`, `pyproject.toml`, `uv.lock`, `Makefile`, `.test_durations`, `tests/conftest.py`, `tests/_socket_guard*.py`, `env.example`, `scripts/setup-test-env.sh`, `alembic.ini`, `codecov.yml` | **forces `run_all`** |
| `unknown` | anything unmatched | **forces `run_all`** |

Two design points that look conservative and are meant to be:

- A rename inside a single lane still forces `run_all`. The pre-image path is
  part of the blast radius and `--name-status` gives no guarantee the two
  sides belong to the same lane's test surface.
- The error path still writes `run_all=true` into `$GITHUB_OUTPUT` before
  exiting 1, so a consumer reading a partially written output file cannot
  infer reduced coverage from a crashed classifier.

## 3. `ci-required` aggregate contract

`ci-required` declares `if: always()` and
`needs: [lint, test, taskiq-smoke, change-classifier]`. `always()` is
load-bearing: a job with no `if:` is *skipped* when a `needs:` child fails, and
a required check that disappears on failure is a green merge button.

`scripts/ci/aggregate_required.py` evaluates `toJSON(needs)`:

| child result | verdict |
|---|---|
| `success` | pass |
| `skipped` | pass **only** with `--authorize-skip <name>`; red otherwise |
| `failure` | red |
| `cancelled` | red |
| absent from the payload | red (`missing`) |
| any other string, `""`, `null`, a missing `result` key | red (`unexpected`) |
| present in the payload but not `--required` | red (`undeclared`) |

Malformed input — non-JSON, a non-object payload, an unset env var, a child
whose value is neither a string nor an object with `result` — is red. The
workflow passes **no** `--authorize-skip` and **no** `--allow-undeclared`
today, so `ci-required` is green only when all four children report `success`.

For the matrix `test` job, GitHub collapses all four shards into a single
`needs.test.result`, which is `failure` if any shard failed. The aggregate
therefore covers all six protected checks through three child entries.

## 4. Verifying the shadow property

`tests/ci/test_ci_required_workflow_contract.py` machine-checks that:

- the six displayed check names, the `test` matrix shape, and the absence of
  `if:`/`name:`/`needs:` on `lint`, `test` and `taskiq-smoke` are unchanged;
- `ci-required` exists with a constant displayed name and `if: always()`;
- its `--required` flags match its `needs:` list exactly;
- no job other than `ci-required` lists `change-classifier` in `needs:`, no
  `if:` expression anywhere mentions it, and `needs.change-classifier.outputs`
  appears nowhere in the workflow.

Run the whole ROB-1294 suite with:

```bash
uv run pytest tests/ci/ -q -ra
actionlint .github/workflows/test.yml
```

## 5. Operator-only follow-up — OUT OF SCOPE for ROB-1294

🔴 **Not performed by this PR.** ROB-1294 writes no GitHub repository setting
and no branch-protection configuration. What follows is a description of the
work, not an instruction to run it now.

Making `ci-required` the sole stable required check would be, in order:

1. Let `ci-required` run on `main` for a while and confirm it reports on every
   PR and push run and that its verdict always matches the six checks it
   summarises. Any divergence is a bug in the aggregate, not in the children.
2. In branch protection, **add** `ci-required` to the required-checks list
   while keeping the existing six. Merge a few PRs in that overlapping state.
3. Only then **remove** the six from the required list, leaving `ci-required`
   alone. Removing them first would leave a window with no enforcement at all.
4. From that point the shard count, the matrix shape, and lane topology can
   change without touching branch protection — provided every new job that
   must gate a merge is added to `ci-required`'s `needs:` **and** to its
   `--required` flags. The contract test in §4 fails if those two lists drift.
5. Activating the classifier (letting `run_all=false` actually skip jobs) is a
   separate change again, and it must keep the skipped jobs' *displayed names*
   reporting — a required check that is skipped is not a passing check unless
   it is passed to `--authorize-skip` deliberately.

Ordering matters more than any individual step: at no point should the set of
enforced checks be empty.
