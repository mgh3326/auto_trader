# dots → auto_trader MCP through Cloudflare Access (hk 3592)

Operator decision: `hk:doc decision/2026-11-08/at-mcp-dots-all-tools` — dots (a ChatGPT
session) uses **every DEFAULT tool, live orders included**, at
`https://at-mcp.robinco.dev/mcp`, behind Cloudflare Access OAuth with the operator's email as
the only allowed identity. Purpose: design new buy and sell strategies with dots.

**State after the PR that adds this runbook: nothing is exposed.** The code accepts an Access
assertion only when `MCP_CF_ACCESS_*` is set, and no tunnel route or Access application
exists. Everything below §2 is the deploy, which needs a separate operator approval.

## 1. How the pieces fit

```
ChatGPT connector ──OAuth (DCR + PKCE)──▶ Cloudflare Access (mgh3326.cloudflareaccess.com)
        │                                         │ serves discovery + the 401 challenge
        └── Authorization: Bearer <access token> ─▶ edge validates the token, then
                                                    forwards with Cf-Access-Jwt-Assertion
                                                    ▼
                         cloudflared tunnel ──▶ HAProxy front (loopback 8765) ──▶ DEFAULT MCP
                                                    origin verifies the assertion itself
```

The edge, not the origin, serves OAuth discovery. Evidence (`curl`, 2026-11-08, hk-mcp which
uses the same setup, handoffkeep has no `.well-known` route in its Go source):

- `GET /.well-known/oauth-protected-resource` → `{"resource":"https://hk-mcp.robinco.dev","authorization_servers":["https://mgh3326.cloudflareaccess.com"]}`
- `GET /.well-known/oauth-authorization-server` → Access issuer, `registration_endpoint`, `S256` PKCE
- `POST /mcp` with no credentials → `401` with `www-authenticate: Bearer realm="OAuth", error="invalid_token", ... resource_metadata="https://hk-mcp.robinco.dev/.well-known/cloudflare-access-protected-resource/mcp"`

So the origin adds **no** discovery route and no `WWW-Authenticate`. It only verifies the
signed `Cf-Access-Jwt-Assertion` header, so it stays safe if the edge is misconfigured or the
origin is reached another way (for example over the tailnet): an unsigned or forged header is
refused.

What the origin accepts on the DEFAULT route (`app/mcp_server/cf_access.py`):

| Credential | Accepted when |
|---|---|
| `Authorization: Bearer <MCP_AUTH_TOKEN>` | unchanged: constant-time equality with the configured token |
| `Cf-Access-Jwt-Assertion` | RS256 signature by a key from `https://<team>/cdn-cgi/access/certs`, `iss == https://<team>`, `aud` contains `MCP_CF_ACCESS_AUD`, `exp` present and valid, `nbf` valid (30 s skew), `email` exactly in `MCP_CF_ACCESS_ALLOWED_EMAILS` (ASCII case-folded, no wildcard) |

A request that carries the assertion header must pass that check even if it also carries a
valid bearer. `Cf-Access-Authenticated-User-Email` and every other header are never read.
Both transports (`streamable-http`, `sse`) use the same provider.

Attribution: an Access call is `caller_agent_id="dots"`, `caller_source="cf_access"` (Sentry
`consumer`, proposals' `creator_agent_id`); the client's `x-paperclip-agent-id` is ignored. The
log has one line per call: `mcp.tool.called tool=… profile=default consumer=dots operator=operator:<email>`.

**Risk to accept knowingly:** the Access identity reaches the live order tools (KIS, Toss,
Upbit place/modify/cancel). Every existing gate still applies and none is changed: env gates,
per-call `dry_run=False` + `confirm=True`, `approval_hash`, loss-sell guard, exact-account
checks, kis_mock attribution, W5 inbox, ROB-1340, NHPLUG, execution-ledger ingest token.

## 2. Deploy (operator approval required — none of this is done by the PR)

Names only below. Never write values into the repo, a ticket or a chat.

Create the steps in this order. Step 2 must not exist before step 1 and its policy do: a
tunnel route without the Access application forwards to the origin with no edge check.

1. **Access application** (Cloudflare Zero Trust): self-hosted, domain `at-mcp.robinco.dev`,
   path `/` (covers `/mcp`, `/health` and `.well-known`), policy **Allow, Emails = the operator
   email, nothing else**. Turn on the MCP / OAuth ("managed OAuth") option the way the
   `hk-mcp.robinco.dev` application has it. Copy the application **AUD tag** (64 hex). Confirm
   the saved policy lists only the operator email before going on.
2. **Tunnel route** (only after step 1 is saved): public hostname `at-mcp.robinco.dev` → service `http://127.0.0.1:8765`
   (the DEFAULT HAProxy front, `ops/ncp/haproxy/haproxy.cfg.tmpl` `ft_mcp_loopback`), on a
   `cloudflared` that runs **on the NCP host** (loopback), or point it at the tailnet front
   `100.122.100.56:8765` from a host on the tailnet.
   > **Open item:** which host and tunnel run `cloudflared` for `hk-mcp.robinco.dev`, and how
   > it is managed, is not recorded in this repo or in the handoffkeep repo/docs. The operator
   > states it before this step; do not guess.
3. **Production env** of the DEFAULT MCP unit (secret-manager / the prod env file — the same
   place `MCP_AUTH_TOKEN` already lives; its value stays as it is):
   - `MCP_CF_ACCESS_TEAM_DOMAIN` — `<team>.cloudflareaccess.com`, bare host name
   - `MCP_CF_ACCESS_AUD` — the AUD tag from step 1
   - `MCP_CF_ACCESS_ALLOWED_EMAILS` — the operator email (comma separated for more; no wildcard)

   `MCP_AUTH_TOKEN` must be non-empty; otherwise, or if only some of the three are set or a
   value is malformed, the DEFAULT unit **refuses to start**. The fixed-profile units (kiwoom,
   h3-*) read the same env file but ignore these variables and stay bearer-only. Then
   forward-deploy as usual (`scripts/deploy-ncp-pull.sh`, exact-SHA CI gate): the script passes
   the env file to every MCP container, starts the new DEFAULT color first and waits for its
   `/health` before switching HAProxy, so a refused boot fails the deploy with the old color
   still serving.
4. Reload the dots connector with URL `https://at-mcp.robinco.dev/mcp` (OAuth).

## 3. Verify (after step 3 or 4)

| Check | Expect |
|---|---|
| `curl -si -X POST https://at-mcp.robinco.dev/mcp` (no credentials) | `401` from the edge, `www-authenticate … resource_metadata=…` |
| `curl -s https://at-mcp.robinco.dev/.well-known/oauth-protected-resource` | `authorization_servers` = the Access issuer |
| dots connector OAuth, then `tools/list` | `200`, the DEFAULT tool list |
| one read tool from dots (for example `get_quote` on a KR symbol) | result returned; log line has `consumer=dots operator=operator:<email>`; Sentry event/span tag `consumer=dots` |
| tailnet client with `Authorization: Bearer <MCP_AUTH_TOKEN>` to `100.122.100.56:8765` | still `200` |
| tailnet request with a made-up `Cf-Access-Jwt-Assertion` and no token | `401` |
| startup log of the new instance | `cf_access_enabled=True` |

Do not place an order to verify. Use read tools only.

## 4. Rollback

Reversible in minutes, no code change and no data change. The order matters. Each step
must finish before the next one starts.

1. **Remove or disable the tunnel route first**: delete the public hostname
   `at-mcp.robinco.dev` (or its ingress rule to `http://127.0.0.1:8765`) from the `cloudflared`
   configuration that serves it, then reload that `cloudflared` (how it is managed is the open
   item in §2). A route that still exists keeps forwarding every request to the origin, with
   or without the edge policy.
   Verify: `curl -si https://at-mcp.robinco.dev/mcp` gets **no answer from the origin**: no
   `200`, no MCP JSON body, no `WWW-Authenticate` from the origin. A Cloudflare error page or a
   connection failure is the expected result. The DEFAULT unit's access log must show no new
   request for that host after the check.
2. **Only then remove the Access application** (or its `at-mcp.robinco.dev` policy). Removing
   the application while the route still exists leaves the origin reachable without an edge
   check. That is the window step 1 closes.
3. **Unset the three `MCP_CF_ACCESS_*` variables** in the DEFAULT unit's env and redeploy. The
   unit then decides every request exactly as before this change (bearer only). The assertion
   path is inactive from this point.

The bearer path (`MCP_AUTH_TOKEN`, tailnet clients) is unaffected by all three steps. The code can stay.

## 5. Failure modes

- **DEFAULT unit refuses to start** after step 3: the log names the problem — partial
  variables, a malformed value (team domain must be `<team>.cloudflareaccess.com`, emails exact
  addresses) or an empty `MCP_AUTH_TOKEN`. Names only, never values.
- **dots gets 401 after OAuth succeeds**: the assertion did not verify at the origin. Look for
  `mcp.cf_access.refused reason=<ExceptionClass>` in the log (`InvalidAudienceError` → wrong
  `MCP_CF_ACCESS_AUD`; `InvalidIssuerError` → wrong team domain; `ExpiredSignatureError`/
  `ImmatureSignatureError` → clock skew over 30 s; no such line and `mcp.cf_access.jwks_failed` →
  the origin cannot reach `https://<team>/cdn-cgi/access/certs`). A failed key fetch is retried at most once
  every 10 s, and an unknown `kid` refreshes the key set at most once a minute.
- **Allowed email refused**: the token's `email` claim is compared exactly (ASCII case-folded)
  to the list; a different address than the one Access shows is a different identity.
