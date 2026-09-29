# /trader operator page (task 889, stage 1)

Read-only operator surface for trader.robinco.dev — broker open orders,
today's fills (KST), and active watches. Stage 2 action buttons are #890 and
do not exist yet.

## What shipped

- SPA entry `frontend/invest/trader.html` → `dist/trader.html`, served by
  `app/routers/trader_spa.py` at `GET /trader` and `GET /trader/{path}`.
  Asset/module URLs inside trader.html use the existing `/invest/app/` Vite
  base and are served by `invest_app_spa` from the same `dist/` directory.
- API `app/routers/trader_page.py` under `/trading/api/trader/`:
  - `GET /trading/api/trader/open-orders` (`?refresh=1` busts the snapshot)
  - `GET /trading/api/trader/fills/today`
  - `GET /trading/api/trader/watches`
- Auth: same operator session as `/invest` (`get_authenticated_user` +
  `AuthMiddleware`). Unauthenticated API calls get 401; the page GET gets a
  303 to `/web-auth/login`.
- Cache: `trader_open_orders_cache_ttl_seconds` (default 45, must stay inside
  30-60). A committed execution-ledger fill drops the snapshot through the
  `on_fill_committed` hook in `run_post_upsert_downstream` — in-process clear
  plus a Redis generation bump (`trader_page:open_orders:gen`) so the API
  notices monitor-side commits too. Redis down degrades to plain TTL.

## Desk: host routing for trader.robinco.dev

No routing change is required for the page to be reachable. Both documented
front paths already forward every path on the hostname to the app:

- cloudflared: `trader.robinco.dev` → `127.0.0.1:8000`
  (`~/.cloudflared/config.yml` ingress, per
  docs/superpowers/plans/2026-05-17-rob-259-haproxy-blue-green.md)
- HAProxy `ops/ncp/haproxy/haproxy.cfg.tmpl`: `ft_api_loopback` → `bk_api`
- Caddy (`Caddyfile`, dev/self-host path): catch-all `handle` →
  `host.docker.internal:8000`

After the deploy, verify:

```bash
curl -sI https://trader.robinco.dev/trader/ -o /dev/null -w '%{http_code}\n'
# expect 303 to /web-auth/login when logged out, 200 when logged in
curl -s https://trader.robinco.dev/trading/api/trader/open-orders | head -c 200
# expect {"detail":"Authentication required..."} (401) when logged out
```

Optional, only if the desk wants bare `https://trader.robinco.dev/` to land on
the page instead of its current target: add a redirect at the front layer.

- Caddy path: inside the `{$DOMAIN_NAME}` block, before the catch-all:

  ```
  handle / {
      redir / /trader/ 308
  }
  ```

  Caution: this changes what `/` serves for every existing link on the
  hostname. Prefer leaving `/` alone and giving operators the `/trader/` URL
  unless a root landing is explicitly wanted.

- cloudflared cannot path-rewrite; a hostname-level redirect needs a
  Cloudflare Redirect Rule (Rules → Redirect Rules): hostname
  `trader.robinco.dev`, path equals `/`, target
  `https://trader.robinco.dev/trader/`, status 308. Same caution applies.

Do not add DNS, do not add cloudflared ingress entries, and do not bind new
listeners — the page is served by the existing app on the existing chain.
