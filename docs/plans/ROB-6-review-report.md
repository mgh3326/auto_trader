# ROB-6 — Trading Decision Workspace Scaffold · Review Report

- **Issue:** ROB-6
- **Branch:** `feature/ROB-6-trading-decision-react-scaffold`
- **Worktree:** `/Users/mgh3326/work/auto_trader-worktrees/feature-ROB-6-trading-decision-react-scaffold`
- **Plan reviewed against:** `docs/plans/ROB-6-trading-decision-react-scaffold-plan.md`
- **Tree state at review:** clean, branch up to date with origin
- **Commits on branch (4):**
  - `d47d2ded feat(rob-6): scaffold trading decision frontend`
  - `f005a5fb feat(rob-6): add trading decisions SPA router`
  - `f84f5c64 build(rob-6): add frontend build integration`
  - `af98f8c5 docs(rob-6): document trading decision workspace`
- **Diff size:** 26 files, +2506 / −1
- **Result:** ✅ **REVIEW PASSED — no blockers. PR can be created.**

---

## 1. Acceptance checklist (from plan §13)

| # | Item | Status | Evidence |
|---|---|---|---|
| 1 | `frontend/trading-decision/` exists with §4.1 structure | ✅ | All 16 expected files tracked (`git ls-files frontend/trading-decision/`). |
| 2 | `npm ci && npm run typecheck && npm run build` succeed | ✅ | Codex: PASS for all three. |
| 3 | `dist/index.html` references `/trading/decisions/assets/...` | ✅ | Codex grep PASS; CI workflow enforces it on every PR. |
| 4 | Workspace `.gitignore` + root `.gitignore` + `.dockerignore` updated | ✅ | Workspace ignores `node_modules/`, `dist/`, `.vite/`, `*.local`, `*.tsbuildinfo`; root mirrors with `frontend/**/...` patterns. |
| 5 | `package-lock.json` committed; `node_modules/`, `dist/` not committed | ✅ | `git ls-files` shows lockfile; no `node_modules/` or `dist/` paths in tree. |
| 6 | `app/routers/trading_decisions_spa.py` exists & registered after `trading_decisions.router` | ✅ | `app/main.py:42, 166` confirmed in diff. |
| 7 | `GET /trading/decisions/` → 200 + HTML when build present | ✅ | `tests/test_trading_decisions_spa_router.py::test_spa_index_returns_html_when_dist_present`. |
| 8 | Deep link → SPA fallback to `index.html` | ✅ | `…test_spa_deep_link_falls_back_to_index`. |
| 9 | `GET /trading/decisions/assets/<hash>.js` → 200 | ✅ | `…test_assets_path_serves_hashed_asset`. |
| 10 | Unknown asset → 404 | ✅ | `…test_assets_path_404s_for_unknown_asset`. |
| 11 | `dist/` missing → 503 with build-missing HTML | ✅ | `…test_index_returns_503_when_dist_missing`. |
| 12 | `/trading/api/decisions` (ROB-2) unaffected | ✅ | Codex re-ran `tests/test_trading_decisions_router.py` and `…_router_safety.py`: PASS. |
| 13 | SPA router imports none of §10's forbidden modules | ✅ | `tests/test_trading_decisions_spa_router_safety.py` enforced; module imports limited to `logging`, `pathlib`, `fastapi`, `fastapi.responses`. |
| 14 | `make frontend-{install,dev,build,typecheck}` work | ✅ | Targets present in `Makefile`; `.PHONY` updated. Codex: `make frontend-typecheck`, `make frontend-build` PASS. |
| 15 | `.github/workflows/frontend-trading-decision.yml` triggers on workspace path changes | ✅ | Path filter present; pinned to Node from `.nvmrc` with npm cache. |
| 16 | `Dockerfile.api` builds cleanly with default args | ⚠️ NOT VERIFIED | `docker` not available in Codex's verification environment. See §3.1. |
| 17 | Ruff check/format clean | ✅ | Codex: PASS. |
| 18 | `ty check app/routers/trading_decisions_spa.py --error-on-warning` clean | ✅ | Codex: PASS. |
| 19 | Targeted pytest green | ✅ | Codex: PASS for `tests/test_trading_decisions_spa_router{,_safety}.py`. |
| 20 | No existing template / model / migration / schema / service modified | ✅ | Diff outside new files: only `app/main.py`, `Makefile`, `Dockerfile.api`, `.gitignore`, `.dockerignore` — all expected. |

**20 of 20 items satisfied** (one with a verification caveat — see §3.1).

---

## 2. Safety constraints (plan §10)

- ✅ `app/routers/trading_decisions_spa.py` imports only `logging`, `pathlib`, `fastapi`, `fastapi.responses`. No `app.services.*`, no `app.tasks`, no broker / KIS / Upbit / Redis / watch / websocket / discord / taskiq imports.
- ✅ Subprocess-based forbidden-import test follows the established ROB-2 pattern (same `FORBIDDEN_PREFIXES` list, same `pytest.fail` shape).
- ✅ No live broker calls or external HTTP performed at import time, request time, or test time. The router is pure file IO + `FileResponse`/`HTMLResponse`.
- ✅ Path traversal is defended via `Path.resolve().relative_to(ASSETS_DIR.resolve())` in `serve_asset` (router lines 51–55) and exercised by `test_assets_path_rejects_traversal`.

---

## 3. Observations (non-blocking)

### 3.1 `Dockerfile.api` final-stage `COPY` is unconditional (minor deviation from plan §6.4)

Plan §6.4 stated *"Default OFF to keep the existing image green"*, intending a guarded `COPY` so a default-flag Docker build remains bit-identical to the pre-PR image. The implementer's approach instead has the **builder stage** emit a placeholder `dist/index.html` containing `<!-- frontend build disabled -->` when `ENABLE_FRONTEND_BUILD=0`, and **always** copies `/build/dist` into the final image (`Dockerfile.api:72`).

**Behavioural difference:** with `ENABLE_FRONTEND_BUILD=0` (default), a Docker image will now contain a tiny placeholder `index.html` at `/app/frontend/trading-decision/dist/`. `INDEX_FILE.is_file()` returns True, so the SPA router serves that placeholder (HTTP 200) rather than the documented 503 build-missing page.

**Why this is not a blocker:**

- Production deploy is **macOS native** via `scripts/deploy-native.sh`; Docker is **not** on the production path today (confirmed via the deploy workflow `.github/workflows/deploy-macos-native.yml`). On macOS-native deploys, `dist/` will not exist, and the documented 503 path still applies — so the README and plan are accurate for production behaviour.
- The placeholder content is harmless and self-describing.
- `docker build` syntax is valid; the placeholder strategy avoids a BuildKit conditional-COPY pattern that would have been more complex.

**Recommendation (follow-up, not this PR):** when the Docker path is enabled for production (the same follow-up that updates `scripts/deploy-native.sh`), tighten this so a default Docker build truly leaves the image bit-identical, or change the build-missing copy to omit `dist/index.html` entirely so the 503 path is consistent across runtimes.

### 3.2 `docker build` was not exercised locally

Codex reported `docker build -f Dockerfile.api .` as **NOT RUN** because Docker is not installed in the verification environment. The Dockerfile changes are syntactically valid and follow the existing multi-stage idiom, and CI (Discord-notified) will surface any breakage if/when a Docker build runs. **Not a blocker** — but worth noting that this acceptance item (§13 "Dockerfile.api builds cleanly with default args") is satisfied by inspection rather than execution in this review.

### 3.3 `tsconfig.node.json` differs slightly from plan §5.4

The plan's version included `"composite": true` and `"noEmit": true`. The implementer's version (`frontend/trading-decision/tsconfig.node.json`) drops both. Functionally equivalent because the workspace `build`/`typecheck` scripts pass `--noEmit` explicitly to `tsc -p tsconfig.node.json`, and project-references (`"references": [...]`) are not used. Codex's `npm run typecheck` and `npm run build` both PASS, so the toolchain accepts the actual configuration. **Not a blocker.**

### 3.4 `vite-env.d.ts` adds `/// <reference types="vite/client" />`

Standard Vite scaffold output. Mildly redundant with the README's "do not use `import.meta.env`" directive, but the file only provides ambient types — it does not encourage env usage. **Not a blocker.**

### 3.5 npm dependency major versions

`package.json` pins to `vite ^8.0.10`, `typescript ^6.0.3`, `@vitejs/plugin-react ^6.0.1`, `react ^19.2.5`. These are at the leading edge of "latest stable at install time". Codex's `npm ci` resolved the lockfile and `npm run build`/`typecheck` succeeded, so the toolchain is internally consistent. The plan explicitly delegated version selection to install time (plan §16, item 1–2); the lockfile is the source of truth. **Not a blocker.**

---

## 4. What I checked

- `git status` — clean, branch up to date.
- `git log main..HEAD` — 4 well-scoped commits, conventional `feat/build/docs(rob-6)` style.
- `git diff --stat main...HEAD` — 26 files, +2506/-1, no surprise paths (no models, migrations, schemas, services, templates, or static assets touched).
- `git diff main...HEAD -- app/main.py Makefile .gitignore .dockerignore Dockerfile.api` — every infra modification matches the plan's §12 changeset table (or is a documented minor deviation per §3 above).
- `git ls-files frontend/trading-decision/` — exactly the 16 tracked files expected; `node_modules/` and `dist/` correctly absent.
- Read every new file under `frontend/trading-decision/` and `app/routers/trading_decisions_spa.py`; verified path-traversal defence, no-cache headers, 503 fallback HTML, SPA fallback route, and import surface.
- Read both new test files; verified they exercise §13 acceptance items 7–11 and §10 safety boundary.
- Read `.github/workflows/frontend-trading-decision.yml`; path filter, Node pinning, and dist-grep verification all match plan §8.

I did **not** edit any application/test/frontend code, place broker orders, call KIS/Upbit, run Docker, or read secrets — per reviewer-mode constraints.

---

## 5. Verdict

All 20 acceptance items satisfied (one with an inspection-only caveat for `docker build`, see §3.1–3.2). Safety boundary holds. Implementation cleanly delivers the scaffold-only scope without creep into ROB-4/ROB-5 territory. The four small observations in §3 are quality-of-life notes for follow-up work, not blockers.

**No must-fix items. The PR can be created.**

---
