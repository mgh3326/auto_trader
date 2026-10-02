# /trader operator page (task 889 stage 1, task 890 stage 2 PR A)

Operator surface for trader.robinco.dev — broker open orders, today's fills
(KST), active watches (stage 1, read-only), plus the approval inbox and the
protected-quantity form (task 890 PR A). Order/watch creation buttons are
#890 PR B and do not exist yet.

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

## Stage 2 PR A (task 890): approval inbox + protected-quantity form

No new write route exists. The page's buttons call pre-existing endpoints:

| control | endpoint (existing) | shared core |
|---|---|---|
| 승인 | `POST /invest/api/approvals/{id}/approve` | `handle_web_approval` → `telegram_callback._handle_approve` |
| 기각 | `POST /invest/api/approvals/{id}/deny` | `handle_web_approval` → `telegram_callback._handle_deny` |
| 손절 승인 (1st click) | `POST /invest/api/approvals/{id}/approve` | `_handle_loss_cut_first_click` (issues a browser-bound token, submits nothing) |
| 손절 최종 확인 (2nd click) | `POST /invest/api/approvals/{id}/loss-cut-confirm` | `_handle_approve(loss_cut_confirmation=True)` |
| 보호 수량 미리보기/저장 | `PUT /invest/api/settings/protected-positions/{scope}/{market}/{symbol}` | `ProtectedQuantityService.save` |

These are the same handlers the Telegram callback runs
(`tests/services/order_proposals/test_trader_page_same_approval_path.py`).
All of them live under `/invest/api/`, which the CSRF middleware protects;
`/trading/` is CSRF-exempt, so no state-changing route may be added there.

New reads (trader role, same 401/403 as the /invest approval hub):

- `GET /trading/api/trader/approvals` — actionable proposals only
  (`app/services/trader_page/approval_inbox.py::inbox_block_reason`): a
  published human card (manual/reconfirm), not auto-approved, unused nonce,
  `valid_until` in the future, a rung still `pending_approval`/`needs_reconfirm`.
- `GET /trading/api/trader/approvals/{proposal_id}` — any proposal, with
  `actionable`/`block_reason` and per-rung broker acceptance state; the row
  re-reads it after an action.

Gates are unchanged. The buttons only work while `INVEST_APPROVALS_ENABLED`
is true, and the loss-cut confirmation also needs
`INVEST_LOSS_CUT_APPROVAL_ENABLED` (both default false). The inbox reports them
as `actions_enabled` / `loss_cut_actions_enabled` and hides the buttons when
off. The protected-quantity save still requires the admin role.
