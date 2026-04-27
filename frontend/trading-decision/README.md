# Trading Decision Workspace

## Prerequisites

Use Node >= 20.19 and npm. If you use nvm, run:

```bash
nvm use
```

## Install

```bash
npm ci
```

From the repo root:

```bash
make frontend-install
```

## Local Dev

Run FastAPI in one terminal:

```bash
make dev
```

Run Vite in another terminal:

```bash
make frontend-dev
```

Open http://localhost:5173/trading/decisions/. Vite proxies `/trading/api`, `/auth`, and `/api` to `http://localhost:8000`.

## Local Prod-Mode Smoke

```bash
make frontend-build
```

Then visit http://localhost:8000/trading/decisions/. FastAPI serves the built `dist/` bundle.

## Assets

Built assets land in `frontend/trading-decision/dist/` and are served by FastAPI from `/trading/decisions/`. The `dist/` directory is gitignored.

## Production Deployment

The macOS native deploy path in `scripts/deploy-native.sh` builds this SPA on every release. The deploy script runs `npm ci && npm run build` inside `frontend/trading-decision/` of the new release checkout, asserts `dist/index.html` exists, and aborts before the `current` symlink switch if the build fails (see ROB-11). No manual `make frontend-build` step on the deploy host is required.

If you ever need to rebuild on a deploy host out-of-band (e.g. a hotfix between deploys), run from the active release directory:

```bash
cd "$AUTO_TRADER_BASE/current/frontend/trading-decision"
npm ci && npm run build
```

and then reload the API process. This is a fallback only — the next deploy will rebuild from scratch.

## Adding Components

Add shared UI under `src/components/` and new pages under `src/pages/`. The API client at `src/api/client.ts` targets `/trading/api`. ROB-4 will introduce real routing.

## Constraints

This scaffold does not read runtime environment variables. Do not add `import.meta.env` usage under `src/` in this PR.

Do not add third-party state libraries such as Redux or Zustand. Do not add React Router or another client router. Add those in ROB-4 with a design doc.

## Troubleshooting

Build failed with "Failed to resolve module": run `npm ci`.

HTTP 503 in production: `dist/` is missing. See the production deployment section.

CSP refuses scripts: verify the bundle uses same-origin `/trading/decisions/assets/...` paths. `vite.config.ts` sets `base` to `/trading/decisions/`.
