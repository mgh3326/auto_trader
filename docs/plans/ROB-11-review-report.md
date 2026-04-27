# ROB-11 Review Report — Native Deploy Builds Trading Decision SPA dist

- **Issue:** ROB-11 — https://linear.app/mgh3326/issue/ROB-11/native-deploy-builds-trading-decision-spa-dist
- **Plan:** `docs/plans/ROB-11-native-deploy-spa-build-plan.md`
- **Branch / worktree:** `feature/ROB-11-native-deploy-builds-trading-decision-spa-dist` at `/Users/mgh3326/work/auto_trader-worktrees/feature-ROB-11-native-deploy-builds-trading-decision-spa-dist`
- **Base:** `origin/main`
- **Reviewer role:** plan-aware sign-off, read-only against production code (only this report was written).

## 1. Commits under review

```
84c25e4f docs(rob-11): add native deploy SPA build plan
fd00a88c docs(rob-11): native deploy now builds the trading-decision SPA
5970c426 feat(rob-11): build trading-decision SPA in native deploy
5d30d85a feat(rob-11): add build_frontend helper to native deploy script
```

`git diff origin/main...HEAD --stat`:

```
 docs/plans/ROB-11-native-deploy-spa-build-plan.md | 633 ++++++++++++++++++++
 frontend/trading-decision/README.md               |   9 +-
 scripts/deploy-native.sh                          |  36 ++
 3 files changed, 674 insertions(+), 4 deletions(-)
```

Only the three files the plan whitelisted (§3) are touched. No `app/`, `alembic/`, `tests/`, `frontend/trading-decision/src/`, `frontend/trading-decision/package.json`, `frontend/trading-decision/package-lock.json`, or `.github/workflows/` paths are modified. No artifacts (`dist/`, `node_modules/`) are committed.

## 2. Acceptance criteria check

| Criterion (plan §10 / issue prompt) | Status | Evidence |
|---|---|---|
| Exactly one new helper `build_frontend()` and one new call site in `scripts/deploy-native.sh` | ✅ | `grep -n build_frontend scripts/deploy-native.sh` → line 69 (definition) and line 256 (call site). |
| Function logs `node` and `npm` versions | ✅ | Lines 84-85 of script: `log "node $(node --version ...)"`, `log "npm  $(npm --version ...)"`. |
| Fails fast if `npm` is not on `PATH` (exit code matches `require_file` convention, i.e. 78 / `EX_CONFIG`) | ✅ | Lines 78-82: `command -v npm` guard, prints message + `PATH=$PATH` to stderr, `return 78`. |
| Runs `npm ci && npm run build` in `frontend/trading-decision` | ✅ | Lines 88-92: subshell `( cd "$workspace"; npm ci; npm run build )`. Uses `npm ci`, not `npm install`. |
| Asserts `dist/index.html` exists | ✅ | Lines 94-97: `[[ ! -f "$index" ]] && echo ... && return 1`. |
| Placed before symlink switch & service restart | ✅ | Call site at line 256; symlink switch at 264-266 (`SWITCHED=1` only at 266); `restart_services` at 268; `run_healthcheck` at 271. |
| Placed before `alembic upgrade head` (preferred per the issue prompt) | ✅ | Call site at line 256; `alembic upgrade head` at line 262. |
| `set -Eeuo pipefail` + `trap rollback ERR` already in effect when `build_frontend` runs | ✅ | `set -Eeuo pipefail` at line 2; `trap rollback ERR` at line 210; call site at 256. Failure propagates via `set -e` (subshell carries `-e` because of `-E`); ERR trap fires; `SWITCHED=0` branch in `rollback` prints "No symlink switch happened …" and exits non-zero. |
| README "Production Deployment" section updated | ✅ | `frontend/trading-decision/README.md:51-62` rewritten. No longer says "does not yet build the SPA". Out-of-band hotfix recipe (`cd "$AUTO_TRADER_BASE/current/frontend/trading-decision" && npm ci && npm run build`) preserved as a fallback. |
| Plan committed at agreed path | ✅ | `docs/plans/ROB-11-native-deploy-spa-build-plan.md`, 633 lines, terminal AOE block present. |
| `frontend/trading-decision/dist/` and `node_modules/` not committed | ✅ | `git ls-files frontend/trading-decision/dist/ frontend/trading-decision/node_modules/` returns empty. `.gitignore:289-292` and `frontend/trading-decision/.gitignore` cover both. |

## 3. Production safety analysis

### 3.1 Failure path (npm missing, npm ci fail, vite build fail, dist missing)

For every failure mode inside `build_frontend`:

1. The function returns non-zero (`return 78` for missing npm, propagated subshell exit for `npm ci`/`npm run build` failures, `return 1` for missing artifact).
2. At line 256 the function is called bare (not in `if`, `while`, `&&`, `||`, or `!` context), so `set -e` aborts the script.
3. `trap rollback ERR` (line 210) fires before exit; `set -E` (line 2) already ensures the trap is inherited by functions and command substitutions.
4. `SWITCHED` is still `0` (initialized at line 55, only set to `1` at line 266 *after* alembic + the symlink switch). The rollback function takes the `else` branch at lines 203-205 and prints "No symlink switch happened, or previous release is unavailable; skipping rollback restart".
5. Outcome: previous `current` release unchanged, no DB migration applied, no `launchctl` churn, exit non-zero → workflow fails → Discord failure embed.

This matches the plan's §4.1/§4.3 design and the issue's "avoid switching `current` or restarting services if frontend build fails" requirement.

### 3.2 Rollback policy regression

`rollback()` is unchanged. The trap is unchanged. The migration ordering note at lines 259-261 (expansion-only / backwards-compatible) is unchanged. No regression.

### 3.3 Re-deploy idempotency

`git clean -fdx -e .venv` at line 250 wipes `frontend/trading-decision/node_modules/` and `frontend/trading-decision/dist/` on every release prep (both are gitignored, so `clean -fdx` removes them). A retry of the same SHA therefore re-runs `npm ci` from a clean tree. No partial-state hazard.

### 3.4 Subshell + `set -e` interaction (sanity check)

`set -e` does not always carry into subshells in old bash, but `set -Eeuo pipefail` here means:
- `-e` is inherited by subshells (this is bash's default behavior; only POSIX `sh` strips it).
- The subshell `(cd "$workspace"; npm ci; npm run build)` exits non-zero when any of its commands fail.
- Outside the subshell the parent's `set -e` evaluates the subshell's exit code as the result of a simple command and aborts.

Confirmed by reading the script and matching the existing pattern. No behavior change for existing subshells in the script (there are none of the form `set +e ... set -e` around the new call).

## 4. Secret / logging review

The new function does **not**:
- read or print any environment variable other than `PATH` (printed only on the npm-missing failure path, which is operator forensics, not a secret leak).
- echo `SHARED_ENV`, `KIS_*`, `UPBIT_*`, `GOOGLE_*`, `TELEGRAM_*`, `DATABASE_URL`, `REDIS_URL`, or any secret.
- enable `set -x`.

`grep -n "SECRET\|API_KEY\|PASSWORD\|TOKEN" scripts/deploy-native.sh` is empty. No secret-handling regression.

`PATH` printout on failure is acceptable: it lists directories, not secret values, and the existing script already exposes the `PATH` indirectly via its line 33 export.

## 5. Local validation results

Run from the worktree root (`/Users/mgh3326/work/auto_trader-worktrees/feature-ROB-11-native-deploy-builds-trading-decision-spa-dist`):

| Check | Command | Result |
|---|---|---|
| Shell syntax | `bash -n scripts/deploy-native.sh` | ✅ exit 0, no output |
| Diff hygiene | `git diff origin/main...HEAD --check` | ✅ `DIFF_CHECK_OK` (no whitespace warnings) |
| Working tree | `git status --short` | ✅ empty |
| Frontend install | `cd frontend/trading-decision && npm ci` | ✅ no errors |
| Frontend build | `npm run build` | ✅ vite emits `dist/index.html` (0.46 kB), `dist/assets/index-*.css` (3.75 kB), `dist/assets/index-*.js` (299.08 kB), built in ~1.13 s |
| Artifact present | `test -f frontend/trading-decision/dist/index.html` | ✅ exists |
| Asset base path | `grep '/trading/decisions/assets/' frontend/trading-decision/dist/index.html` | ✅ matches (referenced from `index.html`) |
| SPA router pytest | `uv run pytest tests/test_trading_decisions_spa_router.py tests/test_trading_decisions_spa_router_safety.py -v --no-cov` | ✅ 7 passed, 0 failed |
| Cleanup | `rm -rf frontend/trading-decision/dist frontend/trading-decision/node_modules` | ✅ tree clean (gitignored anyway) |

`make frontend-build` was not re-run separately because it is a thin wrapper around the same `npm run build` in the same workspace (`Makefile:100-101`); the bare `npm run build` covers it.

## 6. Forbidden-touch audit

Per plan §9 ("What the implementer must NOT do"):

| Constraint | Result |
|---|---|
| No edits under `app/`, `alembic/`, `tests/`, `frontend/trading-decision/src/`, `frontend/trading-decision/package.json`, `frontend/trading-decision/package-lock.json`, `.github/workflows/` | ✅ confirmed via `git diff origin/main...HEAD --stat` |
| No commit of `frontend/trading-decision/dist/` or `frontend/trading-decision/node_modules/` | ✅ confirmed via `git ls-files` |
| No `--no-verify`; no force push to `main`/`production` | ✅ branch is feature branch; no force-push observed |
| No `Dockerfile.api` `ENABLE_FRONTEND_BUILD` flip | ✅ `Dockerfile.api` not touched |
| No `npm install` (only `npm ci`) | ✅ confirmed: `grep -n 'npm install' scripts/deploy-native.sh` empty; function uses `npm ci` |
| No secret logged | ✅ §4 above |
| No `.nvmrc` / `engines.node` change | ✅ neither file is in the diff |
| No `set -x` added | ✅ confirmed |

## 7. Minor observations (non-blocking)

1. **Commit message trailer.** The four implementer commits do not include `Co-Authored-By: Paperclip <noreply@paperclip.ing>` (CLAUDE.md mentions this trailer for the team's PR-merge convention). Recent main history shows it on per-commit entries that are later squash-merged (e.g. PR #594, #593). Since the PR is expected to be squash-merged into `main`, this has no functional impact and the merge commit message can be reformulated by the maintainer at merge time. **Not a must-fix.**
2. **Stale README "Constraints" sentence.** `frontend/trading-decision/README.md:72` still reads "Do not add … React Router or another client router. Add those in ROB-4 with a design doc." ROB-7 already shipped React Router + multi-page UI. Pre-existing text untouched by this PR. **Out of scope for ROB-11; flag as a follow-up doc cleanup.**
3. **Build-time noise.** `npm ci` will produce ~1500 lines per release in the deploy log. Acceptable for the first cut; if log volume becomes an issue, a `--silent`/`--no-progress` flag can be added in a follow-up. **Not a must-fix.**
4. **No deploy-time grep for the `/trading/decisions/assets/` base path.** The plan §8 risk row notes that `frontend-trading-decision.yml` already greps this on PR. Adding the same grep to `build_frontend` would be defense-in-depth; the existing `dist/index.html` existence assertion is sufficient for shipping. **Optional follow-up.**

## 8. Verdict

The implementation matches the plan exactly:
- One new function, one new call site, both at the prescribed locations.
- Correct ordering relative to `uv sync --frozen`, `alembic upgrade head`, the `current` symlink switch, and `restart_services`.
- Failure semantics audited against `set -Eeuo pipefail` + `trap rollback ERR` + `SWITCHED` state machine; production-safe.
- No secret regression, no rollback policy regression, no out-of-scope file touched, no committed artifacts.
- Local validation green: shell syntax, frontend build, dist artifact + asset base, SPA router pytest, diff hygiene.

Recommend proceeding to PR creation.

---

AOE_STATUS: review_passed
AOE_ISSUE: ROB-11
AOE_ROLE: reviewer
AOE_REPORT_PATH: docs/plans/ROB-11-review-report.md
AOE_NEXT: create_pr
