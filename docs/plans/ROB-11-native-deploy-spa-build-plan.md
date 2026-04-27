# ROB-11 — Native Deploy Builds Trading Decision SPA dist Plan

> **For agentic workers:** Implement task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. Implementer must be **Codex (`codex --yolo`)** in the same worktree (see §13 handoff prompt).

- **Linear:** ROB-11 — https://linear.app/mgh3326/issue/ROB-11/native-deploy-builds-trading-decision-spa-dist
- **Worktree:** `/Users/mgh3326/work/auto_trader-worktrees/feature-ROB-11-native-deploy-builds-trading-decision-spa-dist`
- **Branch / base:** `feature/ROB-11-native-deploy-builds-trading-decision-spa-dist` ← `origin/main`
- **Status:** Plan only. No implementation yet. Codex YOLO is the implementer.
- **Depends on (already merged to `main`):**
  - ROB-6 / PR #598 — `frontend/trading-decision/` Vite/React/TS workspace + `app/routers/trading_decisions_spa.py` + `make frontend-*` targets.
  - ROB-7 — Decision Workspace UI (interactive inbox / detail / respond) shipped to `main`.

> ⚠️ This PR ships **a single shell-level change** to `scripts/deploy-native.sh` so the macOS native production deploy builds the React/Vite SPA into `frontend/trading-decision/dist/` before the symlink switch + service restart. **Out of scope:** Docker image SPA bake (ROB-6 §6.4 seam stays default-OFF), Prompt 5 outcomes/analytics UI, broker / KIS / Upbit / Redis behavior, secret handling, auth policy, route changes, or any Python/SQL change.

**Goal:** Make the MacBook native deploy script deterministically produce `frontend/trading-decision/dist/index.html` (and hashed assets) inside each `releases/<sha>/` checkout, ahead of the `current` symlink switch and `launchctl` restarts, so that future native deploys remove the manual production hotfix and stop serving the HTTP 503 "build missing" fallback.

**Architecture:** A single new bash function `build_frontend` is added to `scripts/deploy-native.sh`. It (a) logs `node` and `npm` versions, (b) fails fast (exit 78 / `EX_CONFIG`) if `npm` is not on `PATH`, (c) runs `npm ci && npm run build` inside `$NEW_RELEASE/frontend/trading-decision`, and (d) asserts `dist/index.html` exists. It is invoked from the main deploy flow **between `uv sync --frozen` and `alembic upgrade head`** — i.e. before any DB migration, before the `ln -sfn $NEW_RELEASE $CURRENT` symlink switch, and before `restart_services`. Failure short-circuits via the existing `set -Eeuo pipefail` + `trap rollback ERR`; because `SWITCHED=0` at that point, the rollback path is a no-op (the previous `current` release continues to serve traffic uninterrupted).

**Tech Stack:** macOS bash, Node 20.x (via `~/.hermes/node/bin`, already on the deploy script's `PATH`), npm (bundled with Node), Vite 8 build (`tsc --noEmit && vite build`).

---

## 1. Current vs desired state

### 1.1 Current `scripts/deploy-native.sh` flow

```text
require_file SHARED_ENV
mkdir -p releases logs state/heartbeat
git fetch / locate SHA in source repo
clone --local source repo into $NEW_RELEASE if missing
git checkout --detach $SHA
git clean -fdx -e .venv          # wipes node_modules/, dist/, etc.
uv sync --frozen
alembic upgrade head             # ← migrations run BEFORE symlink switch
ln -sfn $NEW_RELEASE $CURRENT    # ← SWITCHED=1 from here
restart_services                 # launchctl bootout/bootstrap/kickstart
run_healthcheck                  # /healthz, MCP 401/400, websocket heartbeat
```

Production effect today: `frontend/trading-decision/dist/` is **never produced** by this script. The FastAPI route `/trading/decisions/` therefore renders the 503 "Trading Decision Workspace · build missing" fallback (`app/routers/trading_decisions_spa.py:71-83`). The recent prod hotfix manually ran `npm ci && npm run build` inside `~/services/auto_trader/current/frontend/trading-decision`; that fix is **per-release** and is wiped on the next deploy because each release is a fresh checkout.

### 1.2 Desired flow (this PR)

```text
require_file SHARED_ENV
mkdir -p releases logs state/heartbeat
git fetch / locate SHA in source repo
clone --local source repo into $NEW_RELEASE if missing
git checkout --detach $SHA
git clean -fdx -e .venv
uv sync --frozen
build_frontend                   # ★ NEW — npm ci + npm run build + assert dist/index.html
alembic upgrade head
ln -sfn $NEW_RELEASE $CURRENT    # SWITCHED=1
restart_services
run_healthcheck
```

If `build_frontend` fails:
- `set -Eeuo pipefail` propagates the non-zero exit.
- `trap rollback ERR` fires; `SWITCHED=0` → rollback prints "No symlink switch happened" and exits non-zero.
- The previous `current` release is undisturbed; `launchd` services are not restarted; no DB migration ran.

---

## 2. In-scope vs Out-of-scope

| Area | In scope (this PR) | Deferred |
|---|---|---|
| New `build_frontend` bash function in `scripts/deploy-native.sh` | ✅ | — |
| Wiring the new function between `uv sync --frozen` and `alembic upgrade head` | ✅ | — |
| Logging `node --version` / `npm --version` for debug | ✅ | — |
| Fail-fast when `npm` is not on `PATH` (exit 78) | ✅ | — |
| Asserting `frontend/trading-decision/dist/index.html` after build | ✅ | — |
| Updating `frontend/trading-decision/README.md` "Production Deployment" section | ✅ | — |
| `bash -n` syntax check on the modified script (added to PR validation) | ✅ | — |
| `tests/test_trading_decisions_spa_router.py` regression run as a smoke gate | ✅ (run, not changed) | — |
| Build caching across releases (e.g. shared `node_modules` like `.venv`) | ❌ | follow-up |
| Trimming old `releases/<sha>/frontend/trading-decision/node_modules/` to reclaim disk | ❌ | follow-up |
| Flipping the `Dockerfile.api` `ENABLE_FRONTEND_BUILD=1` arg | ❌ | future Docker switch |
| Any Python / Alembic / SQL / FastAPI / auth / CSRF change | ❌ | not needed |
| Adding shellcheck to CI (would require new workflow change) | ❌ | follow-up |
| Touching `.github/workflows/deploy-macos-native.yml` | ❌ | not needed (script is streamed via `ssh ... < scripts/deploy-native.sh`) |

---

## 3. Files touched

| File | Change |
|---|---|
| `scripts/deploy-native.sh` | Add `build_frontend()` (~30 lines) near the existing helpers; insert a single call site between the `uv sync --frozen` block and the `alembic upgrade head` block. |
| `frontend/trading-decision/README.md` | Replace the `## Production Deployment` paragraph that says "the macOS native deploy path … does not yet build the SPA" with the new behavior; remove the 503-by-default warning. |

No other files are changed. **Do not** commit `frontend/trading-decision/dist/` or `node_modules/` — both remain gitignored (`.gitignore:289-292`, `frontend/trading-decision/.gitignore`).

---

## 4. Production deployment ordering & rollback analysis

### 4.1 Why insert *before* `alembic upgrade head`

The user explicitly asked: **"Prefer before Alembic migrations if that is safer."** It is, because:

1. **No DB writes on SPA failure.** Migrations are expansion-only / backwards-compatible by policy (`scripts/deploy-native.sh:223-226`), but "expansion-only" is a property the new release relies on — running them when we already know the release is unshippable trades an irreversible (in practice) DB schema change for nothing. Skipping them on SPA failure keeps the database aligned with the still-running previous release.
2. **No symlink switch on SPA failure.** `SWITCHED=0` at the moment `build_frontend` would fail; the `trap rollback ERR` path explicitly handles that case ("No symlink switch happened … skipping rollback restart").
3. **No `launchctl` churn on SPA failure.** Services keep running on the previous release.
4. **Healthcheck is never reached on SPA failure.** No false "deploy succeeded" Discord notification.

### 4.2 Why insert *after* `uv sync --frozen`

- Both `uv sync` and `npm ci` re-fetch from a network mirror; ordering them either way is fine, but keeping `uv sync` first preserves the existing log grouping and means a reviewer reads them in the same order they appear in current deploy logs.
- `git clean -fdx -e .venv` (which already runs immediately before `uv sync --frozen`) wipes `frontend/trading-decision/node_modules/` and `dist/` in each fresh release checkout, so `npm ci` always starts from a clean slate. No additional cleanup is needed.

### 4.3 Failure modes considered

| Scenario | Today | After this PR |
|---|---|---|
| `npm` missing from `PATH` | (build never attempted) | `build_frontend` exits 78; trap rollback fires; no symlink switch; previous release keeps serving. |
| `npm ci` network failure (registry timeout) | n/a | non-zero exit propagated; same rollback path. |
| `vite build` typecheck/syntax error | n/a | same rollback path. Frontend CI workflow `frontend-trading-decision.yml` should already catch this on PR; the deploy gate is a defense-in-depth. |
| `dist/index.html` not produced despite zero-exit `npm run build` | n/a | explicit `[[ -f $index ]]` check returns 1; trap fires. |
| Re-deploying the same SHA after a partial failure | release dir is reused; `git clean -fdx -e .venv` wipes `node_modules/` and `dist/`; build re-runs from scratch | same |
| Concurrent deploys | blocked by workflow `concurrency: auto-trader-macos-native-production cancel-in-progress: false` | same |

### 4.4 Rollback narrative for this PR's changes

There is no rollback runbook needed beyond reverting the PR. Reverting restores the prior (no-SPA-build) deploy script. Existing releases that already have `dist/` baked stay valid; the rollback simply means future deploys go back to producing the 503 fallback.

---

## 5. The `build_frontend` function (target source)

Insert verbatim into `scripts/deploy-native.sh` — placement detailed in §6 Task 2. The function uses only POSIX-ish features that the existing script already relies on (`local`, `[[ ]]`, command substitution).

```bash
build_frontend() {
  local workspace="$NEW_RELEASE/frontend/trading-decision"
  local index="$workspace/dist/index.html"

  if [[ ! -d "$workspace" ]]; then
    log "Frontend workspace not present at $workspace; skipping SPA build"
    return 0
  fi

  if ! command -v npm >/dev/null 2>&1; then
    echo "npm not found on PATH for native deploy; cannot build trading-decision SPA" >&2
    echo "PATH=$PATH" >&2
    return 78
  fi

  log "Building trading-decision SPA in $workspace"
  log "node $(node --version 2>/dev/null || echo 'unknown')"
  log "npm  $(npm --version 2>/dev/null || echo 'unknown')"

  (
    cd "$workspace"
    npm ci
    npm run build
  )

  if [[ ! -f "$index" ]]; then
    echo "Frontend build did not produce $index" >&2
    return 1
  fi

  log "Frontend SPA build present: $index"
}
```

**Design notes — read before editing:**

- The subshell `( cd "$workspace"; npm ci; npm run build )` keeps the outer script's `cwd` at `$NEW_RELEASE`. The outer `set -e` carries into the subshell, so any failure aborts the function with the right exit code.
- Exit code `78` (`EX_CONFIG`) matches the existing `require_file` convention at `scripts/deploy-native.sh:62-67`.
- The "skip if workspace missing" branch protects future branches that delete `frontend/trading-decision/`. If we ever drop the workspace, the deploy still works.
- `node` and `npm` versions are logged unconditionally for forensics (matches the rest of the script's `log` calls).
- The function does **not** read any secret. It does not pass `--prefer-offline` or `--cache` flags; we accept a fresh `npm ci` per release for now (see §2 Out-of-scope).

---

## 6. Tasks

> Each task is one focused change with a clear pre/post verification step. Commits are per-task.

### Task 1 — Add the `build_frontend` helper to `scripts/deploy-native.sh`

**Files:**
- Modify: `scripts/deploy-native.sh` — add a new function definition; **do not yet wire a call site**. Splitting "define" and "wire" makes the diff smaller and lets the syntax check in Task 1 catch issues before the function is invoked.

**Insert location:** Immediately after the existing `require_file()` helper (currently `scripts/deploy-native.sh:61-67`) and before `restart_services()` (currently at `scripts/deploy-native.sh:69`). Keeping all helpers grouped together matches the existing file layout.

- [ ] **Step 1.1 — Add the function**

  Open `scripts/deploy-native.sh`. After the closing `}` of `require_file()` (line ~67) and the blank line that follows, paste the function from §5 verbatim, followed by a blank line.

- [ ] **Step 1.2 — Verify shell syntax**

  Run:
  ```bash
  bash -n scripts/deploy-native.sh
  ```
  Expected: exit 0, no output. Any parse error means a typo in the paste.

- [ ] **Step 1.3 — (Optional, if installed) shellcheck**

  Run:
  ```bash
  shellcheck scripts/deploy-native.sh || true
  ```
  Read any new warnings introduced by the new function and fix them. Pre-existing warnings on unmodified lines are out of scope. If shellcheck is not installed locally, skip — CI does not currently shellcheck this script.

- [ ] **Step 1.4 — Commit**

  ```bash
  git add scripts/deploy-native.sh
  git commit -m "feat(rob-11): add build_frontend helper to native deploy script"
  ```

### Task 2 — Wire `build_frontend` into the deploy flow

**Files:**
- Modify: `scripts/deploy-native.sh` — add a single new `log` line and a single new function call between the `uv sync --frozen` block and the `alembic upgrade head` block.

**Target placement (current line numbers — verify before editing; the file is short so a string-anchored edit is safer than a line-number edit):**

```text
log "Installing dependencies with uv"
uv sync --frozen

# ↓↓↓ NEW BLOCK GOES HERE ↓↓↓
log "Building Trading Decision SPA"
build_frontend
# ↑↑↑ NEW BLOCK ↑↑↑

log "Running Alembic migrations"
# Online deploy rollback only reverts code/services. Production migrations must be
# expansion-only/backwards-compatible with the previous release; do not merge
# destructive downgrades into this path without a separate data rollback runbook.
ENV_FILE="$SHARED_ENV" uv run alembic upgrade head
```

- [ ] **Step 2.1 — Insert the call site**

  Use `Edit` (not line-number `sed`) and anchor on the unique string `uv sync --frozen\n\nlog "Running Alembic migrations"`. Replace it with the same text plus the two new lines + the surrounding blank line, as shown above.

  Sanity check: the file should now contain exactly one occurrence of `build_frontend` *outside* its own definition.

  ```bash
  grep -n "build_frontend" scripts/deploy-native.sh
  # Expect 2 matches: function definition (line ~70) + call site (between uv sync and alembic).
  ```

- [ ] **Step 2.2 — Verify shell syntax**

  ```bash
  bash -n scripts/deploy-native.sh
  ```
  Expected: exit 0, no output.

- [ ] **Step 2.3 — Trace ordering**

  Confirm the call site sits **before** these three sentinels (in order):

  ```bash
  awk '/build_frontend$/{print NR": "$0}
       /alembic upgrade head/{print NR": "$0}
       /Switching current symlink/{print NR": "$0}
       /Restarting launchd services/{print NR": "$0}' scripts/deploy-native.sh
  ```
  Expected: line numbers strictly increase in the order above.

- [ ] **Step 2.4 — Dry-run the function locally (no deploy)**

  This is the only place where it is feasible to actually exercise the new function locally without a release dir layout. Run a self-contained probe:

  ```bash
  cd /Users/mgh3326/work/auto_trader-worktrees/feature-ROB-11-native-deploy-builds-trading-decision-spa-dist
  (
    set -Eeuo pipefail
    NEW_RELEASE="$PWD"
    log() { printf '[%s] %s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$*"; }
    # shellcheck disable=SC1091
    source <(awk '/^build_frontend\(\)/{flag=1} flag; /^}$/{if(flag){flag=0; print "\n"}}' scripts/deploy-native.sh)
    build_frontend
  )
  ```

  Expected: `node` and `npm` versions are logged; `npm ci` then `npm run build` succeed; the function logs `Frontend SPA build present: .../frontend/trading-decision/dist/index.html`.

  If `npm` is not on PATH locally, expect exit 78 with the message `npm not found on PATH for native deploy; cannot build trading-decision SPA`. That is a successful negative test and proves the fail-fast branch works — but you will need to install Node and re-run the positive case before merging.

- [ ] **Step 2.5 — Confirm artifacts and clean up**

  ```bash
  test -f frontend/trading-decision/dist/index.html && echo OK
  grep -q '/trading/decisions/assets/' frontend/trading-decision/dist/index.html && echo asset-base-OK
  ```
  Both must print `OK` / `asset-base-OK`. Then:

  ```bash
  rm -rf frontend/trading-decision/dist frontend/trading-decision/node_modules
  ```
  Removing the artifacts ensures the `git status` stays clean before commit (`dist/` and `node_modules/` are gitignored, so they never appear in `git status` anyway, but removing them avoids confusion if a teammate inspects the worktree).

- [ ] **Step 2.6 — Commit**

  ```bash
  git add scripts/deploy-native.sh
  git commit -m "feat(rob-11): build trading-decision SPA in native deploy"
  ```

### Task 3 — Update `frontend/trading-decision/README.md` Production Deployment section

**Files:**
- Modify: `frontend/trading-decision/README.md:51-62` (the `## Production Deployment` block).

The current text says the native deploy "does not yet build the SPA" and describes the manual `make frontend-build` workaround. After this PR, the native deploy path *does* build the SPA. The README must reflect that.

- [ ] **Step 3.1 — Replace the section**

  Replace the existing block (current `## Production Deployment` heading through the line that says "Then restart the API.") with:

  ```markdown
  ## Production Deployment

  The macOS native deploy path in `scripts/deploy-native.sh` builds this SPA on every release. The deploy script runs `npm ci && npm run build` inside `frontend/trading-decision/` of the new release checkout, asserts `dist/index.html` exists, and aborts before the `current` symlink switch if the build fails (see ROB-11). No manual `make frontend-build` step on the deploy host is required.

  If you ever need to rebuild on a deploy host out-of-band (e.g. a hotfix between deploys), run from the active release directory:

  ```bash
  cd "$AUTO_TRADER_BASE/current/frontend/trading-decision"
  npm ci && npm run build
  ```

  and then reload the API process. This is a fallback only — the next deploy will rebuild from scratch.
  ```

  > Note: README content above is one nested code fence inside a markdown code fence. When pasting, preserve indentation; the inner fence uses ```` ``` ```` and the outer file does not need any escaping in the README itself (it has no surrounding fences).

- [ ] **Step 3.2 — Quick sanity scan**

  ```bash
  grep -n "does not yet build\|HTTP 503 in production\|build-missing page" frontend/trading-decision/README.md
  ```
  Expected matches: only the `## Troubleshooting` line near the bottom that explains "HTTP 503 in production: `dist/` is missing." Keep that line — it is still a valid troubleshooting hint if a deploy partially regressed, but adjust its surrounding sentence if it implies "the deploy doesn't build the SPA". Specifically, if line 77 still reads `HTTP 503 in production: dist/ is missing. See the production deployment section.`, leave it. If anything else still says "does not yet build", fix it.

- [ ] **Step 3.3 — Commit**

  ```bash
  git add frontend/trading-decision/README.md
  git commit -m "docs(rob-11): native deploy now builds the trading-decision SPA"
  ```

### Task 4 — Run the full validation suite

This task is verification-only. No file changes.

- [ ] **Step 4.1 — Shell syntax**

  ```bash
  bash -n scripts/deploy-native.sh
  ```
  Expected: exit 0.

- [ ] **Step 4.2 — Frontend build (matches what the deploy script will run)**

  ```bash
  cd frontend/trading-decision
  npm ci
  npm run build
  test -f dist/index.html
  grep -q '/trading/decisions/assets/' dist/index.html
  cd -
  ```
  Expected: build succeeds, `dist/index.html` exists, references hashed assets under `/trading/decisions/assets/`.

- [ ] **Step 4.3 — `make frontend-build`**

  ```bash
  make frontend-build
  ```
  Expected: same successful build via the Makefile entry point. (This is what a deploy host operator would run as a manual fallback per the updated README.)

- [ ] **Step 4.4 — SPA router regression suite**

  ```bash
  uv run pytest tests/test_trading_decisions_spa_router.py tests/test_trading_decisions_spa_router_safety.py -v
  ```
  Expected: all tests pass. These tests exercise the FastAPI seam that consumes the `dist/` we are now producing in the deploy. They do not depend on a real `dist/` (each test stages a temporary one), so they should continue to pass with no changes.

  Sanity: `test_index_returns_503_when_dist_missing` still passes — the 503 branch is still the right behavior when `dist/index.html` is genuinely absent (e.g. a deploy that skipped the new function for any reason).

- [ ] **Step 4.5 — Diff hygiene**

  ```bash
  git diff --check
  git status
  ```
  Expected: no whitespace warnings; only the three files in §3 are modified; no `dist/` / `node_modules/` showing up (gitignored).

### Task 5 — Open the PR

- [ ] **Step 5.1 — Push branch**

  ```bash
  git push -u origin feature/ROB-11-native-deploy-builds-trading-decision-spa-dist
  ```

- [ ] **Step 5.2 — Open PR**

  ```bash
  gh pr create \
    --base main \
    --title "feat(rob-11): native deploy builds trading-decision SPA dist" \
    --body "$(cat <<'EOF'
  ## Summary
  - `scripts/deploy-native.sh` now builds `frontend/trading-decision/dist/` on every native release before symlink switch and service restart.
  - Failure short-circuits via the existing `set -Eeuo pipefail` + `trap rollback ERR`; because `SWITCHED=0` at that point, no DB migration runs, the `current` symlink is not flipped, and the previous release keeps serving traffic.
  - README updated to reflect that the native deploy path now bakes the SPA.

  ## Why
  Production has been serving the HTTP 503 "Trading Decision Workspace · build missing" fallback because the macOS native deploy path never produced `dist/index.html`. A manual `npm ci && npm run build` was run on the deploy host as a hotfix; that fix is wiped on every subsequent deploy because each release is a fresh checkout. ROB-11.

  ## Acceptance checklist
  - [ ] `bash -n scripts/deploy-native.sh` clean
  - [ ] `cd frontend/trading-decision && npm ci && npm run build` produces `dist/index.html` referencing `/trading/decisions/assets/`
  - [ ] `uv run pytest tests/test_trading_decisions_spa_router.py tests/test_trading_decisions_spa_router_safety.py -v` green
  - [ ] PR CI green (frontend-trading-decision workflow + any other required checks)
  - [ ] No `frontend/trading-decision/dist/` or `node_modules/` committed

  ## Post-merge smoke (manual)
  After `production` deploy completes, on the deploy host:
  - `ls -1 "$AUTO_TRADER_BASE/current/frontend/trading-decision/dist/index.html"` exists
  - `curl -fsS http://127.0.0.1:8000/healthz` → 200
  - `curl -sS -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8000/trading/decisions/` → 303 (auth redirect to /login), **not** 503
  - `curl -sS -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8000/trading/api/decisions` → 401
  - Native healthcheck (`scripts/healthcheck-native.sh`) passes
  EOF
  )"
  ```

- [ ] **Step 5.3 — Watch CI**

  ```bash
  gh pr checks --watch
  ```
  Expected: all required checks green. The `Frontend - Trading Decision` workflow already verifies `dist/index.html` exists and references hashed assets — the same invariants the new deploy step asserts at deploy time, which is a healthy belt-and-suspenders.

---

## 7. Post-merge production deploy + smoke (operator playbook)

This is **not** part of the PR; it is what the operator runs after the PR merges to `main` and is then merged to `production`.

1. Merge `main` → `production` and push (per `CLAUDE.md` "브랜치 & PR 워크플로우").
2. Wait for `Deploy MacBook Native Production` workflow run to start. Tail logs:
   ```bash
   gh run watch --workflow=deploy-macos-native.yml
   ```
3. In the deploy log, look for the new lines (in order):
   ```
   [...Z] Building Trading Decision SPA
   [...Z] Building trading-decision SPA in /Users/.../releases/<sha>/frontend/trading-decision
   [...Z] node v20.x.x
   [...Z] npm  10.x.x
   ... npm ci output ...
   ... vite build output (chunks emitted) ...
   [...Z] Frontend SPA build present: /Users/.../releases/<sha>/frontend/trading-decision/dist/index.html
   [...Z] Running Alembic migrations
   ```
   If the SPA step fails, the run aborts before "Running Alembic migrations" prints — that is the correct ordering.
4. Manual smoke against the live API (Tailscale or local):
   - `curl -fsS http://127.0.0.1:8000/healthz` returns `200 ok`.
   - `curl -sS -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8000/trading/decisions/` returns `303` (auth redirect). **Not** `503`.
   - `curl -sS -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8000/trading/api/decisions` returns `401`.
   - Browser visit to `https://<deploy-host>/trading/decisions/` after login renders the React inbox (ROB-7 UI), not the build-missing fallback.
   - `ls -l "$AUTO_TRADER_BASE/current/frontend/trading-decision/dist/index.html"` exists.

If any of those fail, do not roll forward; investigate the deploy log for the `build_frontend` block. The previous release continues to serve until a new `current` is symlinked.

---

## 8. Risks & mitigations

| Risk | Likelihood | Mitigation |
|---|---|---|
| `npm` is missing on the deploy host | Low (PATH already contains `~/.hermes/node/bin`) | Fail-fast with exit 78 + explicit `PATH=` log; no symlink switch; previous release intact. |
| `npm ci` registry transient failure | Medium | Re-run the deploy workflow; existing `concurrency: cancel-in-progress: false` prevents overlapping runs from stomping each other. Optional follow-up: add an explicit `npm ci --prefer-offline` once a per-host npm cache is provisioned. |
| Vite build typecheck failure that slipped past PR CI | Low | Already caught by `frontend-trading-decision.yml` on every PR touching the workspace. The deploy gate is defense-in-depth. |
| `dist/index.html` produced but missing the SPA bundle (regression in `vite.config.ts` `base`) | Low | Deploy script asserts the file exists; `frontend-trading-decision.yml` additionally `grep`s for `/trading/decisions/assets/`. Consider extending the deploy assertion to the same grep in a follow-up. |
| First deploy after this PR uses ~200 MB extra disk per release for `node_modules/` | Medium | Out of scope — track disk and add retention follow-up. The existing release-pruning policy (manual today) handles old `releases/<sha>/` cleanup unchanged. |
| Build slows the deploy by ~30–60 s | Expected | Acceptable; the deploy is gated by service restart + healthcheck retries which already dominate the timeline. |
| Failure mid-way leaves stale `node_modules/` in `releases/<sha>/frontend/trading-decision/` | Low | The release dir is reused only on retry of the same SHA; `git clean -fdx -e .venv` at the start of each retry wipes node_modules before re-running. |

---

## 9. What the implementer must NOT do

1. Do **not** modify any file under `app/`, `alembic/`, `tests/`, `frontend/trading-decision/src/`, `frontend/trading-decision/package.json`, `frontend/trading-decision/package-lock.json`, or any workflow under `.github/workflows/`.
2. Do **not** commit `frontend/trading-decision/dist/` or `frontend/trading-decision/node_modules/` (both gitignored; verify with `git status`).
3. Do **not** add `--no-verify` to any commit.
4. Do **not** force-push to `main` or `production`.
5. Do **not** flip `Dockerfile.api`'s `ENABLE_FRONTEND_BUILD` arg in this PR.
6. Do **not** `npm install` (no lockfile drift); always `npm ci`.
7. Do **not** print or log secret values; the new function does not need any secret.
8. Do **not** change `frontend/trading-decision/.nvmrc` or `engines.node` in `package.json`.
9. Do **not** add a `set -x` to the deploy script (it would log the `SHARED_ENV` filename and any future env var passed inline).

---

## 10. What good looks like (acceptance)

- `scripts/deploy-native.sh` contains exactly one new function `build_frontend()` and exactly one new call site. Both are visible in `git diff main...HEAD -- scripts/deploy-native.sh`.
- `frontend/trading-decision/README.md` no longer claims the native deploy skips the SPA build.
- `bash -n scripts/deploy-native.sh` is clean.
- `cd frontend/trading-decision && npm ci && npm run build` produces `dist/index.html` locally.
- `uv run pytest tests/test_trading_decisions_spa_router.py tests/test_trading_decisions_spa_router_safety.py -v` is green.
- `git diff --check` is clean.
- PR CI is green.
- Post-merge production deploy log shows the new "Building Trading Decision SPA" block ahead of "Running Alembic migrations" and "Switching current symlink".
- Post-merge smoke: `/trading/decisions/` unauth returns `303` (login redirect), not `503`.

---

## 11. Validation block (to copy-paste verbatim before tagging the PR ready-for-review)

```bash
set -Eeuo pipefail
cd /Users/mgh3326/work/auto_trader-worktrees/feature-ROB-11-native-deploy-builds-trading-decision-spa-dist

echo "==> shell syntax"
bash -n scripts/deploy-native.sh

echo "==> frontend build"
( cd frontend/trading-decision && npm ci && npm run build )
test -f frontend/trading-decision/dist/index.html
grep -q '/trading/decisions/assets/' frontend/trading-decision/dist/index.html

echo "==> make frontend-build"
make frontend-build

echo "==> SPA router pytest"
uv run pytest tests/test_trading_decisions_spa_router.py tests/test_trading_decisions_spa_router_safety.py -v

echo "==> diff hygiene"
git diff --check
git status --short

echo "ALL VALIDATIONS PASSED"
```

---

## 12. Self-review (planner notes)

- **Spec coverage** — every requirement in the issue prompt has a task or acceptance row:
  - "build and verify `frontend/trading-decision/dist/index.html` during release preparation, before symlink switch/restart" → §1.2 + Task 2 placement.
  - "logs node/npm versions" → §5 function body, `log "node ..." log "npm ..."`.
  - "fails fast if `npm` is not available" → §5 function body, exit 78 branch.
  - "runs `npm ci` and `npm run build` in `frontend/trading-decision`" → §5 subshell.
  - "asserts `dist/index.html` exists" → §5 closing `[[ -f $index ]]` check.
  - "Placement should avoid switching `current` or restarting services if frontend build fails" → §4.1 ordering analysis.
  - "Prefer before Alembic migrations if that is safer" → §4.1 + Task 2 anchor between `uv sync --frozen` and `alembic upgrade head`.
  - "Update docs that currently say native deploy does not build the SPA" → Task 3.
  - "Add/adjust lightweight tests/checks where practical" → Task 4 reuses existing pytest + `bash -n` (no new test file; existing CI workflow already greps `dist/index.html`).
  - "Do NOT commit `dist` or `node_modules`" → §9.1 + §9.2 + Task 2.5 cleanup.
- **Placeholder scan** — no TBD/TODO; every code block is concrete enough for an implementer to type or paste.
- **Type/identifier consistency** — `build_frontend`, `$NEW_RELEASE`, `$workspace`, `$index` are used identically across §5, Task 1, and Task 2.
- **Production safety re-check**:
  - `set -Eeuo pipefail` + `trap rollback ERR` are already at `scripts/deploy-native.sh:2,177`. The new function relies on both; placement is between `uv sync --frozen` (line 220) and `alembic upgrade head` (line 226), well before `SWITCHED=1` (line 230).
  - `git clean -fdx -e .venv` (line 217) wipes `node_modules/` and `dist/` at the start of each release prep, so `npm ci` always sees a clean tree.
  - `PATH` at line 33 already contains `$HOME/.hermes/node/bin`, which is where the deploy host's Node lives.
- **Scope creep avoided** — no Docker change, no CI workflow change, no Python change, no auth change, no schema change.

---

## 13. Implementer handoff prompt (Codex YOLO)

Save the block below to `/tmp/ROB-11-codex-yolo-implementer-prompt.md` and launch with:

```bash
codex --yolo exec "$(cat /tmp/ROB-11-codex-yolo-implementer-prompt.md)"
```

```text
You are the implementer for ROB-11 (Native deploy builds trading-decision SPA dist).

Worktree: /Users/mgh3326/work/auto_trader-worktrees/feature-ROB-11-native-deploy-builds-trading-decision-spa-dist
Branch:   feature/ROB-11-native-deploy-builds-trading-decision-spa-dist   (already checked out)
Plan:     docs/plans/ROB-11-native-deploy-spa-build-plan.md  ← READ FULLY, FOLLOW EXACTLY
Linear:   ROB-11  https://linear.app/mgh3326/issue/ROB-11/native-deploy-builds-trading-decision-spa-dist
Base:     origin/main

Project context:
- ROB-6 added the React/Vite/TS workspace at frontend/trading-decision/ and the FastAPI seam at app/routers/trading_decisions_spa.py. ROB-7 added the interactive UI on top.
- frontend/trading-decision/dist/ is gitignored and never committed.
- scripts/deploy-native.sh today does NOT build the SPA. Production has been serving the 503 build-missing fallback. A manual production hotfix ran npm ci && npm run build inside the live release; it is wiped on every redeploy.
- This PR ships ONE shell-level change: a build_frontend function in scripts/deploy-native.sh, called between `uv sync --frozen` and `alembic upgrade head`.

Hard constraints (do NOT relax without confirming with the planner):
1. Do NOT touch anything under app/, alembic/, tests/, frontend/trading-decision/src/, frontend/trading-decision/package.json, frontend/trading-decision/package-lock.json, or .github/workflows/.
2. Do NOT commit frontend/trading-decision/dist/ or frontend/trading-decision/node_modules/. Both must remain gitignored.
3. Do NOT print or log secret values. The new function does not need any secret.
4. Do NOT add `set -x` to the deploy script.
5. Do NOT use `npm install`; always `npm ci` (lockfile-strict).
6. Do NOT flip Dockerfile.api's ENABLE_FRONTEND_BUILD arg.
7. Insert the new helper function and call site EXACTLY where the plan says (§5, §6 Task 1 and Task 2). Placement matters for production safety: failure must abort BEFORE alembic migrations and BEFORE the symlink switch.
8. Use exit code 78 for "npm not found on PATH" to match the existing `require_file` convention.

Build order (one task per commit; run the local validation between tasks):
  1. Task 1 — Add `build_frontend()` to scripts/deploy-native.sh after the `require_file()` helper. Plan §6 Task 1.
  2. Task 2 — Wire a single call site between `uv sync --frozen` and `alembic upgrade head`, with a `log "Building Trading Decision SPA"` line above it. Plan §6 Task 2. Includes a local probe via §6 Task 2 Step 2.4.
  3. Task 3 — Update frontend/trading-decision/README.md "## Production Deployment" section. Plan §6 Task 3.
  4. Task 4 — Run the full validation suite from §11. Plan §6 Task 4.
  5. Task 5 — Push and open a PR against main with the body in §6 Task 5. Plan §6 Task 5.

After every task:
  bash -n scripts/deploy-native.sh
Then commit: `git commit -m "<scope>(rob-11): <verb> <thing>"`.

Final validation block (paste verbatim into the terminal before opening the PR):
  See plan §11.

If anything in the plan looks wrong, contradicts scripts/deploy-native.sh, or contradicts frontend/trading-decision/README.md as currently checked in, STOP and surface the discrepancy in a short note before continuing — do not invent. The script and README on this branch are the authority for what already exists.
```

---

AOE_STATUS: plan_ready
AOE_ISSUE: ROB-11
AOE_ROLE: planner
AOE_PLAN_PATH: docs/plans/ROB-11-native-deploy-spa-build-plan.md
AOE_NEXT: start_codex_yolo_implementer
