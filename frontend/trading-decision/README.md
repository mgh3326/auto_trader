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

The production-baked path is currently off by default. The macOS native deploy path in `scripts/deploy-native.sh` does not yet build the SPA, so `/trading/decisions/` in production returns the HTTP 503 build-missing page until a follow-up enables that flow.

To preview the production-baked path on a deploy host, run:

```bash
make frontend-build
```

Then restart the API.

## Adding Components

Add shared UI under `src/components/` and new pages under `src/pages/`. The API client at `src/api/client.ts` targets `/trading/api`. ROB-4 will introduce real routing.

## Constraints

This scaffold does not read runtime environment variables. Do not add `import.meta.env` usage under `src/` in this PR.

Do not add third-party state libraries such as Redux or Zustand. Do not add React Router or another client router. Add those in ROB-4 with a design doc.

## Troubleshooting

Build failed with "Failed to resolve module": run `npm ci`.

HTTP 503 in production: `dist/` is missing. See the production deployment section.

CSP refuses scripts: verify the bundle uses same-origin `/trading/decisions/assets/...` paths. `vite.config.ts` sets `base` to `/trading/decisions/`.
