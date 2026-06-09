# ROB-469 PR1 — MCP Observe + Detect Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the auto_trader MCP server detectable and the next disconnect diagnosable — add an unauthenticated, dependency-free `/health` route, startup/shutdown lifecycle logging, switch the native/HAProxy/docker health probes to `/health`→200, and add a supervision runbook.

**Architecture:** Factor the health route + lifespan logging into a small, unit-testable `app/mcp_server/lifecycle.py` (so it can be tested against a minimal `FastMCP` without importing the 128-tool production server), wire it into `app/mcp_server/main.py`, and repoint the ops health probes from the auth-gated `/mcp` (401/400) to the new dependency-free `/health` (200). No application logic, no DB migration, no broker/order mutation.

**Tech Stack:** Python 3.13, FastMCP 3.2.0 (`streamable-http` over uvicorn), Starlette (`custom_route`, `TestClient`), pytest, bash ops scripts, HAProxy, docker-compose.

**Spec:** `docs/superpowers/specs/2026-06-09-rob-469-mcp-server-resilience-design.md` (§4 PR1).

**Branch / worktree:** work happens in the existing worktree `/Users/mgh3326/work/auto_trader.rob-469` on branch `rob-469`.

---

## Background facts (verified against installed source — do not re-derive)

- `app/mcp_server/main.py` builds a module-level `mcp = FastMCP(name="auto_trader-mcp", ..., on_duplicate="error")`, adds two middlewares, calls `register_all_tools(mcp, profile=_mcp_profile)`, then `main()` selects transport and calls `mcp.run(...)`. The module-level `_auth_token = _env("MCP_AUTH_TOKEN", "")` (line 38) controls auth.
- **`@mcp.custom_route("/health", methods=["GET"], include_in_schema=False)` is UNauthenticated.** In fastmcp 3.2.0, only the `/mcp` route is wrapped in `RequireAuthMiddleware` (`fastmcp/server/http.py:336`); custom routes are appended via `_additional_http_routes` (`transport.py:137`, mounted at `http.py:357-358`). App-level `AuthenticationMiddleware` only *populates* auth context, it does not reject. Verified: `GET /health` → 200 even with `MCP_AUTH_TOKEN` set.
- **Lifespan:** `from fastmcp.server.lifespan import lifespan` is a decorator that turns an async-generator `(server) -> AsyncIterator[dict]` into a composable `Lifespan`; pass it to `FastMCP(..., lifespan=...)` (`server.py:273,337`). Teardown (the `finally` after `yield`) is the safe shutdown hook. **Do NOT call `signal.signal()`** — uvicorn's `capture_signals()` overrides custom handlers (`uvicorn/server.py:322-340`).
- **Tool count:** `len(await server.list_tools())` works standalone (no request context needed). `get_tools()` does NOT exist; use `list_tools()`.
- **Testing the route:** `app = mcp.http_app()` returns a `StarletteWithLifespan`; `from starlette.testclient import TestClient; with TestClient(app) as c: c.get("/health")` runs the lifespan and serves the route. Verified round-trip: `200 {'status':'ok'}`.
- **Diagnosis property:** a *startup* log with no matching *shutdown* log before the next *startup* ⇒ hard-kill/OOM/SIGKILL (teardown never ran). A *shutdown* log ⇒ graceful. Signal-type cannot be distinguished (uvicorn owns it).

---

## File structure

| File | Responsibility | Action |
|---|---|---|
| `app/mcp_server/lifecycle.py` | `/health` route registration + startup/shutdown lifespan logging (testable unit) | **Create** |
| `tests/test_mcp_server_lifecycle.py` | Unit tests for `/health` (unauth, bypasses auth, dependency-free) + lifespan logs | **Create** |
| `app/mcp_server/main.py` | Wire lifespan into `FastMCP(...)`, register `/health`, enrich startup log + crash log | **Modify** |
| `ops/native/scripts/healthcheck-native.sh` | Probe `/health`→200 instead of `/mcp`→401/400 | **Modify** |
| `ops/native/haproxy/haproxy.cfg.tmpl` | `bk_mcp` health-check → `GET /health` expect 200 | **Modify** |
| `scripts/deploy-native.sh` | Built-in fallback probe → `/health`→200 | **Modify** |
| `docker-compose.prod.yml` | Add the missing `mcp` service healthcheck (L7 `/health`) | **Modify** |
| `docs/runbooks/mcp-health-supervision.md` | Operator runbook: health route, probe interpretation, restart per path | **Create** |

---

## Task 1: `/health` route in `app/mcp_server/lifecycle.py`

**Files:**
- Create: `app/mcp_server/lifecycle.py`
- Test: `tests/test_mcp_server_lifecycle.py`

- [ ] **Step 1: Write the failing tests for the health route**

Create `tests/test_mcp_server_lifecycle.py`:

```python
"""ROB-469 PR1: MCP server lifecycle observability — /health route + lifespan logging.

These tests run with NO database/redis available, which is itself the proof that
/health is dependency-free (a true event-loop liveness probe).
"""

from __future__ import annotations

import logging

import pytest
from fastmcp import FastMCP
from starlette.testclient import TestClient

from app.mcp_server.auth import build_auth_provider
from app.mcp_server.lifecycle import build_server_lifespan, register_health_route


@pytest.mark.unit
def test_health_returns_ok_payload() -> None:
    mcp = FastMCP(name="lifecycle-test")
    register_health_route(mcp, service="test-mcp", version="9.9.9")
    app = mcp.http_app()
    with TestClient(app) as client:
        resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["service"] == "test-mcp"
    assert body["version"] == "9.9.9"
    assert isinstance(body["uptime_s"], (int, float))


@pytest.mark.unit
def test_health_bypasses_auth_while_mcp_is_gated() -> None:
    # Auth IS enabled, yet /health must still return 200 unauthenticated,
    # while the /mcp protocol route stays gated.
    mcp = FastMCP(name="lifecycle-test", auth=build_auth_provider("super-secret-token"))
    register_health_route(mcp)
    app = mcp.http_app()
    with TestClient(app) as client:
        health = client.get("/health")  # no Authorization header
        mcp_route = client.get("/mcp")  # no Authorization header
    assert health.status_code == 200
    assert health.json()["status"] == "ok"
    assert mcp_route.status_code in (400, 401, 406)  # NOT 200 — auth/headers reject
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_mcp_server_lifecycle.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.mcp_server.lifecycle'` (or ImportError on `register_health_route`).

- [ ] **Step 3: Create `app/mcp_server/lifecycle.py` with the health route**

```python
"""ROB-469: MCP server lifecycle observability.

Unauthenticated, dependency-free /health route and startup/shutdown logging,
factored out of app/mcp_server/main.py so they can be unit-tested against a
minimal FastMCP instance without importing the full 128-tool production server.
"""

from __future__ import annotations

import logging
import time
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING

from fastmcp.server.lifespan import lifespan as fastmcp_lifespan
from starlette.requests import Request
from starlette.responses import JSONResponse

if TYPE_CHECKING:
    from fastmcp import FastMCP

logger = logging.getLogger(__name__)

# Module import is the closest in-process proxy to process start time.
STARTED_MONOTONIC = time.monotonic()


def register_health_route(
    mcp: "FastMCP",
    *,
    service: str = "auto-trader-mcp",
    version: str = "0.1.0",
) -> None:
    """Register an UNAUTHENTICATED, dependency-free GET /health route.

    fastmcp 3.2.0 mounts custom routes outside RequireAuthMiddleware (which wraps
    only the /mcp route), so /health returns 200 even when MCP_AUTH_TOKEN gates
    /mcp. The handler touches NO DB/Redis/broker, so it is a true event-loop
    liveness probe: a wedged loop stops answering it and supervision detects that.
    """

    @mcp.custom_route("/health", methods=["GET"], include_in_schema=False)
    async def health(request: Request) -> JSONResponse:  # noqa: ARG001
        return JSONResponse(
            {
                "status": "ok",
                "service": service,
                "version": version,
                "uptime_s": round(time.monotonic() - STARTED_MONOTONIC, 1),
            }
        )
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_mcp_server_lifecycle.py -v`
Expected: PASS for `test_health_returns_ok_payload` and `test_health_bypasses_auth_while_mcp_is_gated`.

- [ ] **Step 5: Commit**

```bash
git add app/mcp_server/lifecycle.py tests/test_mcp_server_lifecycle.py
git commit -m "$(cat <<'EOF'
feat(ROB-469 PR1): unauthenticated /health route for MCP server

Dependency-free GET /health (status/service/version/uptime_s) registered via
@mcp.custom_route, which fastmcp 3.2.0 mounts outside RequireAuthMiddleware → 200
even when MCP_AUTH_TOKEN gates /mcp. A wedged event loop stops answering it.

Co-authored-by: Hermes <hermes@example.invalid>
Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: Startup/shutdown lifespan logging in `lifecycle.py`

**Files:**
- Modify: `app/mcp_server/lifecycle.py`
- Test: `tests/test_mcp_server_lifecycle.py:append`

- [ ] **Step 1: Write the failing test for lifespan logging**

Append to `tests/test_mcp_server_lifecycle.py`:

```python
@pytest.mark.unit
def test_lifespan_logs_startup_and_shutdown(caplog: pytest.LogCaptureFixture) -> None:
    mcp = FastMCP(name="lifecycle-test", lifespan=build_server_lifespan(service="test-mcp"))

    @mcp.tool
    def echo(x: int) -> int:
        return x

    app = mcp.http_app()
    with caplog.at_level(logging.INFO, logger="app.mcp_server.lifecycle"):
        with TestClient(app):
            pass  # entering runs startup, exiting runs shutdown teardown
    assert "mcp.lifecycle.startup_complete" in caplog.text
    assert "tools=1" in caplog.text  # the single registered tool was counted
    assert "mcp.lifecycle.shutdown" in caplog.text
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_mcp_server_lifecycle.py::test_lifespan_logs_startup_and_shutdown -v`
Expected: FAIL with `ImportError: cannot import name 'build_server_lifespan'`.

- [ ] **Step 3: Add `build_server_lifespan` to `app/mcp_server/lifecycle.py`**

Append to `app/mcp_server/lifecycle.py`:

```python
def build_server_lifespan(*, service: str = "auto-trader-mcp"):
    """Build a FastMCP lifespan that logs startup-complete and shutdown.

    Diagnosis property (ROB-469): a startup log with NO matching shutdown log
    before the next startup ⇒ hard-kill/OOM/SIGKILL (teardown never ran); a
    shutdown log ⇒ graceful stop. Signal type cannot be distinguished — uvicorn
    owns signal handling, so we do NOT install signal handlers here.
    """

    @fastmcp_lifespan
    async def _server_lifespan(server: "FastMCP") -> AsyncIterator[dict]:
        try:
            tool_count = len(await server.list_tools())
        except Exception:  # never block startup on a best-effort count
            tool_count = -1
        logger.info(
            "mcp.lifecycle.startup_complete service=%s tools=%d uptime_s=%.1f",
            service,
            tool_count,
            time.monotonic() - STARTED_MONOTONIC,
        )
        try:
            yield {}
        finally:
            logger.info(
                "mcp.lifecycle.shutdown service=%s uptime_s=%.1f",
                service,
                time.monotonic() - STARTED_MONOTONIC,
            )

    return _server_lifespan
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run pytest tests/test_mcp_server_lifecycle.py -v`
Expected: PASS for all three tests.

- [ ] **Step 5: Commit**

```bash
git add app/mcp_server/lifecycle.py tests/test_mcp_server_lifecycle.py
git commit -m "$(cat <<'EOF'
feat(ROB-469 PR1): MCP startup/shutdown lifespan logging

build_server_lifespan logs mcp.lifecycle.startup_complete (with tool count) and
mcp.lifecycle.shutdown (graceful teardown). Presence/absence of the shutdown log
distinguishes graceful stop from hard-kill/OOM. No signal handlers (uvicorn owns them).

Co-authored-by: Hermes <hermes@example.invalid>
Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 3: Wire lifecycle into `app/mcp_server/main.py`

**Files:**
- Modify: `app/mcp_server/main.py` (imports near top; `FastMCP(...)` ctor ~line 40-56; `register_all_tools` call ~line 56; startup log ~line 82-89; transport branches ~line 95-113; `except` ~line 116-124)
- Test: `tests/test_mcp_server_lifecycle.py:append`

- [ ] **Step 1: Write the failing wiring test**

Append to `tests/test_mcp_server_lifecycle.py`:

```python
@pytest.mark.unit
def test_main_module_wires_health_route() -> None:
    # Importing the production module must register /health on the real server
    # instance. _additional_http_routes is where @custom_route appends (fastmcp 3.2.0).
    # (Lifespan wiring is covered by test_lifespan_logs_startup_and_shutdown + the
    # visible FastMCP(lifespan=...) ctor change; FastMCP's default _lifespan is a
    # non-None default_lifespan, so an "is not None" check here would false-pass.)
    import app.mcp_server.main as main_mod

    paths = {getattr(r, "path", None) for r in main_mod.mcp._additional_http_routes}
    assert "/health" in paths
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_mcp_server_lifecycle.py::test_main_module_wires_health_route -v`
Expected: FAIL — `/health` not in `_additional_http_routes` (route not yet wired).

- [ ] **Step 3: Add the lifecycle import to `app/mcp_server/main.py`**

Add after the existing `from app.mcp_server.tooling import register_all_tools  # noqa: E402` line (~line 36):

```python
from app.mcp_server.lifecycle import (  # noqa: E402
    build_server_lifespan,
    register_health_route,
)
```

- [ ] **Step 4: Wire the lifespan into the `FastMCP(...)` constructor**

Find the constructor (the `mcp = FastMCP(` block, ~line 40-56) and add a `lifespan=` kwarg. Change:

```python
mcp = FastMCP(
    name="auto_trader-mcp",
    instructions=(
        "Market data, holdings lookup, and order execution tools for auto_trader "
        "(symbol search, quote, holdings, OHLCV, indicators, trade, order management)."
    ),
    version="0.1.0",
    auth=auth_provider,
    # ROB-447: fail-fast on duplicate tool names at boot instead of the default "warn"
    # (last-registration-silently-wins), which had let the brief판 shadow the report판's
    # get_market_reports / get_latest_market_brief. Any future collision now raises.
    on_duplicate="error",
)
```

to (add the `lifespan=` line):

```python
mcp = FastMCP(
    name="auto_trader-mcp",
    instructions=(
        "Market data, holdings lookup, and order execution tools for auto_trader "
        "(symbol search, quote, holdings, OHLCV, indicators, trade, order management)."
    ),
    version="0.1.0",
    auth=auth_provider,
    # ROB-447: fail-fast on duplicate tool names at boot instead of the default "warn"
    # (last-registration-silently-wins), which had let the brief판 shadow the report판's
    # get_market_reports / get_latest_market_brief. Any future collision now raises.
    on_duplicate="error",
    # ROB-469: startup/shutdown lifecycle logging (diagnose disconnect root cause).
    lifespan=build_server_lifespan(),
)
```

- [ ] **Step 5: Register the `/health` route after tool registration**

Find `register_all_tools(mcp, profile=_mcp_profile)` (~line 56) and add the health route registration immediately after it:

```python
register_all_tools(mcp, profile=_mcp_profile)
# ROB-469: unauthenticated, dependency-free liveness probe for HAProxy / native
# healthcheck / docker healthcheck. Registered after tools so the count is final.
register_health_route(mcp, version="0.1.0")
```

- [ ] **Step 6: Enrich the startup log and crash log in `main()`**

In `main()`, hoist the graceful-shutdown timeout and enrich the startup log. Change the existing log block (~line 82-89):

```python
    mcp_type = _env("MCP_TYPE", "streamable-http")
    mcp_host = _env("MCP_HOST", "0.0.0.0")
    mcp_port = _env_int("MCP_PORT", 8765)
    mcp_path = _env("MCP_PATH", "/mcp")

    logging.info(
        f"Starting MCP server: type={mcp_type} host={mcp_host} port={mcp_port} path={mcp_path}"
    )
```

to:

```python
    mcp_type = _env("MCP_TYPE", "streamable-http")
    mcp_host = _env("MCP_HOST", "0.0.0.0")
    mcp_port = _env_int("MCP_PORT", 8765)
    mcp_path = _env("MCP_PATH", "/mcp")
    graceful_shutdown_timeout = get_mcp_graceful_shutdown_timeout()
    auth_enabled = bool(_auth_token and _auth_token.strip())

    logging.info(
        "mcp.lifecycle.starting type=%s host=%s port=%s path=%s "
        "graceful_shutdown_timeout=%s auth_enabled=%s",
        mcp_type,
        mcp_host,
        mcp_port,
        mcp_path,
        graceful_shutdown_timeout,
        auth_enabled,
    )
```

Then in the two transport branches that recompute the timeout, remove the now-duplicate local assignment. Change the `sse` branch:

```python
        elif mcp_type == "sse":
            graceful_shutdown_timeout = get_mcp_graceful_shutdown_timeout()
            mcp.run(
```

to:

```python
        elif mcp_type == "sse":
            mcp.run(
```

and the `streamable-http` branch:

```python
        elif mcp_type == "streamable-http":
            graceful_shutdown_timeout = get_mcp_graceful_shutdown_timeout()
            mcp.run(
```

to:

```python
        elif mcp_type == "streamable-http":
            mcp.run(
```

(The hoisted `graceful_shutdown_timeout` variable is already in scope for both branches.)

Finally, enrich the `except` block (~line 116-124) with a distinct crash log. Change:

```python
    except Exception as exc:
        capture_exception(
            exc,
            mcp_type=mcp_type,
            mcp_host=mcp_host,
            mcp_port=mcp_port,
            mcp_path=mcp_path,
        )
        raise
```

to:

```python
    except Exception as exc:
        # ROB-469: an unhandled mcp.run() exception is a CRASH, distinct from a
        # graceful mcp.lifecycle.shutdown. Log it explicitly before Sentry capture.
        logging.exception(
            "mcp.lifecycle.crashed type=%s host=%s port=%s", mcp_type, mcp_host, mcp_port
        )
        capture_exception(
            exc,
            mcp_type=mcp_type,
            mcp_host=mcp_host,
            mcp_port=mcp_port,
            mcp_path=mcp_path,
        )
        raise
```

- [ ] **Step 7: Run the wiring test + the full lifecycle test file**

Run: `uv run pytest tests/test_mcp_server_lifecycle.py -v`
Expected: PASS for all four tests (`test_main_module_wires_health_route` now green).

- [ ] **Step 8: Run the existing boot test to confirm no regression**

Run: `uv run pytest tests/test_mcp_tool_registration_boot.py -v`
Expected: PASS (registration surface unchanged; lifespan/health are additive).

- [ ] **Step 9: Commit**

```bash
git add app/mcp_server/main.py tests/test_mcp_server_lifecycle.py
git commit -m "$(cat <<'EOF'
feat(ROB-469 PR1): wire /health + lifecycle logging into MCP main

FastMCP(lifespan=build_server_lifespan()), register_health_route after tools,
enriched mcp.lifecycle.starting startup log (graceful_timeout + auth_enabled),
and a distinct mcp.lifecycle.crashed log on unhandled mcp.run() exceptions.

Co-authored-by: Hermes <hermes@example.invalid>
Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 4: Repoint ops health probes to `/health`→200

**Files:**
- Modify: `ops/native/scripts/healthcheck-native.sh`
- Modify: `ops/native/haproxy/haproxy.cfg.tmpl`
- Modify: `scripts/deploy-native.sh`
- Modify: `docker-compose.prod.yml`

> No automated test harness exists for these shell/config files; each step is an exact edit plus a `grep` verification that the old `/mcp` probe is gone and the new `/health` probe is present.

- [ ] **Step 1: `ops/native/scripts/healthcheck-native.sh` — probe `/health`→200**

Replace the MCP probe block:

```bash
code=$(curl -sS -o /dev/null -w '%{http_code}' -H 'Accept: text/event-stream' "http://127.0.0.1:${MCP_PORT}/mcp" || true)
if [[ "$code" != "401" && "$code" != "400" ]]; then
  echo "mcp unexpected status at :${MCP_PORT}: $code" >&2
  rc=1
fi
```

with:

```bash
# ROB-469: probe the unauthenticated, dependency-free /health route (200) instead
# of the auth-gated /mcp (401/400). A 200 proves the event loop is responsive — a
# wedged loop stops answering /health.
code=$(curl -sS -o /dev/null -w '%{http_code}' "http://127.0.0.1:${MCP_PORT}/health" || true)
if [[ "$code" != "200" ]]; then
  echo "mcp health failed at :${MCP_PORT}: $code" >&2
  rc=1
fi
```

- [ ] **Step 2: `ops/native/haproxy/haproxy.cfg.tmpl` — `bk_mcp` → `GET /health` expect 200**

Replace, inside `backend bk_mcp`, the probe lines:

```
    # ROB-259 review: send the same Accept: text/event-stream header that
    # cloudflared/native healthcheck send, so FastMCP returns the expected
    # 400/401 status. Without the header FastMCP can respond with a different
    # status (e.g. 406) and HAProxy would mark the backend DOWN.
    option httpchk
    http-check send meth GET uri /mcp ver HTTP/1.1 hdr Host trader-mcp.robinco.dev hdr Accept text/event-stream
    http-check expect status 400,401
```

with:

```
    # ROB-469: probe the unauthenticated /health route (200). Dependency-free, so
    # a 200 means the event loop is live; a wedged loop stops answering and HAProxy
    # marks the backend DOWN (inter 5s). Pre-ROB-469 /mcp probe kept commented for
    # one release as a rollback reference.
    option httpchk GET /health
    http-check expect status 200
    # OLD (pre-ROB-469):
    # option httpchk
    # http-check send meth GET uri /mcp ver HTTP/1.1 hdr Host trader-mcp.robinco.dev hdr Accept text/event-stream
    # http-check expect status 400,401
```

- [ ] **Step 3: `scripts/deploy-native.sh` — built-in fallback probe → `/health`→200**

Replace the MCP fallback probe block (inside `run_healthcheck_once`):

```bash
  code="$(curl -sS -o /dev/null -w '%{http_code}' -H 'Accept: text/event-stream' http://127.0.0.1:8765/mcp || true)"
  if [[ "$code" != "401" && "$code" != "400" ]]; then
    echo "MCP unexpected status: $code" >&2
    rc=1
  fi
```

with:

```bash
  # ROB-469: probe unauthenticated /health (200) instead of auth-gated /mcp.
  code="$(curl -sS -o /dev/null -w '%{http_code}' http://127.0.0.1:8765/health || true)"
  if [[ "$code" != "200" ]]; then
    echo "MCP health failed: $code" >&2
    rc=1
  fi
```

- [ ] **Step 4: `docker-compose.prod.yml` — add the missing `mcp` healthcheck**

In the `mcp:` service, immediately after the `command: ["python", "-m", "app.mcp_server.main"]` line, add a healthcheck block (L7 `/health` via stdlib `urllib`, so it needs no `curl` in the image):

```yaml
    command: ["python", "-m", "app.mcp_server.main"]
    healthcheck:
      # ROB-469: L7 probe of the unauthenticated /health route. A TCP-connect
      # check would pass even on a wedged loop; /health proves responsiveness.
      test: [ "CMD", "python", "-c", "import sys,urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8765/health', timeout=5).status == 200 else 1)" ]
      interval: 30s
      timeout: 10s
      retries: 3
      start_period: 40s
```

- [ ] **Step 5: Verify the probe switch with grep**

Run:
```bash
grep -n "/health" ops/native/scripts/healthcheck-native.sh ops/native/haproxy/haproxy.cfg.tmpl scripts/deploy-native.sh docker-compose.prod.yml
grep -n "http-check expect status 200" ops/native/haproxy/haproxy.cfg.tmpl
! grep -nE "expect status 400,401" ops/native/haproxy/haproxy.cfg.tmpl | grep -v '^\s*#' | grep -v '# OLD'
```
Expected: each file shows a new `/health` probe; the active (non-commented) HAProxy expect is `200`; the only remaining `400,401` line is the commented `# OLD` reference.

- [ ] **Step 6: Lint the shell scripts (if shellcheck is available; otherwise skip)**

Run: `command -v shellcheck >/dev/null && shellcheck ops/native/scripts/healthcheck-native.sh scripts/deploy-native.sh || echo "shellcheck not installed — skip"`
Expected: no new errors (or skip message).

- [ ] **Step 7: Commit**

```bash
git add ops/native/scripts/healthcheck-native.sh ops/native/haproxy/haproxy.cfg.tmpl scripts/deploy-native.sh docker-compose.prod.yml
git commit -m "$(cat <<'EOF'
feat(ROB-469 PR1): repoint MCP health probes to unauthenticated /health (200)

native healthcheck-native.sh, HAProxy bk_mcp, deploy-native.sh fallback, and the
docker-compose mcp service (previously had NO healthcheck) now probe /health→200
instead of the auth-gated /mcp→401/400. Old /mcp probe kept commented one release.

Co-authored-by: Hermes <hermes@example.invalid>
Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 5: Supervision runbook

**Files:**
- Create: `docs/runbooks/mcp-health-supervision.md`

- [ ] **Step 1: Write the runbook**

Create `docs/runbooks/mcp-health-supervision.md`:

```markdown
# Runbook: MCP server health & supervision (ROB-469)

The auto_trader MCP server is a single FastMCP `streamable-http` uvicorn process.
Production runs the native launchd blue/green path (8766 blue / 8767 green →
HAProxy → stable 8765). `docker-compose.prod.yml` is the legacy/secondary path.

## /health endpoint
- `GET http://127.0.0.1:<port>/health` → `200 {"status":"ok","service":"auto-trader-mcp","version":"...","uptime_s":...}`.
- **Unauthenticated** (bypasses `MCP_AUTH_TOKEN`) and **dependency-free** (no DB/Redis).
  A 200 means the event loop is responsive; a wedged loop stops answering it.
- Ports: stable 8765 (HAProxy), blue 8766, green 8767.

## Probe interpretation
- `/health` 200 → process up and loop responsive.
- `/health` non-200 / timeout → process down OR event loop wedged. HAProxy
  (`inter 5s`) marks the backend DOWN; launchd `KeepAlive` restarts only on
  process EXIT (a wedged-but-alive process is NOT restarted until PR3's watchdog).

## Lifecycle logs (diagnose a disconnect)
Filter Sentry / logs for `service:auto-trader-mcp`. Log lines:
- `mcp.lifecycle.starting ...` — env/config at boot.
- `mcp.lifecycle.startup_complete tools=N ...` — server ready, N tools registered.
- `mcp.lifecycle.shutdown ...` — graceful stop (teardown ran).
- `mcp.lifecycle.crashed ...` — unhandled `mcp.run()` exception.
- **Diagnosis:** `startup_complete` with NO matching `shutdown` before the next
  `starting` ⇒ hard-kill/OOM/SIGKILL (teardown never ran). A `shutdown` ⇒ graceful.

## Manual checks
```bash
# native, active color (find color from HAProxy or launchctl):
curl -s http://127.0.0.1:8766/health   # blue
curl -s http://127.0.0.1:8767/health   # green
curl -s http://127.0.0.1:8765/health   # stable (via HAProxy)
```

## Restart
- **Native (launchd):** `launchctl kickstart -k gui/$(id -u)/com.robinco.auto-trader.mcp-<color>`
- **Docker (legacy):** `docker compose -f docker-compose.prod.yml restart mcp`

## Notes
- True in-session client reconnect is the Claude Code harness's job, not the server's.
- Continuous hung-but-alive recovery (heartbeat watchdog) lands in ROB-469 PR3.
```

- [ ] **Step 2: Commit**

```bash
git add docs/runbooks/mcp-health-supervision.md
git commit -m "$(cat <<'EOF'
docs(ROB-469 PR1): MCP health & supervision runbook

/health interpretation, lifecycle-log diagnosis (startup w/o shutdown = hard-kill),
manual checks per port, restart per deploy path.

Co-authored-by: Hermes <hermes@example.invalid>
Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 6: Full verification gate

- [ ] **Step 1: Run the full lifecycle + boot tests**

Run: `uv run pytest tests/test_mcp_server_lifecycle.py tests/test_mcp_tool_registration_boot.py -v`
Expected: all PASS.

- [ ] **Step 2: Lint + format the changed Python (CI checks `app/` AND `tests/`)**

Run: `uv run ruff check app/mcp_server/lifecycle.py app/mcp_server/main.py tests/test_mcp_server_lifecycle.py && uv run ruff format --check app/mcp_server/lifecycle.py app/mcp_server/main.py tests/test_mcp_server_lifecycle.py`
Expected: no lint errors; format clean. (If format fails, run `uv run ruff format <files>` and amend.)

- [ ] **Step 3: Confirm zero migration / zero broker mutation**

Run: `git diff --name-only main...HEAD`
Expected: only `app/mcp_server/lifecycle.py`, `app/mcp_server/main.py`, `tests/test_mcp_server_lifecycle.py`, the four ops files, and the two docs files. No `alembic/`, no `app/services/brokers/`, no order/ledger paths.

- [ ] **Step 4: Push and open the PR (only when the user asks to ship)**

```bash
git push -u origin rob-469
```
Then open a PR (base `main`) titled `feat(ROB-469 PR1): MCP /health + lifecycle observability + health-probe repoint`, body summarizing: unauthenticated dependency-free /health, lifecycle logging (startup/shutdown/crashed diagnosis), ops probes repointed to /health→200, runbook. Note: zero migration, no broker/order mutation; native launchd is the authoritative path.

---

## Operator deploy note (post-merge, gated)
Deploy the **app change first** (so `/health` exists), verify `curl /health`→200 on 8766/8767/8765, *then* the config change is already bundled — the HAProxy/native probes will use `/health` on the next deploy cycle. The commented `/mcp` probe is the one-release rollback reference.

---

## Self-review notes (author)
- **Spec coverage (§4):** /health route (T1), lifespan logging (T2), main wiring + enriched startup/crash log (T3), native+HAProxy+deploy+docker probe repoint (T4), runbook (T5). All §4 items mapped.
- **Out of scope (correct):** no per-tool timeout/QueuePool/semaphore (PR2), no watchdog/heartbeat (PR3), no get_news (deferred).
- **Type consistency:** `register_health_route(mcp, *, service, version)` and `build_server_lifespan(*, service)` used identically in tests and main wiring; `STARTED_MONOTONIC` single source; log keys `mcp.lifecycle.{starting,startup_complete,shutdown,crashed}` consistent across code, tests, and runbook.
- **No placeholders:** every code/edit/command step contains the literal content.
