# Fix Proxy Redirect Scheme (#478) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix `AuthMiddleware` to generate relative-path `next` URLs instead of absolute URLs, eliminating scheme mismatch behind reverse proxy and the silent redirect-to-`/` bug.

**Architecture:** Single change in `app/middleware/auth.py:174` — replace `str(request.url)` with relative path pattern matching `require_login()`. Add tests to prevent regression.

**Tech Stack:** FastAPI, Starlette, pytest, TestClient

**Spec:** `docs/superpowers/specs/2026-04-10-fix-proxy-redirect-scheme-design.md`

---

## File Structure

| File | Action | Responsibility |
|------|--------|---------------|
| `app/middleware/auth.py` | Modify line 174 | Replace absolute URL with relative path |
| `tests/test_auth_middleware.py` | Modify | Add redirect `next` format tests |

---

### Task 1: Add failing tests for relative-path `next` in redirect

**Files:**
- Modify: `tests/test_auth_middleware.py`

The existing test app already has `/test-protected` and the middleware wired up. We need a new route with query string support, and tests that assert the exact `next` value format.

- [ ] **Step 1: Add a route with query string support to the test app**

In `tests/test_auth_middleware.py`, after the existing route definitions (after line 48, the `nested_api_data` route), add:

```python
@app.get("/portfolio/", response_class=HTMLResponse)
async def portfolio_page(request: Request):
    return "Portfolio Page"
```

- [ ] **Step 2: Write test — redirect uses relative path (no scheme/host)**

Add at the end of `tests/test_auth_middleware.py`:

```python
def test_redirect_next_uses_relative_path(client, mock_session_local):
    """AuthMiddleware must generate relative-path next, not absolute URL."""
    response = client.get("/portfolio/", follow_redirects=False)
    assert response.status_code == 303
    location = response.headers["location"]
    # next must be a relative path, not http://testserver/portfolio/
    assert location == "/web-auth/login?next=/portfolio/"
```

- [ ] **Step 3: Write test — redirect preserves query string**

Add at the end of `tests/test_auth_middleware.py`:

```python
def test_redirect_next_preserves_query_string(client, mock_session_local):
    """Query string in original URL must survive the redirect."""
    response = client.get("/portfolio/?tab=crypto&sort=asc", follow_redirects=False)
    assert response.status_code == 303
    location = response.headers["location"]
    assert location == "/web-auth/login?next=/portfolio/?tab=crypto&sort=asc"
```

- [ ] **Step 4: Write test — redirect without query has no trailing `?`**

Add at the end of `tests/test_auth_middleware.py`:

```python
def test_redirect_next_no_trailing_question_mark(client, mock_session_local):
    """Path without query string must not have trailing '?'."""
    response = client.get("/test-protected", follow_redirects=False)
    assert response.status_code == 303
    location = response.headers["location"]
    assert location == "/web-auth/login?next=/test-protected"
    assert "next=/test-protected?" not in location
```

- [ ] **Step 5: Run tests to verify they fail**

Run:
```bash
uv run pytest tests/test_auth_middleware.py -v -k "test_redirect_next"
```

Expected: All 3 new tests **FAIL** because current code generates `next=http://testserver/...` (absolute URL).

- [ ] **Step 6: Commit failing tests**

```bash
git add tests/test_auth_middleware.py
git commit -m "test: add failing tests for relative-path next in auth redirect (#478)"
```

---

### Task 2: Fix `AuthMiddleware` to use relative path for `next`

**Files:**
- Modify: `app/middleware/auth.py:172-176`

- [ ] **Step 1: Replace absolute URL with relative path**

In `app/middleware/auth.py`, change lines 172-176 from:

```python
            if not user:
                # Save the original URL to redirect back after login
                next_url = str(request.url)
                return RedirectResponse(
                    url=f"/web-auth/login?next={next_url}",
```

to:

```python
            if not user:
                # Use relative path to avoid scheme mismatch behind reverse proxy
                next_url = (
                    f"{request.url.path}?{request.url.query}"
                    if request.url.query
                    else request.url.path
                )
                return RedirectResponse(
                    url=f"/web-auth/login?next={next_url}",
```

- [ ] **Step 2: Run the new tests to verify they pass**

Run:
```bash
uv run pytest tests/test_auth_middleware.py -v -k "test_redirect_next"
```

Expected: All 3 tests **PASS**.

- [ ] **Step 3: Run the full auth middleware test suite**

Run:
```bash
uv run pytest tests/test_auth_middleware.py -v
```

Expected: All tests pass, including the existing `test_protected_route_no_auth` (it asserts `"/web-auth/login" in location` which still holds).

- [ ] **Step 4: Run linter**

Run:
```bash
make lint
```

Expected: No new warnings or errors.

- [ ] **Step 5: Commit the fix**

```bash
git add app/middleware/auth.py
git commit -m "fix: use relative path for login redirect next param (#478)

AuthMiddleware was using str(request.url) which produces absolute URLs.
Behind a reverse proxy this generates http:// scheme even for HTTPS
requests, and _sanitize_next() rejects absolute URLs causing silent
redirect to /. Now uses request.url.path + query string, matching
the existing require_login() pattern."
```

---

### Task 3: Verify existing tests still pass

**Files:** None (verification only)

- [ ] **Step 1: Run full test suite**

Run:
```bash
uv run pytest tests/test_auth_middleware.py tests/test_auth_web_router.py tests/test_auth_router.py -v
```

Expected: All tests pass. No regressions in auth-related tests.

- [ ] **Step 2: Run broader test suite to check for side effects**

Run:
```bash
make test-unit
```

Expected: All unit tests pass.
