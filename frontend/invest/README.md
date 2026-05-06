# @auto-trader/invest

`/invest/app` — 토스식 모바일 read-only 통합 홈 (ROB-123).

## Develop

```bash
cd frontend/invest
nvm use
npm ci
npm run dev   # http://localhost:5174 (Vite). 백엔드 :8000 필요.
```

## Build

```bash
npm run typecheck && npm test && npm run build
```

빌드 산출물은 `frontend/invest/dist/` → `app/routers/invest_app_spa.py` 가 서빙.
