# AUTO_TRADER-3Z AnyIO Backend Crash Fix Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Prevent HTTP requests from crashing with `ModuleNotFoundError: No module named 'anyio._backends'` / `KeyError: 'asyncio'` when Sentry FastAPI integration is enabled.

**Architecture:** The highest-confidence first-party trigger is `app/middleware/auth.py`, which currently subclasses Starlette `BaseHTTPMiddleware`. Replace it with a pure ASGI middleware so auth gating no longer depends on `BaseHTTPMiddleware`'s AnyIO event/cancel-scope internals. Keep all existing auth semantics unchanged, then lock the fix with a Sentry-enabled regression test.

**Tech Stack:** Python 3.13, FastAPI, Starlette, AnyIO, sentry-sdk, pytest

---

## Investigation Summary

- Sentry issue: `AUTO_TRADER-3Z` (`https://mgh3326-daum.sentry.io/issues/AUTO_TRADER-3Z`)
- Error: `ModuleNotFoundError: No module named 'anyio._backends'` followed by `KeyError: 'asyncio'`
- Request surface: `GET /` in `development`, running via `uvicorn --reload`
- Local repo state:
  - `anyio 4.12.1`
  - `starlette 0.52.1`
  - `fastapi 0.135.1`
  - `sentry-sdk 2.54.0`
- `anyio._backends._asyncio` imports normally in the virtualenv, so the issue is not “package missing from lockfile”.
- Minimal FastAPI + Sentry works, but the app’s auth middleware path is the most suspicious first-party boundary.
- Starlette documents long-standing `BaseHTTPMiddleware` limitations and recommends pure ASGI middleware for safer behavior.

### Task 1: Add a Sentry-enabled regression test for the auth middleware path

**Files:**
- Modify: `tests/test_auth_middleware.py`
- Reference: `app/middleware/auth.py`
- Reference: `tests/test_sentry_init.py`

**Step 1: Write the failing test**

Add a regression test near the existing `AuthMiddleware` contract tests:

```python
def test_protected_route_redirects_cleanly_with_sentry_fastapi_enabled(monkeypatch):
    import sentry_sdk
    from sentry_sdk.integrations.fastapi import FastApiIntegration

    test_app = FastAPI()
    test_app.add_middleware(AuthMiddleware)

    @test_app.get("/test-protected", response_class=HTMLResponse)
    async def protected_route(request: Request):
        return "Protected Content"

    @test_app.get("/web-auth/login", response_class=HTMLResponse)
    async def login_page():
        return "Login Page"

    monkeypatch.setattr(
        AuthMiddleware,
        "_load_user",
        staticmethod(AsyncMock(return_value=None)),
    )

    sentry_sdk.init(dsn=None, integrations=[FastApiIntegration()])
    client = TestClient(test_app)

    response = client.get("/test-protected", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"].startswith("/web-auth/login")
```

Keep the test narrow:
- No DB calls
- No `create_app()` lifespan startup
- No real Sentry network dependency
- Only the Sentry FastAPI integration + auth middleware + one protected route

**Step 2: Run the single regression test and confirm current failure**

Run:

```bash
uv run pytest tests/test_auth_middleware.py::test_protected_route_redirects_cleanly_with_sentry_fastapi_enabled -v
```

Expected: FAIL on the current branch because the request does not complete cleanly under the `BaseHTTPMiddleware` implementation. If the failure shows up as a hang locally, capture the stack and keep the test in place before changing code.

**Step 3: Commit the red test**

```bash
git add tests/test_auth_middleware.py
git commit -m "test: reproduce sentry auth middleware anyio crash"
```

### Task 2: Rewrite `AuthMiddleware` as pure ASGI middleware

**Files:**
- Modify: `app/middleware/auth.py`
- Test: `tests/test_auth_middleware.py`

**Step 1: Replace the `BaseHTTPMiddleware` implementation**

Convert the middleware to a plain ASGI class:

```python
from starlette.types import ASGIApp, Receive, Scope, Send


class AuthMiddleware:
    def __init__(self, app: ASGIApp):
        self.app = app
        self.public_paths = self.BASE_PUBLIC_PATHS.copy()
        if settings.DOCS_ENABLED:
            self.public_paths.extend(self.DOCS_PATHS)
        self.public_api_paths = self.PUBLIC_API_PATHS.copy()
        if settings.PUBLIC_API_PATHS:
            self.public_api_paths.extend(settings.PUBLIC_API_PATHS)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request = Request(scope, receive=receive)
        path = request.url.path
        is_api_request = self._is_api_request_path(path)

        response = await self._maybe_authenticate(request, scope, is_api_request)
        if response is not None:
            await response(scope, receive, send)
            return

        await self.app(scope, receive, send)
```

Important implementation details:
- Remove the `BaseHTTPMiddleware` import entirely.
- Keep helper methods (`_is_public_path`, `_is_public_api_path`, `_is_legacy_deprecated_path`, `_is_api_request_path`) unchanged unless tests require small cleanup.
- Move the current `dispatch()` decision tree into a helper that returns either:
  - `None` to continue to the downstream app, or
  - a `JSONResponse` / `RedirectResponse` to short-circuit.
- Preserve `request.state.user` behavior by writing into `scope["state"]` before calling the downstream app:

```python
scope.setdefault("state", {})
scope["state"]["user"] = user
```

Do not change auth policy in this task. Preserve:
- `/api/n8n/*` API key behavior
- public path allowlist behavior
- legacy deprecated path passthrough
- API `401` JSON responses
- HTML `303` redirect behavior

**Step 2: Run the auth middleware test file**

Run:

```bash
uv run pytest tests/test_auth_middleware.py -v
```

Expected: PASS, including the new Sentry regression test.

**Step 3: Run the focused Sentry tests**

Run:

```bash
uv run pytest tests/test_main_sentry.py tests/test_sentry_init.py -v
```

Expected: PASS. The middleware change must not break Sentry initialization or exception capture behavior.

**Step 4: Commit the middleware rewrite**

```bash
git add app/middleware/auth.py tests/test_auth_middleware.py
git commit -m "fix: replace auth base middleware with pure asgi"
```

### Task 3: Verify the issue path and stop if the root cause survives

**Files:**
- No new files unless verification exposes a second bug

**Step 1: Re-run the exact regression command**

Run:

```bash
uv run pytest tests/test_auth_middleware.py::test_protected_route_redirects_cleanly_with_sentry_fastapi_enabled -v
```

Expected: PASS.

**Step 2: Run one app-factory smoke test**

Run:

```bash
uv run python - <<'PY'
from unittest.mock import AsyncMock, Mock
from fastapi.testclient import TestClient
import app.main as main_module
import app.middleware.auth as auth_module

main_module.init_sentry = Mock(return_value=True)
main_module.setup_monitoring = AsyncMock()
main_module.cleanup_monitoring = AsyncMock()
main_module.broker.startup = AsyncMock()
main_module.broker.shutdown = AsyncMock()
main_module.broker.is_worker_process = False
auth_module.AuthMiddleware._load_user = staticmethod(AsyncMock(return_value=None))

client = TestClient(main_module.create_app())
response = client.get("/", follow_redirects=False)
print(response.status_code, response.headers.get("location"))
PY
```

Expected: `303 /web-auth/login?...`

**Step 3: If the smoke test still reproduces the issue, stop and open a follow-up plan**

Do not immediately pin packages or disable Sentry integration in the same change. If the pure ASGI rewrite does not eliminate the issue:
- capture the new traceback
- compare with upstream `sentry-sdk` / `starlette` known issues
- create a separate follow-up plan for either:
  - a targeted dependency pin, or
  - a temporary config flag to disable `FastApiIntegration()` while keeping logging/error capture

**Step 4: Commit verification notes**

```bash
git add app/middleware/auth.py tests/test_auth_middleware.py
git commit -m "test: verify sentry auth middleware regression fix"
```

## Scope Guardrails

- Do not change public/private auth policy while rewriting the middleware.
- Do not modify router handlers to work around middleware behavior.
- Do not call `sentry_sdk.init(...)` outside `app/monitoring/sentry.py` in production code.
- Do not pin/downgrade `anyio`, `starlette`, `fastapi`, or `sentry-sdk` unless Task 3 proves the middleware rewrite is insufficient.

## Verification Checklist

- `uv run pytest tests/test_auth_middleware.py -v`
- `uv run pytest tests/test_main_sentry.py tests/test_sentry_init.py -v`
- Manual smoke script for `GET /` returns redirect instead of exception
- Sentry issue commit can include `Fixes AUTO_TRADER-3Z`
