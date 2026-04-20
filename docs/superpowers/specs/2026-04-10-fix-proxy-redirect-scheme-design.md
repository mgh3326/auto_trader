# Fix: Proxy-behind redirect generates wrong scheme in `next` URL

**Issue:** [#478](https://github.com/mgh3326/auto_trader/issues/478)
**Date:** 2026-04-10
**Status:** Approved

## Problem

`AuthMiddleware._maybe_authenticate()` builds the login redirect `next` parameter using `str(request.url)`, which produces an absolute URL. Behind the Caddy reverse proxy, the ASGI app sees the original (internal) scheme `http://` instead of the external `https://`, so the generated URL becomes:

```
/web-auth/login?next=http://mgh3326.duckdns.org/portfolio/
```

This causes two bugs:

1. **Scheme mismatch** — The `next` value contains `http://` even though the user accessed via HTTPS.
2. **Silent redirect to `/`** — `_sanitize_next()` in `web_router.py` rejects absolute URLs (any value with a scheme or netloc) and returns `None`, so the user is redirected to `/` instead of their original page after login.

## Root Cause

`app/middleware/auth.py` line 174:

```python
next_url = str(request.url)  # absolute URL with internal scheme
```

Meanwhile, `app/auth/web_router.py` line 235-239 (`require_login()`) already uses the correct pattern:

```python
next_url = (
    f"{request.url.path}?{request.url.query}"
    if request.url.query
    else request.url.path
)
```

The middleware and the dependency use different strategies for the same purpose.

## Solution

Replace the absolute URL with a relative path in `AuthMiddleware._maybe_authenticate()`, matching the existing `require_login()` pattern.

### Change

**File:** `app/middleware/auth.py`, inside `_maybe_authenticate()` (line ~174)

Before:
```python
next_url = str(request.url)
```

After:
```python
next_url = (
    f"{request.url.path}?{request.url.query}"
    if request.url.query
    else request.url.path
)
```

This is the only production code change required.

### Why this works

- **Eliminates scheme entirely** — Relative paths have no scheme, so proxy/internal scheme mismatch is irrelevant.
- **Passes `_sanitize_next()` validation** — Relative paths starting with `/` are accepted; the login post-redirect now lands on the original page.
- **Preserves open-redirect defense** — `_sanitize_next()` continues to reject absolute URLs, external hostnames, and non-`/`-prefixed paths.
- **Preserves query strings** — The `request.url.query` check ensures query parameters survive the round-trip.

### Why NOT other approaches

- **Caddy `X-Forwarded-Proto` + uvicorn `--proxy-headers`** — Useful infra hygiene but does not fix this bug alone, because `_sanitize_next()` still rejects absolute URLs. Could be a separate improvement.
- **Relax `_sanitize_next()` to allow same-origin absolute URLs** — Increases open-redirect attack surface unnecessarily. The relative-path approach is simpler and safer.

### Helper extraction

Both `require_login()` and `_maybe_authenticate()` will use the same 3-line inline pattern. A shared helper is not warranted for two call sites with a trivial expression. If a third site appears, extract then.

## Test Plan

### Unit tests for `AuthMiddleware` redirect

1. **Unauthenticated GET to protected path** — Verify `Location` header contains `next=/protected-path` (relative, no scheme/host).
2. **Unauthenticated GET with query string** — Verify `next=/path?key=value` preserves the query string.
3. **Unauthenticated GET to path without query** — Verify `next=/path` with no trailing `?`.

### Integration test for login round-trip

4. **Login → redirect back** — Simulate unauthenticated access to `/portfolio/`, follow redirect to login, submit credentials, verify final redirect lands on `/portfolio/` (not `/`).

### Negative tests (existing behavior preserved)

5. **Public paths bypass** — `/web-auth/login`, `/health`, `/auth/` do not trigger redirect.
6. **API paths return 401 JSON** — `/api/...` returns `{"detail": "Authentication required..."}`, not a redirect.

## Out of Scope

These are noted for future work but are NOT part of this change:

- **Caddy `X-Forwarded-Proto` + uvicorn `--proxy-headers`** — Infra improvement, separate issue.
- **localhost HTTP Secure cookie** — Production `secure=True` is correct behavior; local HTTPS testing guide or dev-mode cookie override is a separate concern.

## Files Changed

| File | Change |
|------|--------|
| `app/middleware/auth.py` | Replace `str(request.url)` with relative path pattern |
| `tests/test_auth_middleware_redirect.py` (new) | Unit + integration tests for redirect behavior |
