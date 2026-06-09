# ROB-469 PR3 — Self-heal watchdog Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Recover a hung-but-alive MCP server — the case PR2's per-tool timeout cannot fix (a tool blocking the event loop *synchronously*). The MCP process writes a periodic heartbeat from its event loop; an external launchd watchdog detects a stale heartbeat (loop wedged) and force-restarts that color via `launchctl kickstart -k`.

**Architecture:** A heartbeat asyncio task started in PR1's `build_server_lifespan` writes `{updated_at_unix, color, is_running}` atomically every N seconds. If the loop wedges, the heartbeat goes stale. `scripts/mcp_watchdog.py` (run by a new launchd job, **dry-run by default**) polls both color heartbeats and kickstarts only a **wedged** color (is_running=true + stale) whose launchd job is currently **loaded** — so it never restarts the inactive/booted-out blue/green color, and clean exits are left to launchd `KeepAlive`.

**Tech Stack:** Python 3.13 asyncio, FastMCP lifespan, macOS launchd (`launchctl kickstart`), bash ops wrappers, pytest.

**Spec:** ROB-469 design spec §6 (`docs/superpowers/specs/2026-06-09-rob-469-mcp-server-resilience-design.md`).

**Branch / worktree:** `/Users/mgh3326/work/auto_trader.rob-469-pr3`, branch `rob-469-pr3`, **stacked on PR1's branch `rob-469`** (it extends PR1's `build_server_lifespan`). PR base = `rob-469`; after PR1 merges, rebase onto `main`. Independent of PR2.

---

## Background facts (verified against current branch — do not re-derive)

- **PR1's lifespan** (`app/mcp_server/lifecycle.py::build_server_lifespan`) logs startup/shutdown around `yield {}`. PR3 starts the heartbeat task before `yield` and cancels it in the `finally`. The module already imports `logging`, `time`, `AsyncIterator`, `TYPE_CHECKING`, `fastmcp_lifespan`, and has `STARTED_MONOTONIC`.
- **Atomic heartbeat precedent** (`websocket_monitor.py:123-165` `_write_heartbeat`): build dict with `"updated_at_unix": time.time()`, `heartbeat_path.parent.mkdir(parents=True, exist_ok=True)`, write to `heartbeat_path.with_suffix(".tmp")`, then `temp_path.replace(heartbeat_path)` (atomic rename), `except OSError` → `logger.warning` (never crash). Mirror this exactly.
- **Heartbeat dir convention:** `$AUTO_TRADER_BASE/state/heartbeat/` (WS uses `kis.json`/`upbit.json`, per `healthcheck-native.sh:64-66`). MCP uses `mcp-<color>.json`.
- **`run-mcp.sh`** sets `COLOR` from `AUTO_TRADER_COLOR` (blue→8766/green→8767), exports `MCP_PORT`, then `exec uv run python -m app.mcp_server.main`. PR3 adds an `export MCP_HEARTBEAT_PATH=...` before the exec; `AUTO_TRADER_COLOR` is already in the process env (from the plist).
- **launchd plist shape** (`ops/native/plists/com.robinco.auto-trader.mcp-blue.plist`): `Label`, `ProgramArguments` (one script path), `WorkingDirectory`, `EnvironmentVariables` (HOME, PATH, AUTO_TRADER_BASE, AUTO_TRADER_ENV_FILE, AUTO_TRADER_COLOR, AUTO_TRADER_MCP_PORT), `RunAtLoad`, `KeepAlive=true`, `ThrottleInterval=10`, resource limits, `StandardOutPath`/`StandardErrorPath`. Plist paths are absolute under `/Users/mgh3326/services/auto_trader/...`.
- **Deploy auto-wires single-active jobs** (`scripts/deploy-native.sh`): `SINGLE_ACTIVE_LABELS=(worker scheduler kis-websocket upbit-websocket)` (line ~51). `restart_single_active_services()` (line ~132) for each label: `install -m 0644 $PLIST_DIR/$label.plist $HOME/Library/LaunchAgents/$label.plist`, bootout, bootstrap (5 retries), `launchctl enable`, `launchctl kickstart -k gui/$uid/$label`. Plists are rsync'd from `$NEW_RELEASE/ops/native/plists/` to `$PLIST_DIR` (line ~86). So **adding the watchdog label + its plist file makes deploy install and start it automatically.**
- **The watchdog's restart command:** `launchctl kickstart -k gui/$(id -u)/com.robinco.auto-trader.mcp-<color>` (same form deploy uses). `-k` force-kills then restarts; loads-check via `launchctl print gui/$uid/<label>` (exit 0 = loaded).

## Branch-interaction note
PR3 stacks on PR1 (needs the lifespan). It modifies `app/mcp_server/lifecycle.py`, `app/mcp_server/env_utils.py`, `app/mcp_server/main.py` (none — see below), `ops/native/scripts/run-mcp.sh`, `scripts/deploy-native.sh`, and the PR1 runbook. PR2 also touched `env_utils.py` (timeout helpers) — at merge both append distinct functions (clean/auto-merge). PR3 does **not** touch `main.py` (the heartbeat lives entirely in the lifespan PR1 already wired) and does **not** touch `tests/test_mcp_server_main.py`.

---

## File structure

| File | Responsibility | Action |
|---|---|---|
| `app/mcp_server/heartbeat.py` | atomic heartbeat write + loop (testable unit) | **Create** |
| `app/mcp_server/env_utils.py` | `get_mcp_heartbeat_path()` / `_interval_s()` / `get_mcp_color()` | **Modify** |
| `app/mcp_server/lifecycle.py` | start/stop heartbeat task in `build_server_lifespan` | **Modify** |
| `tests/test_mcp_heartbeat.py` | heartbeat write + loop tests | **Create** |
| `scripts/mcp_watchdog.py` | `evaluate_heartbeat` (pure) + poll loop + kickstart | **Create** |
| `tests/test_mcp_watchdog.py` | evaluate/read/decision tests | **Create** |
| `ops/native/scripts/run-mcp.sh` | export `MCP_HEARTBEAT_PATH` | **Modify** |
| `ops/native/scripts/run-mcp-watchdog.sh` | env-setup wrapper that execs the watchdog | **Create** |
| `ops/native/plists/com.robinco.auto-trader.mcp-watchdog.plist` | launchd job for the watchdog | **Create** |
| `scripts/deploy-native.sh` | add watchdog to `SINGLE_ACTIVE_LABELS` | **Modify** |
| `docs/runbooks/mcp-health-supervision.md` | watchdog section (PR1's runbook) | **Modify** |

---

## Task 1: Heartbeat writer + lifespan integration

**Files:**
- Create: `app/mcp_server/heartbeat.py`
- Modify: `app/mcp_server/env_utils.py`, `app/mcp_server/lifecycle.py`
- Test: `tests/test_mcp_heartbeat.py`

- [ ] **Step 1: Write failing tests**

Create `tests/test_mcp_heartbeat.py`:

```python
"""ROB-469 PR3: MCP heartbeat writer tests."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from app.mcp_server.heartbeat import write_heartbeat


@pytest.mark.unit
def test_write_heartbeat_atomic_payload(tmp_path: Path) -> None:
    hb = tmp_path / "state" / "heartbeat" / "mcp-blue.json"
    write_heartbeat(hb, color="blue", is_running=True)
    assert hb.exists()
    data = json.loads(hb.read_text())
    assert data["color"] == "blue"
    assert data["is_running"] is True
    assert isinstance(data["updated_at_unix"], (int, float))
    # no leftover temp file
    assert not (hb.with_suffix(".tmp")).exists()


@pytest.mark.unit
def test_write_heartbeat_creates_parent_dirs(tmp_path: Path) -> None:
    hb = tmp_path / "a" / "b" / "c" / "mcp-green.json"
    write_heartbeat(hb, color="green", is_running=False)
    assert hb.exists()
    assert json.loads(hb.read_text())["is_running"] is False


@pytest.mark.unit
def test_write_heartbeat_swallows_oserror(tmp_path: Path) -> None:
    # A path whose parent is a FILE (not a dir) makes mkdir/replace fail; the
    # writer must warn and NOT raise (a heartbeat failure must never crash the loop).
    clash = tmp_path / "clash"
    clash.write_text("i am a file")
    hb = clash / "mcp-blue.json"
    write_heartbeat(hb, color="blue", is_running=True)  # must not raise


@pytest.mark.unit
@pytest.mark.asyncio
async def test_heartbeat_loop_writes_then_marks_stopped_on_cancel(tmp_path: Path) -> None:
    from app.mcp_server.heartbeat import heartbeat_loop

    hb = tmp_path / "mcp-blue.json"
    task = asyncio.create_task(heartbeat_loop(hb, interval_s=0.05, color="blue"))
    await asyncio.sleep(0.12)  # at least one write
    assert json.loads(hb.read_text())["is_running"] is True
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    # graceful cancel writes a final is_running=False
    assert json.loads(hb.read_text())["is_running"] is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_mcp_heartbeat.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.mcp_server.heartbeat'`.

- [ ] **Step 3: Create `app/mcp_server/heartbeat.py`**

```python
"""ROB-469 PR3: MCP server liveness heartbeat.

The MCP server writes a heartbeat file from its event loop every N seconds. If the
loop wedges (e.g. a synchronous-blocking tool that PR2's timeout cannot cancel), the
heartbeat goes stale and the external watchdog (scripts/mcp_watchdog.py) restarts the
process. Atomic write mirrors websocket_monitor._write_heartbeat.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path

logger = logging.getLogger(__name__)


def write_heartbeat(path: str | Path, *, color: str, is_running: bool) -> None:
    """Atomically write the heartbeat file. Never raises (a heartbeat failure must
    not crash the server loop)."""
    data = {
        "updated_at_unix": time.time(),
        "service": "auto-trader-mcp",
        "color": color,
        "is_running": is_running,
    }
    hb = Path(path)
    try:
        hb.parent.mkdir(parents=True, exist_ok=True)
        tmp = hb.with_suffix(".tmp")
        with open(tmp, "w") as f:
            json.dump(data, f)
        tmp.replace(hb)
    except OSError as exc:
        logger.warning("mcp.heartbeat.write_failed path=%s err=%s", path, exc)


async def heartbeat_loop(path: str | Path, *, interval_s: float, color: str) -> None:
    """Write a heartbeat immediately, then every ``interval_s`` seconds. On
    cancellation (graceful shutdown) write a final ``is_running=False`` so the
    watchdog distinguishes a clean stop from a wedge."""
    try:
        while True:
            write_heartbeat(path, color=color, is_running=True)
            await asyncio.sleep(interval_s)
    except asyncio.CancelledError:
        write_heartbeat(path, color=color, is_running=False)
        raise
```

- [ ] **Step 4: Add env helpers to `app/mcp_server/env_utils.py`**

Append after `get_mcp_graceful_shutdown_timeout()`:

```python
def get_mcp_heartbeat_path() -> str | None:
    """Path the MCP server writes its liveness heartbeat to. None disables it
    (the watchdog only runs in the native deployment, which sets this)."""
    return _env("MCP_HEARTBEAT_PATH")


def get_mcp_heartbeat_interval_s() -> float:
    """Seconds between MCP heartbeat writes (default 10)."""
    raw = _env("MCP_HEARTBEAT_INTERVAL_S")
    if raw is None:
        return 10.0
    try:
        return float(raw)
    except ValueError:
        logging.warning(
            f"Invalid float for MCP_HEARTBEAT_INTERVAL_S={raw!r}, using default=10.0"
        )
        return 10.0


def get_mcp_color() -> str:
    """Deployment color (blue/green) for heartbeat tagging; 'unknown' if unset."""
    return _env("AUTO_TRADER_COLOR") or "unknown"
```

- [ ] **Step 5: Start/stop the heartbeat in `build_server_lifespan`**

In `app/mcp_server/lifecycle.py`, add imports near the top (after the existing `import time`):

```python
import asyncio
import contextlib

from app.mcp_server.env_utils import (
    get_mcp_color,
    get_mcp_heartbeat_interval_s,
    get_mcp_heartbeat_path,
)
from app.mcp_server.heartbeat import heartbeat_loop
```

Then wrap the heartbeat task around the existing `yield {}` in `_server_lifespan`. Change:

```python
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
```

to:

```python
        logger.info(
            "mcp.lifecycle.startup_complete service=%s tools=%d uptime_s=%.1f",
            service,
            tool_count,
            time.monotonic() - STARTED_MONOTONIC,
        )
        # ROB-469 PR3: liveness heartbeat task (no-op when MCP_HEARTBEAT_PATH unset).
        heartbeat_task: asyncio.Task | None = None
        hb_path = get_mcp_heartbeat_path()
        if hb_path:
            heartbeat_task = asyncio.create_task(
                heartbeat_loop(
                    hb_path,
                    interval_s=get_mcp_heartbeat_interval_s(),
                    color=get_mcp_color(),
                )
            )
            logger.info("mcp.heartbeat.started path=%s", hb_path)
        try:
            yield {}
        finally:
            if heartbeat_task is not None:
                heartbeat_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await heartbeat_task
            logger.info(
                "mcp.lifecycle.shutdown service=%s uptime_s=%.1f",
                service,
                time.monotonic() - STARTED_MONOTONIC,
            )
```

- [ ] **Step 6: Run tests + PR1 lifecycle regression**

Run: `uv run pytest tests/test_mcp_heartbeat.py tests/test_mcp_server_lifecycle.py -q`
Expected: all PASS (heartbeat tests green; PR1 lifecycle tests still green — heartbeat is a no-op when `MCP_HEARTBEAT_PATH` is unset, which it is in tests).

- [ ] **Step 7: Commit**

```bash
git add app/mcp_server/heartbeat.py app/mcp_server/env_utils.py app/mcp_server/lifecycle.py tests/test_mcp_heartbeat.py
git commit -m "$(cat <<'EOF'
feat(ROB-469 PR3): MCP liveness heartbeat in the server lifespan

write_heartbeat (atomic tmp+replace, never raises) + heartbeat_loop (writes every N s,
final is_running=False on graceful cancel). Started in PR1's build_server_lifespan,
gated on MCP_HEARTBEAT_PATH (no-op otherwise, so tests/dev are unaffected). If the event
loop wedges, the heartbeat goes stale — the signal the watchdog (next commit) acts on.

Co-authored-by: Hermes <hermes@example.invalid>
Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: Watchdog (`scripts/mcp_watchdog.py`)

**Files:**
- Create: `scripts/mcp_watchdog.py`
- Test: `tests/test_mcp_watchdog.py`

- [ ] **Step 1: Write failing tests**

Create `tests/test_mcp_watchdog.py`:

```python
"""ROB-469 PR3: MCP watchdog decision-logic tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.mcp_watchdog import evaluate_heartbeat, read_heartbeat


@pytest.mark.unit
def test_evaluate_missing_is_skipped() -> None:
    assert evaluate_heartbeat(None, now=1000.0, stale_threshold_s=30.0) == "missing"


@pytest.mark.unit
def test_evaluate_stopped_is_skipped() -> None:
    data = {"updated_at_unix": 1000.0, "is_running": False, "color": "blue"}
    assert evaluate_heartbeat(data, now=1000.0, stale_threshold_s=30.0) == "stopped"


@pytest.mark.unit
def test_evaluate_fresh_is_healthy() -> None:
    data = {"updated_at_unix": 980.0, "is_running": True, "color": "blue"}
    assert evaluate_heartbeat(data, now=1000.0, stale_threshold_s=30.0) == "healthy"


@pytest.mark.unit
def test_evaluate_stale_running_is_wedged() -> None:
    data = {"updated_at_unix": 900.0, "is_running": True, "color": "blue"}
    assert evaluate_heartbeat(data, now=1000.0, stale_threshold_s=30.0) == "wedged"


@pytest.mark.unit
def test_evaluate_running_without_timestamp_is_missing() -> None:
    data = {"is_running": True, "color": "blue"}
    assert evaluate_heartbeat(data, now=1000.0, stale_threshold_s=30.0) == "missing"


@pytest.mark.unit
def test_read_heartbeat_missing_returns_none(tmp_path: Path) -> None:
    assert read_heartbeat(tmp_path / "nope.json") is None


@pytest.mark.unit
def test_read_heartbeat_corrupt_returns_none(tmp_path: Path) -> None:
    p = tmp_path / "mcp-blue.json"
    p.write_text("{not json")
    assert read_heartbeat(p) is None


@pytest.mark.unit
def test_read_heartbeat_valid_returns_dict(tmp_path: Path) -> None:
    p = tmp_path / "mcp-blue.json"
    p.write_text(json.dumps({"is_running": True, "updated_at_unix": 1.0}))
    assert read_heartbeat(p) == {"is_running": True, "updated_at_unix": 1.0}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_mcp_watchdog.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'scripts.mcp_watchdog'`.

- [ ] **Step 3: Create `scripts/mcp_watchdog.py`**

```python
#!/usr/bin/env python3
"""ROB-469 PR3: MCP self-heal watchdog.

Polls the per-color MCP heartbeat files. A color is restarted (launchctl kickstart -k)
ONLY when it is "wedged" — is_running=true but the heartbeat is stale (the event loop
is alive but stuck, which PR2's per-tool timeout cannot cancel) — AND its launchd job
is currently loaded (so we never restart the inactive blue/green color). Clean exits
are left to launchd KeepAlive.

DRY-RUN BY DEFAULT: logs the decision but does NOT kickstart unless
MCP_WATCHDOG_DRY_RUN=false (or --no-dry-run). This lets an operator observe heartbeats
before arming the self-heal.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
import time
from pathlib import Path

logger = logging.getLogger("mcp_watchdog")

COLORS = ("blue", "green")
LABEL_FMT = "com.robinco.auto-trader.mcp-{color}"


def read_heartbeat(path: str | Path) -> dict | None:
    """Return the parsed heartbeat dict, or None if missing/unreadable/corrupt."""
    try:
        with open(path) as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def evaluate_heartbeat(
    data: dict | None, *, now: float, stale_threshold_s: float
) -> str:
    """Classify a heartbeat: 'missing' | 'stopped' | 'healthy' | 'wedged'. Only
    'wedged' (running but stale) warrants a restart."""
    if data is None:
        return "missing"
    if not data.get("is_running", False):
        return "stopped"
    updated = data.get("updated_at_unix")
    if not isinstance(updated, (int, float)):
        return "missing"
    return "wedged" if (now - float(updated)) > stale_threshold_s else "healthy"


def _job_is_loaded(label: str, *, uid: int) -> bool:
    result = subprocess.run(
        ["launchctl", "print", f"gui/{uid}/{label}"],
        capture_output=True,
        text=True,
    )
    return result.returncode == 0


def _kickstart(label: str, *, uid: int) -> None:
    subprocess.run(
        ["launchctl", "kickstart", "-k", f"gui/{uid}/{label}"],
        capture_output=True,
        text=True,
    )


def check_once(
    heartbeat_dir: Path, *, stale_threshold_s: float, dry_run: bool, uid: int
) -> dict[str, str]:
    """One pass over both colors. Returns {color: status}. Kickstarts a wedged color
    whose launchd job is loaded, unless dry_run."""
    now = time.time()
    statuses: dict[str, str] = {}
    for color in COLORS:
        label = LABEL_FMT.format(color=color)
        data = read_heartbeat(heartbeat_dir / f"mcp-{color}.json")
        status = evaluate_heartbeat(data, now=now, stale_threshold_s=stale_threshold_s)
        statuses[color] = status
        if status != "wedged":
            continue
        if not _job_is_loaded(label, uid=uid):
            logger.info("mcp.watchdog.skip color=%s wedged-but-not-loaded", color)
            continue
        if dry_run:
            logger.warning("mcp.watchdog.would_kickstart color=%s (dry-run)", color)
        else:
            logger.warning("mcp.watchdog.kickstart color=%s (stale heartbeat)", color)
            _kickstart(label, uid=uid)
    return statuses


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="mcp_watchdog")
    p.add_argument("--heartbeat-dir", type=Path, default=None)
    p.add_argument("--interval-s", type=float, default=15.0)
    p.add_argument("--stale-threshold-s", type=float, default=30.0)
    p.add_argument("--once", action="store_true", help="One pass then exit.")
    dry = os.getenv("MCP_WATCHDOG_DRY_RUN", "true").strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
    )
    p.add_argument("--no-dry-run", dest="dry_run", action="store_false", default=dry)
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    args = _parse_args(argv)
    base = os.getenv("AUTO_TRADER_BASE", os.path.expanduser("~/services/auto_trader"))
    hb_dir = args.heartbeat_dir or Path(base) / "state" / "heartbeat"
    uid = os.getuid()
    logger.info(
        "mcp.watchdog.start dir=%s interval_s=%s stale_s=%s dry_run=%s",
        hb_dir, args.interval_s, args.stale_threshold_s, args.dry_run,
    )
    while True:
        check_once(hb_dir, stale_threshold_s=args.stale_threshold_s, dry_run=args.dry_run, uid=uid)
        if args.once:
            return 0
        time.sleep(args.interval_s)


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_mcp_watchdog.py -q`
Expected: PASS (8 tests).

- [ ] **Step 5: Commit**

```bash
git add scripts/mcp_watchdog.py tests/test_mcp_watchdog.py
git commit -m "$(cat <<'EOF'
feat(ROB-469 PR3): mcp_watchdog — restart a wedged MCP color (dry-run default)

Polls per-color heartbeats; evaluate_heartbeat → missing|stopped|healthy|wedged. Only a
WEDGED color (is_running=true + stale) whose launchd job is LOADED is restarted via
launchctl kickstart -k (so the inactive blue/green color is never touched; clean exits
are left to launchd KeepAlive). DRY-RUN BY DEFAULT (MCP_WATCHDOG_DRY_RUN=false to arm).

Co-authored-by: Hermes <hermes@example.invalid>
Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 3: Ops wiring (heartbeat env + watchdog launchd job)

**Files:**
- Modify: `ops/native/scripts/run-mcp.sh`
- Create: `ops/native/scripts/run-mcp-watchdog.sh`
- Create: `ops/native/plists/com.robinco.auto-trader.mcp-watchdog.plist`
- Modify: `scripts/deploy-native.sh`

- [ ] **Step 1: Export `MCP_HEARTBEAT_PATH` from `run-mcp.sh`**

In `ops/native/scripts/run-mcp.sh`, change the tail:

```bash
_export_selected_env_prefixes MCP_
export MCP_PORT="$PORT"

exec uv run python -m app.mcp_server.main
```

to:

```bash
_export_selected_env_prefixes MCP_
export MCP_PORT="$PORT"
# ROB-469 PR3: per-color liveness heartbeat the watchdog polls.
export MCP_HEARTBEAT_PATH="${AUTO_TRADER_BASE:-$HOME/services/auto_trader}/state/heartbeat/mcp-${COLOR}.json"

exec uv run python -m app.mcp_server.main
```

- [ ] **Step 2: Create the watchdog wrapper `ops/native/scripts/run-mcp-watchdog.sh`**

```bash
#!/usr/bin/env bash
# ROB-469 PR3: launchd wrapper for the MCP self-heal watchdog.
set -euo pipefail

export AUTO_TRADER_CURRENT="${AUTO_TRADER_BASE:-$HOME/services/auto_trader}/current-blue"
source "${AUTO_TRADER_BASE:-$HOME/services/auto_trader}/scripts/common.sh"

# Arm the kickstart only when the operator sets MCP_WATCHDOG_DRY_RUN=false in the env
# file; default stays dry-run (observe-only).
_export_selected_env_prefixes MCP_WATCHDOG_

exec uv run python -m scripts.mcp_watchdog
```

> Note: `current-blue` is only used so `common.sh` can `cd` into a valid release dir; the watchdog itself reads `AUTO_TRADER_BASE` for the heartbeat dir and acts on whichever color is wedged.

- [ ] **Step 3: Create the watchdog plist `ops/native/plists/com.robinco.auto-trader.mcp-watchdog.plist`**

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.robinco.auto-trader.mcp-watchdog</string>
  <key>ProgramArguments</key>
  <array>
    <string>/Users/mgh3326/services/auto_trader/scripts/run-mcp-watchdog.sh</string>
  </array>
  <key>WorkingDirectory</key>
  <string>/Users/mgh3326/services/auto_trader/current-blue</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>HOME</key>
    <string>/Users/mgh3326</string>
    <key>PATH</key>
    <string>/Users/mgh3326/.local/bin:/Users/mgh3326/.hermes/node/bin:/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
    <key>AUTO_TRADER_BASE</key>
    <string>/Users/mgh3326/services/auto_trader</string>
    <key>AUTO_TRADER_ENV_FILE</key>
    <string>/Users/mgh3326/services/auto_trader/shared/.env.prod.native</string>
  </dict>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>ThrottleInterval</key>
  <integer>10</integer>
  <key>StandardOutPath</key>
  <string>/Users/mgh3326/services/auto_trader/logs/com.robinco.auto-trader.mcp-watchdog.out.log</string>
  <key>StandardErrorPath</key>
  <string>/Users/mgh3326/services/auto_trader/logs/com.robinco.auto-trader.mcp-watchdog.err.log</string>
</dict>
</plist>
```

- [ ] **Step 4: Add the watchdog to `SINGLE_ACTIVE_LABELS` in `scripts/deploy-native.sh`**

Change:

```bash
SINGLE_ACTIVE_LABELS=(
  "com.robinco.auto-trader.worker"
  "com.robinco.auto-trader.scheduler"
  "com.robinco.auto-trader.kis-websocket"
  "com.robinco.auto-trader.upbit-websocket"
)
```

to:

```bash
SINGLE_ACTIVE_LABELS=(
  "com.robinco.auto-trader.worker"
  "com.robinco.auto-trader.scheduler"
  "com.robinco.auto-trader.kis-websocket"
  "com.robinco.auto-trader.upbit-websocket"
  # ROB-469 PR3: single non-color-specific watchdog that restarts a wedged MCP color.
  "com.robinco.auto-trader.mcp-watchdog"
)
```

- [ ] **Step 5: Verify shell + plist sanity**

Run:
```bash
bash -n ops/native/scripts/run-mcp.sh ops/native/scripts/run-mcp-watchdog.sh scripts/deploy-native.sh && echo "bash syntax OK"
plutil -lint ops/native/plists/com.robinco.auto-trader.mcp-watchdog.plist 2>/dev/null || python3 -c "import plistlib; plistlib.loads(open('ops/native/plists/com.robinco.auto-trader.mcp-watchdog.plist','rb').read()); print('plist OK')"
chmod +x ops/native/scripts/run-mcp-watchdog.sh
grep -n "mcp-watchdog" scripts/deploy-native.sh ops/native/plists/com.robinco.auto-trader.mcp-watchdog.plist
```
Expected: bash syntax OK; plist OK; the watchdog label is present in `SINGLE_ACTIVE_LABELS`.

- [ ] **Step 6: Commit**

```bash
git add ops/native/scripts/run-mcp.sh ops/native/scripts/run-mcp-watchdog.sh ops/native/plists/com.robinco.auto-trader.mcp-watchdog.plist scripts/deploy-native.sh
git commit -m "$(cat <<'EOF'
feat(ROB-469 PR3): native launchd wiring for the MCP watchdog

run-mcp.sh exports MCP_HEARTBEAT_PATH (per-color). New run-mcp-watchdog.sh wrapper +
com.robinco.auto-trader.mcp-watchdog.plist (KeepAlive, RunAtLoad). deploy-native.sh adds
the watchdog to SINGLE_ACTIVE_LABELS so the existing rsync+bootstrap+kickstart loop
installs and starts it. Watchdog stays dry-run until MCP_WATCHDOG_DRY_RUN=false.

Co-authored-by: Hermes <hermes@example.invalid>
Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 4: Runbook — watchdog section

**Files:**
- Modify: `docs/runbooks/mcp-health-supervision.md`

- [ ] **Step 1: Append a watchdog section**

Add to the end of `docs/runbooks/mcp-health-supervision.md`:

```markdown

## Self-heal watchdog (ROB-469 PR3)
The MCP server writes a per-color heartbeat (`$AUTO_TRADER_BASE/state/heartbeat/mcp-<color>.json`,
`{updated_at_unix, color, is_running}`) every `MCP_HEARTBEAT_INTERVAL_S` (default 10s) from
its event loop. `com.robinco.auto-trader.mcp-watchdog` (launchd, installed by deploy)
polls both colors and restarts a **wedged** color — `is_running=true` but stale
(> stale threshold, default 30s) AND its launchd job loaded — via
`launchctl kickstart -k gui/$(id -u)/com.robinco.auto-trader.mcp-<color>`. This covers the
hung-but-alive case PR2's per-tool timeout cannot (a synchronous-blocking tool).

- **Why both:** a clean crash/OOM is restarted by launchd `KeepAlive` (process EXIT); a
  wedged-but-alive loop never exits, so only the heartbeat-staleness watchdog catches it.
- **Inactive color is never restarted:** a wedged color is acted on only if its launchd
  job is loaded; a graceful stop writes `is_running=false`; a never-started color has no file.
- **Arming:** the watchdog ships **dry-run** (logs `mcp.watchdog.would_kickstart`). Set
  `MCP_WATCHDOG_DRY_RUN=false` in `shared/.env.prod.native` and restart the watchdog to arm.
- **Manual check:** `cat $AUTO_TRADER_BASE/state/heartbeat/mcp-<color>.json`;
  `uv run python -m scripts.mcp_watchdog --once` (dry-run) prints per-color status.
- **Logs:** `logs/com.robinco.auto-trader.mcp-watchdog.{out,err}.log`; filter `mcp.watchdog.*`.
```

- [ ] **Step 2: Commit**

```bash
git add docs/runbooks/mcp-health-supervision.md
git commit -m "$(cat <<'EOF'
docs(ROB-469 PR3): watchdog section in the MCP supervision runbook

Heartbeat path/fields, wedged-vs-clean-exit recovery split, inactive-color safety,
arming via MCP_WATCHDOG_DRY_RUN=false, manual --once check, log locations.

Co-authored-by: Hermes <hermes@example.invalid>
Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 5: Verification gate

- [ ] **Step 1: Run all PR3 tests + PR1 regressions**

Run: `uv run pytest tests/test_mcp_heartbeat.py tests/test_mcp_watchdog.py tests/test_mcp_server_lifecycle.py tests/test_mcp_tool_registration_boot.py -q`
Expected: all PASS.

- [ ] **Step 2: Lint/format the changed Python**

Run: `uv run ruff check app/mcp_server/heartbeat.py app/mcp_server/env_utils.py app/mcp_server/lifecycle.py scripts/mcp_watchdog.py tests/test_mcp_heartbeat.py tests/test_mcp_watchdog.py`
Then `ruff format --check` the same. Fix + amend if needed.

- [ ] **Step 3: Watchdog smoke (dry-run, no DB needed)**

Run:
```bash
mkdir -p /tmp/hbsmoke && printf '{"updated_at_unix": 1, "is_running": true, "color": "blue"}' > /tmp/hbsmoke/mcp-blue.json
uv run python -m scripts.mcp_watchdog --once --heartbeat-dir /tmp/hbsmoke --stale-threshold-s 5 2>&1 | grep -E "watchdog"
```
Expected: logs `mcp.watchdog.would_kickstart color=blue (dry-run)` for blue (stale, but `_job_is_loaded` is false locally → it logs `skip ... not-loaded`; either way it must NOT raise and must NOT kickstart).

- [ ] **Step 4: Scope + guardrails**

Run: `git diff --name-only rob-469..HEAD`
Expected only: `app/mcp_server/heartbeat.py`, `app/mcp_server/env_utils.py`, `app/mcp_server/lifecycle.py`, `scripts/mcp_watchdog.py`, `tests/test_mcp_heartbeat.py`, `tests/test_mcp_watchdog.py`, `ops/native/scripts/run-mcp.sh`, `ops/native/scripts/run-mcp-watchdog.sh`, `ops/native/plists/com.robinco.auto-trader.mcp-watchdog.plist`, `scripts/deploy-native.sh`, `docs/runbooks/mcp-health-supervision.md`, and this plan doc. No migration, no broker/order/ledger, no `main.py`, no `test_mcp_server_main.py`.

- [ ] **Step 5: Push + open PR (only when the user asks to ship)**

```bash
git push -u origin rob-469-pr3
```
PR base `rob-469` (stacked on PR1); retarget to `main` after PR1 merges. Title: `feat(ROB-469 PR3): MCP self-heal watchdog (heartbeat + launchctl kickstart)`.

---

## Self-review notes (author)
- **Spec coverage (§6):** heartbeat writer in lifespan (T1), watchdog + kickstart (T2), ops/plist/deploy wiring (T3), runbook (T4). All §6 items mapped.
- **Safety:** watchdog is dry-run by default; restarts only a wedged+loaded color; never touches the inactive color; clean exits left to launchd. Heartbeat write never raises.
- **No placeholders.** Every step has literal code/commands.
- **Type/name consistency:** `write_heartbeat(path, *, color, is_running)`, `heartbeat_loop(path, *, interval_s, color)`, `evaluate_heartbeat(data, *, now, stale_threshold_s)`, `read_heartbeat(path)`, `check_once(...)` used identically across code, tests, and ops. Env keys `MCP_HEARTBEAT_PATH`/`MCP_HEARTBEAT_INTERVAL_S`/`MCP_WATCHDOG_DRY_RUN`/`AUTO_TRADER_COLOR` consistent across app, wrapper, plist, and runbook.
- **Stacked on PR1:** extends `build_server_lifespan`; does not touch `main.py` or `test_mcp_server_main.py`.
