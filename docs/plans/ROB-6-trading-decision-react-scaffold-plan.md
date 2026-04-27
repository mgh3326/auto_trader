# ROB-6 — Trading Decision Workspace · React/Vite/TS Scaffold Plan

- **PR scope:** Prompt 3 of `~/.hermes/workspace/prompts/auto_trader_trading_decision_workspace_roadmap.md` only.
- **Branch / worktree:** `feature/ROB-6-trading-decision-react-scaffold` at
  `/Users/mgh3326/work/auto_trader-worktrees/feature-ROB-6-trading-decision-react-scaffold`.
- **Status:** Plan only. No implementation yet.
- **Depends on:** ROB-1 / PR #595 (DB schema) and ROB-2 / PR #597 (API contract) — both merged to `main` (latest at `70be680a`).
- **Implementer:** Codex with `codex --yolo` (Codex quota currently available).

> ⚠️ This PR ships a **frontend workspace scaffold + a single hello page + the FastAPI seam to serve it**. Decision UI (ROB-4 / future), outcome / analytics UI (ROB-5 / future), Discord delivery, periodic reassessment, live trading, broker / watch / KIS / Upbit / Redis side effects are **explicitly out of scope**.

---

## 1. Goal

Introduce a self-contained React + Vite + TypeScript workspace inside the existing `auto_trader` repo at `frontend/trading-decision/`, plus the minimum FastAPI plumbing to serve its built assets at `/trading/decisions/`, plus the docs an engineer needs to develop, build, and deploy it.

Two non-negotiables:

1. **No collision with `/trading/api/*`** (ROB-2 endpoints) and no rewrite of the existing Jinja pages (`portfolio_decision_desk.html`, `screener_*.html`, `pending_orders_dashboard.html`, …). The SPA route lives entirely under `/trading/decisions/...`.
2. **No execution-path side effects in the new FastAPI module.** Same forbidden-import boundary as ROB-1/2 (see §10).

---

## 2. In-scope vs Out-of-scope

| Area | In scope (this PR) | Deferred |
|---|---|---|
| `frontend/trading-decision/` Vite + React + TS workspace | ✅ | — |
| Single "hello decision workspace" page | ✅ | — |
| Vite config (base path, dev proxy, build output) | ✅ | — |
| FastAPI SPA serving module (`app/routers/trading_decisions_spa.py`) | ✅ | — |
| Router registration in `app/main.py` | ✅ | — |
| Forbidden-import safety test for the new router | ✅ | — |
| Router unit tests (200/SPA fallback/missing-build/asset path) | ✅ | — |
| `Makefile` targets (`frontend-install`, `frontend-dev`, `frontend-build`, `frontend-typecheck`) | ✅ | — |
| `.gitignore` + `.dockerignore` updates (`node_modules/`, `frontend/**/dist/`) | ✅ | — |
| GitHub Actions workflow that builds & typechecks the workspace on PR | ✅ | — |
| `frontend/trading-decision/README.md` (dev/build/deploy quickstart) | ✅ | — |
| `Dockerfile.api` Node build stage producing `dist/` | ✅ (additive, OFF by default — see §6.4) | full enablement |
| Decision workspace UI (proposal cards, accept/modify/defer flow) | ❌ | ROB-4 / future |
| Outcome / analytics UI | ❌ | ROB-5 / future |
| Live broker / watch / KIS / Upbit / Redis / Discord effects | ❌ (forbidden — §10) | — |
| Rewriting existing Jinja templates as React | ❌ | future |
| Auth refactor (existing `AuthMiddleware` covers `/trading/*` already) | ❌ | — |
| Updates to `scripts/deploy-native.sh` to install Node and run the SPA build | ❌ | follow-up issue |
| Linting/Prettier/ESLint inside the workspace beyond `tsc --noEmit` typecheck | ❌ | follow-up |
| Test runner inside the workspace (vitest / RTL) | ❌ | when there's UI worth testing (ROB-4) |
| Tailwind / component library / state management / routing libs | ❌ | when needed |

---

## 3. Workflow the scaffold must support

```text
# Day 1 (this PR):
$ make frontend-install                           # one-time npm ci
$ make frontend-dev                               # Vite dev server on http://localhost:5173, proxies /trading/api → :8000
$ make dev                                        # FastAPI on :8000 (existing)
$ open http://localhost:5173/trading/decisions/   # hello page

# Production-ish smoke:
$ make frontend-build                             # produces frontend/trading-decision/dist/
$ make dev                                        # FastAPI serves dist/ at /trading/decisions/
$ open http://localhost:8000/trading/decisions/   # same hello page from baked assets

# Day N+1 (ROB-4 implementer):
$ # adds React components under src/, talks to /trading/api/decisions endpoints
$ # this PR's seam is unchanged
```

---

## 4. File structure & boundaries

### 4.1 New files

```text
frontend/trading-decision/
├── .gitignore                       # node_modules/, dist/, .vite/
├── .nvmrc                           # 20  (Node LTS pin)
├── README.md                        # quickstart: npm ci / npm run dev / npm run build / where assets land
├── package.json
├── package-lock.json                # committed
├── tsconfig.json                    # app build config
├── tsconfig.node.json               # vite.config.ts compile config
├── vite.config.ts
├── index.html
└── src/
    ├── main.tsx                     # React 19 entry, mounts <App/>
    ├── App.tsx                      # <HelloDecision/>
    ├── App.css                      # minimal styles
    ├── api/
    │   └── client.ts                # tiny fetch wrapper anchored at "/trading/api"
    ├── components/
    │   └── HelloDecision.tsx        # the only hello component this PR ships
    └── pages/
        └── HelloPage.tsx            # placeholder for future routes
```

```text
app/routers/
└── trading_decisions_spa.py         # NEW — serves dist/index.html + dist/assets under /trading/decisions
```

```text
tests/
├── test_trading_decisions_spa_router.py        # NEW — unit tests
└── test_trading_decisions_spa_router_safety.py # NEW — forbidden-import safety
```

```text
.github/workflows/
└── frontend-trading-decision.yml    # NEW — runs npm ci / typecheck / build on PRs touching the workspace
```

### 4.2 Files modified

| File | Change | Why |
|---|---|---|
| `app/main.py` | `from app.routers import trading_decisions_spa` and `app.include_router(trading_decisions_spa.router)` next to `trading_decisions.router` (after it) | Wire the SPA route into the app factory. |
| `Makefile` | Add targets: `frontend-install`, `frontend-dev`, `frontend-build`, `frontend-typecheck` (all are wrappers around `npm` calls in `frontend/trading-decision/`). Add them to the existing `.PHONY` list. | Match the repo's `make`-driven dev ergonomics. |
| `.gitignore` | Append: `frontend/**/node_modules/`, `frontend/**/dist/`, `frontend/**/.vite/` | Prevent accidental commit of build artifacts. The repo's existing `dist/` rule already covers `**/dist/`, but the explicit `frontend/**/dist/` line documents intent and survives reorganization. |
| `.dockerignore` | Append: `frontend/**/node_modules/`, `frontend/**/.vite/` | Keep the Docker build context lean. **Do not** ignore `frontend/trading-decision/dist/` here — once §6.4 is enabled, the final stage needs it. (For now, with §6.4 OFF by default, it doesn't matter; the line is forward-compatible.) |
| `Dockerfile.api` | Add an optional Node build stage `frontend-builder` producing `/build/dist`, and a guarded `COPY` into the final image at `/app/frontend/trading-decision/dist`. Gated by an `ARG ENABLE_FRONTEND_BUILD=0`. Default OFF to keep the existing image green. | See §6.4. |

No changes to: any ROB-1/ROB-2 file, models, migrations, schemas, services, templates, `static/`, Caddyfile (CSP already permits same-origin scripts), `.circleci/config.yml` (the dedicated GitHub workflow is enough — adding a Node executor to CircleCI is YAGNI).

---

## 5. Vite + React + TS config decisions

### 5.1 `package.json`

```json
{
  "name": "@auto-trader/trading-decision",
  "private": true,
  "version": "0.0.0",
  "type": "module",
  "engines": { "node": ">=20.0.0" },
  "scripts": {
    "dev": "vite",
    "build": "tsc -b && vite build",
    "preview": "vite preview",
    "typecheck": "tsc -b --noEmit"
  },
  "dependencies": {
    "react": "^19.0.0",
    "react-dom": "^19.0.0"
  },
  "devDependencies": {
    "@types/react": "^19.0.0",
    "@types/react-dom": "^19.0.0",
    "@vitejs/plugin-react": "^5.0.0",
    "typescript": "^5.6.0",
    "vite": "^7.0.0"
  }
}
```

> Implementer: pin to the latest stable majors at install time (Codex should run `npm install react@latest react-dom@latest @types/react@latest @types/react-dom@latest @vitejs/plugin-react@latest typescript@latest vite@latest --save-dev` after `npm init`, then commit the resulting `package-lock.json`). The version ranges above are guidance; the lockfile is the source of truth.

### 5.2 `vite.config.ts`

```ts
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  base: "/trading/decisions/",          // ALL asset URLs are emitted with this prefix
  build: {
    outDir: "dist",
    assetsDir: "assets",
    sourcemap: true,
    emptyOutDir: true,
  },
  server: {
    port: 5173,
    strictPort: true,
    proxy: {
      "/trading/api":   { target: "http://localhost:8000", changeOrigin: false },
      "/auth":          { target: "http://localhost:8000", changeOrigin: false },
      "/api":           { target: "http://localhost:8000", changeOrigin: false },
    },
  },
});
```

**Why these choices:**
- `base: "/trading/decisions/"` is the single most important config — every emitted `<script src>` and `<link href>` becomes `/trading/decisions/assets/<hash>.{js,css}`, which lines up exactly with the FastAPI mount in §6.
- Dev-time proxy lets the React dev server hit the real FastAPI for `/trading/api/...` (ROB-2 endpoints) while serving the SPA itself with HMR.
- `strictPort: true` makes the dev server fail loudly instead of drifting to a different port.
- `sourcemap: true` is cheap insurance until the bundle becomes large enough to hurt; revisit in ROB-4 if needed.

### 5.3 `tsconfig.json`

```json
{
  "compilerOptions": {
    "target": "ES2022",
    "lib": ["ES2022", "DOM", "DOM.Iterable"],
    "module": "ESNext",
    "moduleResolution": "bundler",
    "jsx": "react-jsx",
    "strict": true,
    "noUncheckedIndexedAccess": true,
    "noImplicitOverride": true,
    "noFallthroughCasesInSwitch": true,
    "skipLibCheck": true,
    "esModuleInterop": true,
    "isolatedModules": true,
    "verbatimModuleSyntax": true,
    "useDefineForClassFields": true,
    "forceConsistentCasingInFileNames": true,
    "resolveJsonModule": true
  },
  "include": ["src"],
  "references": [{ "path": "./tsconfig.node.json" }]
}
```

### 5.4 `tsconfig.node.json`

```json
{
  "compilerOptions": {
    "target": "ES2022",
    "lib": ["ES2023"],
    "module": "ESNext",
    "moduleResolution": "bundler",
    "skipLibCheck": true,
    "strict": true,
    "noEmit": true,
    "composite": true,
    "allowSyntheticDefaultImports": true
  },
  "include": ["vite.config.ts"]
}
```

### 5.5 `index.html`

```html
<!doctype html>
<html lang="en">
  <head>
    <meta charset="UTF-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>Trading Decision Workspace · auto_trader</title>
  </head>
  <body>
    <div id="root"></div>
    <script type="module" src="/src/main.tsx"></script>
  </body>
</html>
```

### 5.6 `src/main.tsx`

```tsx
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import App from "./App";
import "./App.css";

const container = document.getElementById("root");
if (!container) throw new Error("#root element not found");

createRoot(container).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
```

### 5.7 `src/App.tsx`

```tsx
import HelloDecision from "./components/HelloDecision";

export default function App() {
  return <HelloDecision />;
}
```

### 5.8 `src/components/HelloDecision.tsx`

```tsx
export default function HelloDecision() {
  return (
    <main className="hello-decision">
      <h1>Trading Decision Workspace</h1>
      <p>
        Hello from React + Vite + TypeScript. This page is the scaffold for the
        upcoming decision UI (ROB-4). It is intentionally empty.
      </p>
      <p>
        Backend endpoints live under <code>/trading/api/decisions</code> and{" "}
        <code>/trading/api/proposals</code> (see ROB-2).
      </p>
    </main>
  );
}
```

### 5.9 `src/api/client.ts`

```ts
const API_BASE = "/trading/api";

export async function apiFetch<T>(
  path: string,
  init?: RequestInit,
): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, {
    credentials: "same-origin",
    headers: { "Content-Type": "application/json", ...(init?.headers ?? {}) },
    ...init,
  });
  if (!res.ok) {
    throw new Error(`Request failed: ${res.status} ${res.statusText}`);
  }
  return (await res.json()) as T;
}
```

### 5.10 `src/App.css`

```css
.hello-decision { max-width: 720px; margin: 4rem auto; padding: 0 1.5rem;
  font: 16px/1.6 ui-sans-serif, system-ui, sans-serif; color: #1a1a1a; }
.hello-decision h1 { font-size: 1.75rem; margin-bottom: 1rem; }
.hello-decision code { background: #f3f3f3; padding: 0.1rem 0.35rem; border-radius: 3px; }
```

### 5.11 `src/pages/HelloPage.tsx`

```tsx
import HelloDecision from "../components/HelloDecision";

export default function HelloPage() {
  return <HelloDecision />;
}
```

> The `pages/` and `api/` folders are intentionally created with one tiny file each so the directory structure required by the roadmap (`src/api/`, `src/components/`, `src/pages/`) is real, not aspirational. ROB-4 will fill them in.

### 5.12 `.gitignore` (workspace-local)

```text
node_modules/
dist/
.vite/
*.local
```

### 5.13 `.nvmrc`

```text
20
```

---

## 6. FastAPI integration (`app/routers/trading_decisions_spa.py`)

### 6.1 Routing surface

| Route | Method | Purpose | Behavior |
|---|---|---|---|
| `/trading/decisions/assets/{full_path:path}` | GET | Hashed Vite assets | Stream from `dist/assets/<full_path>` via `FileResponse`; 404 if file is missing. |
| `/trading/decisions/` | GET | SPA shell | Return `dist/index.html` (200 + `text/html`); `Cache-Control: no-cache` so deploys don't get pinned by browser cache. |
| `/trading/decisions/{full_path:path}` | GET | SPA client-side routing fallback | Same `dist/index.html` as above (so deep links like `/trading/decisions/inbox/2026-04-27` work). |
| Anything else | — | Unchanged | ROB-2 router still owns `/trading/api/decisions|proposals|...`. |

> The `assets/{full_path:path}` route is registered **before** the catch-all `/{full_path:path}` so FastAPI matches the more specific route first.

### 6.2 Module sketch

```python
# app/routers/trading_decisions_spa.py
"""SPA shell router for the Trading Decision Workspace (ROB-6).

Serves the prebuilt React + Vite bundle from
``frontend/trading-decision/dist/`` under ``/trading/decisions/``.

This module MUST NOT import any broker, watch, Redis, KIS, Upbit, or task-queue
module — see tests/test_trading_decisions_spa_router_safety.py.
"""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import FileResponse, HTMLResponse, Response

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/trading/decisions", tags=["trading-decisions-spa"])

REPO_ROOT = Path(__file__).resolve().parents[2]
DIST_DIR = REPO_ROOT / "frontend" / "trading-decision" / "dist"
INDEX_FILE = DIST_DIR / "index.html"
ASSETS_DIR = DIST_DIR / "assets"

_BUILD_MISSING_HTML = """\
<!doctype html><html><head><meta charset="utf-8"><title>Trading Decision Workspace · build missing</title></head>
<body style="font:16px/1.6 ui-sans-serif,system-ui;max-width:680px;margin:4rem auto;padding:0 1rem;">
<h1>Trading Decision Workspace · build missing</h1>
<p>The React bundle has not been built yet. Run:</p>
<pre><code>cd frontend/trading-decision &amp;&amp; npm ci &amp;&amp; npm run build</code></pre>
<p>or, from the repo root: <code>make frontend-install &amp;&amp; make frontend-build</code>.</p>
</body></html>
"""


def _no_cache(response: Response) -> Response:
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return response


@router.get("/assets/{asset_path:path}", include_in_schema=False)
async def serve_asset(asset_path: str) -> FileResponse:
    candidate = (ASSETS_DIR / asset_path).resolve()
    try:
        candidate.relative_to(ASSETS_DIR.resolve())
    except ValueError:
        # Defends against `..` traversal even though Starlette normalises paths.
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    if not candidate.is_file():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    return FileResponse(candidate)


@router.get("/", include_in_schema=False)
async def spa_index(request: Request) -> Response:
    return _serve_index()


@router.get("/{full_path:path}", include_in_schema=False)
async def spa_fallback(full_path: str, request: Request) -> Response:
    # Never shadow API. /trading/api/... is owned by trading_decisions.router and
    # registered before this router in app/main.py, so it always matches first.
    return _serve_index()


def _serve_index() -> Response:
    if not INDEX_FILE.is_file():
        logger.warning(
            "SPA build missing at %s — returning 503 build-missing page", INDEX_FILE
        )
        response = HTMLResponse(
            content=_BUILD_MISSING_HTML,
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        )
        return _no_cache(response)
    response = FileResponse(INDEX_FILE, media_type="text/html")
    return _no_cache(response)
```

**Why a hand-rolled router instead of `app.mount(StaticFiles(html=True))`:**
- We need a real **SPA fallback**: any unmatched path under `/trading/decisions/` must serve `index.html` so client-side routing works. `StaticFiles(html=True)` does not do that — it 404s on unknown paths.
- We need a developer-friendly **build-missing** page (HTTP 503 with explicit instructions). `StaticFiles` would just 404.
- We want a **forbidden-import safety test** (§10) on the module. A dedicated module is the clean unit to run that test against.

### 6.3 Wiring in `app/main.py`

Add after line `app.include_router(trading_decisions.router)` (around `app/main.py:164`):

```python
from app.routers import (
    ...,
    trading_decisions,
    trading_decisions_spa,        # NEW
    ...,
)

# inside create_app():
app.include_router(trading_decisions.router)
app.include_router(trading_decisions_spa.router)   # NEW — must be after trading_decisions.router
```

**Order matters.** `trading_decisions.router` declares the `/trading/api/...` routes; registering it first guarantees they win over any (non-existent but defensively considered) overlap. `trading_decisions_spa.router`'s prefix `/trading/decisions` is already disjoint, so this is belt-and-braces.

`AuthMiddleware` (already global) will require login on `/trading/decisions/...` exactly like every other `/trading/*` path. No new middleware. `TemplateFormCSRFMiddleware` already exempts `^/trading/` (`app/main.py:192`).

### 6.4 Dockerfile.api Node build stage (additive, default OFF)

Append a builder stage and a guarded copy. The default `ENABLE_FRONTEND_BUILD=0` keeps the current image identical, so this PR cannot regress production. ROB-4 (or a follow-up) flips the flag.

```dockerfile
# ... existing builder + final stages above ...

# ==============================================================================
# STAGE 1b (optional): frontend-builder — builds the React/Vite SPA
# Toggle with: --build-arg ENABLE_FRONTEND_BUILD=1
# ==============================================================================
FROM --platform=$BUILDPLATFORM node:20-alpine AS frontend-builder
ARG ENABLE_FRONTEND_BUILD=0
WORKDIR /build
COPY frontend/trading-decision/package.json frontend/trading-decision/package-lock.json ./
RUN if [ "$ENABLE_FRONTEND_BUILD" = "1" ]; then npm ci; fi
COPY frontend/trading-decision/ ./
RUN if [ "$ENABLE_FRONTEND_BUILD" = "1" ]; then npm run build; else mkdir -p dist && echo '<!-- frontend build disabled -->' > dist/index.html; fi
```

In the existing `final` stage, after `COPY . .`, add:

```dockerfile
ARG ENABLE_FRONTEND_BUILD=0
COPY --from=frontend-builder /build/dist /app/frontend/trading-decision/dist
```

**Why default OFF:** until `scripts/deploy-native.sh` is updated (separate follow-up issue), production deploys use the macOS native path, not Docker. Flipping this on now adds a Node toolchain dependency to every Docker build for no production benefit. Keep the seam, defer the flip.

The `Makefile`'s `docker-build` target stays unchanged. A new optional `docker-build-with-frontend` target is **not** added — implementer can pass `--build-arg` manually until the flip happens.

---

## 7. Makefile additions

Append to the existing `.PHONY` line and add:

```makefile
.PHONY: ... frontend-install frontend-dev frontend-build frontend-typecheck

frontend-install: ## Install React/Vite workspace deps (npm ci)
	cd frontend/trading-decision && npm ci

frontend-dev: ## Start Vite dev server on :5173 (requires `make dev` for the API on :8000)
	cd frontend/trading-decision && npm run dev

frontend-build: ## Build the React/Vite workspace into frontend/trading-decision/dist/
	cd frontend/trading-decision && npm run build

frontend-typecheck: ## Run tsc --noEmit on the React/Vite workspace
	cd frontend/trading-decision && npm run typecheck
```

---

## 8. CI workflow (`.github/workflows/frontend-trading-decision.yml`)

```yaml
name: Frontend - Trading Decision

on:
  push:
    branches: [main]
    paths:
      - "frontend/trading-decision/**"
      - ".github/workflows/frontend-trading-decision.yml"
  pull_request:
    paths:
      - "frontend/trading-decision/**"
      - ".github/workflows/frontend-trading-decision.yml"

jobs:
  build:
    runs-on: ubuntu-latest
    timeout-minutes: 10
    defaults:
      run:
        working-directory: frontend/trading-decision
    steps:
      - uses: actions/checkout@v5
      - uses: actions/setup-node@v4
        with:
          node-version-file: frontend/trading-decision/.nvmrc
          cache: "npm"
          cache-dependency-path: frontend/trading-decision/package-lock.json
      - run: npm ci
      - run: npm run typecheck
      - run: npm run build
      - name: Verify dist/index.html exists and references hashed assets
        run: |
          test -f dist/index.html
          grep -q '/trading/decisions/assets/' dist/index.html
```

Why a dedicated workflow instead of bolting Node onto `test.yml`: keeps the Python lint/test pipeline single-purpose and cache-friendly; pathfilter ensures no Node spin-up for backend-only PRs.

CircleCI is left untouched (it already runs Python lint + test). The GitHub workflow is the source of truth for SPA build health.

---

## 9. Tests

### 9.1 `tests/test_trading_decisions_spa_router.py`

Each test uses a `TestClient`. We do **not** stand up the real `dist/` directory in pytest; instead we monkeypatch the module-level `INDEX_FILE` / `ASSETS_DIR` paths to a tmp dir to exercise both the "build present" and "build missing" branches deterministically.

```python
"""Unit tests for the Trading Decision Workspace SPA router (ROB-6)."""
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.routers import trading_decisions_spa


def _make_client(tmp_path: Path, *, with_dist: bool) -> TestClient:
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "assets").mkdir()
    if with_dist:
        (dist / "index.html").write_text(
            '<!doctype html><html><head>'
            '<script type="module" src="/trading/decisions/assets/index-abc123.js"></script>'
            '</head><body><div id="root"></div></body></html>',
            encoding="utf-8",
        )
        (dist / "assets" / "index-abc123.js").write_text("export const x = 1;")
        (dist / "assets" / "logo.svg").write_text("<svg/>")
    # Repoint module globals to the temp dist so each test is hermetic.
    trading_decisions_spa.DIST_DIR = dist  # type: ignore[attr-defined]
    trading_decisions_spa.INDEX_FILE = dist / "index.html"  # type: ignore[attr-defined]
    trading_decisions_spa.ASSETS_DIR = dist / "assets"  # type: ignore[attr-defined]

    app = FastAPI()
    app.include_router(trading_decisions_spa.router)
    return TestClient(app)


@pytest.mark.unit
def test_spa_index_returns_html_when_dist_present(tmp_path):
    client = _make_client(tmp_path, with_dist=True)
    res = client.get("/trading/decisions/")
    assert res.status_code == 200
    assert res.headers["content-type"].startswith("text/html")
    assert "/trading/decisions/assets/index-abc123.js" in res.text
    assert res.headers["cache-control"].startswith("no-cache")


@pytest.mark.unit
def test_spa_deep_link_falls_back_to_index(tmp_path):
    client = _make_client(tmp_path, with_dist=True)
    res = client.get("/trading/decisions/inbox/2026-04-27")
    assert res.status_code == 200
    assert "<div id=\"root\">" in res.text


@pytest.mark.unit
def test_assets_path_serves_hashed_asset(tmp_path):
    client = _make_client(tmp_path, with_dist=True)
    res = client.get("/trading/decisions/assets/index-abc123.js")
    assert res.status_code == 200
    assert "export const x = 1;" in res.text


@pytest.mark.unit
def test_assets_path_404s_for_unknown_asset(tmp_path):
    client = _make_client(tmp_path, with_dist=True)
    res = client.get("/trading/decisions/assets/missing.js")
    assert res.status_code == 404


@pytest.mark.unit
def test_assets_path_rejects_traversal(tmp_path):
    client = _make_client(tmp_path, with_dist=True)
    # Starlette normalises ../ in the URL path, but we still defend in the handler.
    res = client.get("/trading/decisions/assets/..%2Fsecret.txt")
    assert res.status_code in (400, 404)


@pytest.mark.unit
def test_index_returns_503_when_dist_missing(tmp_path):
    client = _make_client(tmp_path, with_dist=False)
    res = client.get("/trading/decisions/")
    assert res.status_code == 503
    assert "build missing" in res.text.lower()
    assert "npm run build" in res.text
```

### 9.2 `tests/test_trading_decisions_spa_router_safety.py`

Mirrors `tests/test_trading_decisions_router_safety.py` (the ROB-2 pattern):

```python
"""Safety test: the SPA router must not import execution paths."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

FORBIDDEN_PREFIXES = [
    "app.services.kis",
    "app.services.kis_trading_service",
    "app.services.kis_trading_contracts",
    "app.services.upbit",
    "app.services.upbit_websocket",
    "app.services.brokers",
    "app.services.order_service",
    "app.services.fill_notification",
    "app.services.execution_event",
    "app.services.redis_token_manager",
    "app.services.kis_websocket",
    "app.services.kis_websocket_internal",
    "app.tasks",
]


@pytest.mark.unit
def test_spa_router_module_does_not_import_execution_paths():
    project_root = Path(__file__).resolve().parent.parent
    script = """
import importlib, json, sys
importlib.import_module("app.routers.trading_decisions_spa")
print(json.dumps(sorted(sys.modules)))
"""
    env = os.environ.copy()
    env["PYTHONPATH"] = str(project_root)
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=project_root,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    loaded = set(json.loads(result.stdout))
    violations = sorted(
        m for m in loaded for p in FORBIDDEN_PREFIXES
        if m == p or m.startswith(f"{p}.")
    )
    if violations:
        pytest.fail(f"Forbidden execution-path imports: {violations}")
```

### 9.3 Verification commands

```bash
# Backend
uv run pytest tests/test_trading_decisions_spa_router.py -v
uv run pytest tests/test_trading_decisions_spa_router_safety.py -v
uv run ruff check app/routers/trading_decisions_spa.py tests/test_trading_decisions_spa_router.py tests/test_trading_decisions_spa_router_safety.py
uv run ruff format --check app/routers/trading_decisions_spa.py tests/test_trading_decisions_spa_router.py tests/test_trading_decisions_spa_router_safety.py
uv run ty check app/routers/trading_decisions_spa.py --error-on-warning

# Frontend
cd frontend/trading-decision
npm ci
npm run typecheck
npm run build
test -f dist/index.html
grep -q '/trading/decisions/assets/' dist/index.html
cd -

# End-to-end smoke
make dev &        # FastAPI on :8000
sleep 3
curl -sf http://localhost:8000/trading/decisions/ | grep -q "<div id=\"root\""
curl -sf -o /dev/null -w '%{http_code}\n' http://localhost:8000/trading/decisions/assets/  # expect 404 (directory has no listing)
kill %1
```

---

## 10. Side-effect safety boundaries (forbidden imports)

`app/routers/trading_decisions_spa.py` MUST NOT import (directly or transitively) any of:

```text
app.services.kis
app.services.kis_trading_service
app.services.kis_trading_contracts
app.services.upbit
app.services.upbit_websocket
app.services.brokers
app.services.order_service
app.services.fill_notification
app.services.execution_event
app.services.redis_token_manager
app.services.kis_websocket
app.services.kis_websocket_internal
app.tasks
```

Same list as ROB-1 / ROB-2. Enforced by `tests/test_trading_decisions_spa_router_safety.py` (§9.2). The module's permitted imports are: `pathlib`, `logging`, `fastapi`, and `fastapi.responses` — nothing else.

---

## 11. `frontend/trading-decision/README.md`

Must cover, in this order, with literal commands:

1. **Prerequisites:** Node ≥ 20 (use `nvm use` to pick up `.nvmrc`), `npm`.
2. **Install:** `npm ci` (or `make frontend-install` from repo root).
3. **Local dev (fast iteration):** Run FastAPI in one terminal with `make dev`, run Vite dev server in another with `make frontend-dev`. Open http://localhost:5173/trading/decisions/. Vite proxies `/trading/api`, `/auth`, `/api` to `http://localhost:8000`.
4. **Local prod-mode smoke:** `make frontend-build` then visit http://localhost:8000/trading/decisions/. FastAPI serves the built `dist/`.
5. **Where assets land:** `frontend/trading-decision/dist/`. Served by FastAPI from `/trading/decisions/`. The `dist/` directory is gitignored.
6. **Production deployment:** Currently OFF by default. The macOS native deploy (`scripts/deploy-native.sh`) does not yet build the SPA — calling `/trading/decisions/` in prod will return the HTTP 503 build-missing page. Tracked in a follow-up issue. To preview the production-baked path, run `make frontend-build` on the deploy host before restarting the API.
7. **Adding new components:** drop them under `src/components/`. Add new pages under `src/pages/`. The api client at `src/api/client.ts` already targets `/trading/api`. ROB-4 will introduce real routing.
8. **Constraints (hard):** No environment variables read at runtime in this scaffold (no `import.meta.env` usage in `src/`). No third-party state libs (Redux, Zustand, …). No router (React Router, …). Add them in ROB-4 with a design doc.
9. **Troubleshooting:** Build failed with "Failed to resolve module"? You probably forgot `npm ci`. 503 in production? `dist/` is missing — see step 6. CSP refuses scripts? Verify the new bundle uses same-origin `/trading/decisions/assets/...` paths (it should — see `vite.config.ts` `base`).

---

## 12. File-by-file changeset

| File | Action | Notes |
|---|---|---|
| `frontend/trading-decision/.gitignore` | **new** | §5.12 |
| `frontend/trading-decision/.nvmrc` | **new** | `20` |
| `frontend/trading-decision/README.md` | **new** | §11 |
| `frontend/trading-decision/package.json` | **new** | §5.1 |
| `frontend/trading-decision/package-lock.json` | **new** | committed (lockfile) |
| `frontend/trading-decision/tsconfig.json` | **new** | §5.3 |
| `frontend/trading-decision/tsconfig.node.json` | **new** | §5.4 |
| `frontend/trading-decision/vite.config.ts` | **new** | §5.2 |
| `frontend/trading-decision/index.html` | **new** | §5.5 |
| `frontend/trading-decision/src/main.tsx` | **new** | §5.6 |
| `frontend/trading-decision/src/App.tsx` | **new** | §5.7 |
| `frontend/trading-decision/src/App.css` | **new** | §5.10 |
| `frontend/trading-decision/src/api/client.ts` | **new** | §5.9 |
| `frontend/trading-decision/src/components/HelloDecision.tsx` | **new** | §5.8 |
| `frontend/trading-decision/src/pages/HelloPage.tsx` | **new** | §5.11 |
| `app/routers/trading_decisions_spa.py` | **new** | §6.2 |
| `tests/test_trading_decisions_spa_router.py` | **new** | §9.1 |
| `tests/test_trading_decisions_spa_router_safety.py` | **new** | §9.2 |
| `.github/workflows/frontend-trading-decision.yml` | **new** | §8 |
| `app/main.py` | **modify** | import + `include_router(trading_decisions_spa.router)` after `trading_decisions.router` |
| `Makefile` | **modify** | add `frontend-*` targets and append to `.PHONY` |
| `.gitignore` | **modify** | append `frontend/**/node_modules/`, `frontend/**/dist/`, `frontend/**/.vite/` |
| `.dockerignore` | **modify** | append `frontend/**/node_modules/`, `frontend/**/.vite/` |
| `Dockerfile.api` | **modify** | additive Node build stage + guarded `COPY`; default OFF (`ENABLE_FRONTEND_BUILD=0`) |
| `docs/plans/ROB-6-trading-decision-react-scaffold-plan.md` | **this file** | — |

No changes to: any existing template, any existing static asset, any model/migration/schema/service, `Caddyfile`, CircleCI config, `pyproject.toml`, `uv.lock`.

---

## 13. Acceptance checklist (used at PR review time)

- [ ] `frontend/trading-decision/` exists with the §4.1 structure.
- [ ] `npm ci && npm run typecheck && npm run build` succeed locally on Node 20.
- [ ] `frontend/trading-decision/dist/index.html` references hashed assets at `/trading/decisions/assets/...` (verified by §8 CI step).
- [ ] `frontend/trading-decision/.gitignore` excludes `node_modules/` and `dist/`; root `.gitignore` and `.dockerignore` updated as in §12.
- [ ] `package-lock.json` is committed; `node_modules/` and `dist/` are NOT committed.
- [ ] `app/routers/trading_decisions_spa.py` exists and is registered in `app/main.py` after `trading_decisions.router`.
- [ ] `GET /trading/decisions/` returns 200 + HTML when `dist/index.html` exists.
- [ ] `GET /trading/decisions/inbox/anything` returns the same `index.html` (SPA fallback).
- [ ] `GET /trading/decisions/assets/<hash>.js` returns 200 + the asset bytes.
- [ ] `GET /trading/decisions/assets/missing.js` returns 404.
- [ ] `GET /trading/decisions/` returns **503** with the build-missing HTML when `dist/index.html` is absent.
- [ ] `GET /trading/api/decisions` (ROB-2) is unaffected — list endpoint still returns 401 unauthenticated, 200 authenticated.
- [ ] `app/routers/trading_decisions_spa.py` imports **none** of the §10 forbidden modules (test enforced).
- [ ] `make frontend-install`, `make frontend-dev`, `make frontend-build`, `make frontend-typecheck` all work from repo root.
- [ ] `.github/workflows/frontend-trading-decision.yml` runs on a PR that touches `frontend/trading-decision/**` and is green.
- [ ] `Dockerfile.api` builds cleanly with default args (`docker build -f Dockerfile.api .`); SPA stage is skipped because `ENABLE_FRONTEND_BUILD` defaults to `0`.
- [ ] `uv run ruff check app/ tests/` and `uv run ruff format --check app/ tests/` clean.
- [ ] `uv run ty check app/ --error-on-warning` clean.
- [ ] `uv run pytest tests/test_trading_decisions_spa_router.py tests/test_trading_decisions_spa_router_safety.py -v` green.
- [ ] No existing template, model, migration, schema, or service file modified.

---

## 14. Out-of-scope reminders (do not creep)

If during implementation any of these is tempting, **stop and split into a new PR**:

- Adding any UI beyond the hello page (proposal cards, accept/modify/defer flow) → ROB-4.
- Adding outcome / analytics views → ROB-5.
- Adding a router (`react-router`), state lib (Redux/Zustand), or styling system (Tailwind, Mantine, MUI) → defer until ROB-4 has a real design.
- Adding `vitest` / `@testing-library/react` → defer until there is non-trivial UI worth testing.
- Updating `scripts/deploy-native.sh` to install Node and run `npm run build` → follow-up issue.
- Flipping `ENABLE_FRONTEND_BUILD=1` in `Dockerfile.api` or in CI → follow-up issue.
- Rewriting any existing Jinja template (`portfolio_decision_desk.html`, etc.) as React → out of scope.
- Calling KIS / Upbit / brokers / Redis / watch / discord / taskiq from the SPA router → forbidden (§10).
- Adding `import.meta.env`-based runtime config → defer; the api client is hard-anchored at `/trading/api`.
- Adding ESLint / Prettier / Husky → follow-up; `tsc --noEmit` is the contract this PR ships.
- Adding a Storybook → no.

---

## 15. Implementer handoff prompt (for `codex --yolo`)

Paste the block below into a fresh **Codex `--yolo`** session in the same worktree (`feature-ROB-6-trading-decision-react-scaffold`). Codex is expected to write tests first where practical, run all verification commands, and commit in small focused chunks.

```text
You are the implementer for ROB-6 (Trading Decision Workspace · React/Vite/TS scaffold PR).
You are running as `codex --yolo` in this worktree:

  /Users/mgh3326/work/auto_trader-worktrees/feature-ROB-6-trading-decision-react-scaffold

Branch: feature/ROB-6-trading-decision-react-scaffold
Plan:   docs/plans/ROB-6-trading-decision-react-scaffold-plan.md   ← READ FIRST, FOLLOW EXACTLY

ROB-1 (DB schema, PR #595) and ROB-2 (API contract, PR #597) are already on main. DO NOT modify their files.

Hard constraints:
1. SCOPE: Scaffold + hello page + FastAPI seam + docs only. No decision UI, no analytics UI, no broker/watch/redis/discord side effects, no rewriting Jinja templates.
2. FastAPI module `app/routers/trading_decisions_spa.py` MUST NOT import any module in plan §10's forbidden list. The safety test in `tests/test_trading_decisions_spa_router_safety.py` enforces this; it must stay green.
3. Vite `base` is `/trading/decisions/`. Verify by running `npm run build` and checking that `dist/index.html` references `/trading/decisions/assets/...`.
4. The SPA router serves `dist/index.html` for `/trading/decisions/` and `/trading/decisions/{full_path:path}`, and serves hashed assets at `/trading/decisions/assets/...`. When `dist/index.html` is missing, return HTTP 503 with the build-missing HTML page from plan §6.2.
5. `Dockerfile.api`'s new Node stage is gated by `ARG ENABLE_FRONTEND_BUILD=0` and DEFAULTS OFF. Do not flip it on in this PR.
6. Existing routers (`trading_decisions.router`, `trading.router`, …) must remain registered in `app/main.py`. The new router is added AFTER `trading_decisions.router`.
7. Commit `package-lock.json`. Do NOT commit `node_modules/` or `dist/`.

Build order (TDD-friendly, frequent commits):

  STEP 1 — Workspace skeleton
    a. Initialise the workspace under `frontend/trading-decision/` with `npm init -y` then `npm install --save react@latest react-dom@latest` and `npm install --save-dev vite@latest @vitejs/plugin-react@latest typescript@latest @types/react@latest @types/react-dom@latest`.
    b. Replace generated package.json scripts to match plan §5.1.
    c. Add `.gitignore`, `.nvmrc`, `tsconfig.json`, `tsconfig.node.json`, `vite.config.ts`, `index.html`, `src/main.tsx`, `src/App.tsx`, `src/App.css`, `src/api/client.ts`, `src/components/HelloDecision.tsx`, `src/pages/HelloPage.tsx` exactly per plan §5.
    d. Run `npm run typecheck` and `npm run build`. Verify `dist/index.html` contains `/trading/decisions/assets/`.
    e. Update root `.gitignore` and `.dockerignore` per plan §12.
    f. Commit: "feat(rob-6): scaffold frontend/trading-decision Vite+React+TS workspace".

  STEP 2 — FastAPI SPA router (TDD)
    a. Write `tests/test_trading_decisions_spa_router.py` exactly per plan §9.1. Run it; it should fail because the module does not exist yet.
    b. Implement `app/routers/trading_decisions_spa.py` per plan §6.2.
    c. Re-run the tests; they should pass.
    d. Add `tests/test_trading_decisions_spa_router_safety.py` per plan §9.2; verify green.
    e. Commit: "feat(rob-6): add SPA router for /trading/decisions".

  STEP 3 — App wiring
    a. Modify `app/main.py` to import and register `trading_decisions_spa.router` after `trading_decisions.router`.
    b. Run the existing test suite for trading_decisions to ensure no regression: `uv run pytest tests/test_trading_decisions_router.py tests/test_trading_decisions_router_safety.py -q`.
    c. Smoke test: build the SPA, start `make dev`, curl `http://localhost:8000/trading/decisions/`. Expect 200 + HTML containing `<div id="root">`.
    d. Commit: "feat(rob-6): register trading_decisions_spa router".

  STEP 4 — Makefile, Docker, CI
    a. Append `frontend-install`, `frontend-dev`, `frontend-build`, `frontend-typecheck` targets to `Makefile` and to the `.PHONY` line.
    b. Add the `frontend-builder` stage to `Dockerfile.api` with default `ARG ENABLE_FRONTEND_BUILD=0` per plan §6.4. Verify `make docker-build` still succeeds with no extra args.
    c. Add `.github/workflows/frontend-trading-decision.yml` per plan §8.
    d. Commit: "build(rob-6): make + docker + CI integration for trading-decision workspace".

  STEP 5 — README + final verification
    a. Add `frontend/trading-decision/README.md` per plan §11.
    b. Run all the verification commands from plan §9.3. Everything must pass.
    c. Run `uv run ruff check app/ tests/`, `uv run ruff format --check app/ tests/`, `uv run ty check app/ --error-on-warning`. All clean.
    d. Commit: "docs(rob-6): trading-decision workspace README and dev workflow".

When the §13 acceptance checklist is fully green, open a PR against `main` titled
"feat(rob-6): trading decision workspace React/Vite/TS scaffold" with a body that:
- summarises the scaffold scope (plan §1–2),
- explicitly notes that §6.4 Docker stage is OFF by default and that production deploy will continue to return the 503 build-missing page until the follow-up,
- links the plan doc.
```

---

## 16. Open decisions (defaults chosen, easy to revisit in review)

1. **React 19 vs 18.** → React 19. Stable, current as of 2026-04. Hooks API is unchanged for our scaffold-level usage. Revisit if a peer dependency forces 18.
2. **Vite 7 vs 6 vs 5.** → Latest stable Vite at install time (the lockfile is the truth). The config in §5.2 is forward-compatible.
3. **`react-router` now or later?** → Later. The SPA fallback in §6.2 is sufficient for the hello page; ROB-4 will pick the router with the page taxonomy in mind.
4. **State management library?** → None. Plain React state until a real shared-state need exists.
5. **Component library / styling?** → Plain CSS in `App.css`. Decision deferred to ROB-4 design.
6. **Testing library?** → None in this PR. The hello page has no testable behaviour; pulling in vitest/RTL adds ~30 deps for zero return. Add when ROB-4 introduces actual UI logic.
7. **`StaticFiles` mount vs hand-rolled router.** → Hand-rolled (§6.2 rationale). Lets us return a build-missing 503 with instructions and supports SPA fallback cleanly.
8. **Pin Node at 20 vs 22.** → 20 LTS. Most boring choice; 22 LTS doesn't add anything we need.
9. **Should `Dockerfile.api` enable the Node stage by default?** → No (§6.4). Docker isn't on the production path today; flipping it on is a separate follow-up gated by deploy-script changes.
10. **Should we add the workspace to `.circleci/config.yml`?** → No. The dedicated GitHub workflow (§8) is the SPA's CI. Adding a Node executor to CircleCI is duplicative.
11. **Vite dev port (5173) collision risk?** → Acceptable. `strictPort: true` makes it fail loud if taken. The README documents the port.
12. **Source maps in production?** → On (§5.2). Bundle is tiny; reverse it later if it ever matters.
13. **`base: '/trading/decisions/'` includes the trailing slash?** → Yes. Vite requires the trailing slash to emit asset URLs correctly.

---
