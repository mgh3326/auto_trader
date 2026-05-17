# ROB-259: HAProxy Blue/Green for FastAPI + FastMCP — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Eliminate deploy-time 502/connection-refused on `trader.robinco.dev` and `trader-mcp.robinco.dev` by introducing an HAProxy stable origin in front of FastAPI and FastMCP, and adding a blue/green color switch flow to `scripts/deploy-native.sh`.

**Architecture:** HAProxy runs as a launchd-managed stable listener owning `127.0.0.1:8000` (FastAPI) and `127.0.0.1:8765` (FastMCP). Two color-specific launchd services per app (api-blue:8001, api-green:8002, mcp-blue:8766, mcp-green:8767) read their own per-color release symlinks (`current-blue`, `current-green`). Deploy bootstraps the inactive color against the new release, smokes it directly, atomically rewrites HAProxy config + reloads via SIGUSR2, smokes public endpoints, then drains the old color. Worker/scheduler/websocket services stay single-active and keep using `current`.

**Tech Stack:** macOS launchd, HAProxy (Homebrew master-worker mode), bash, Python+pytest+subprocess for testing, Cloudflare Tunnel (unchanged), Alembic backward-compatible migrations.

---

## Scope & Non-Goals

**In scope (single PR):**

- HAProxy install guidance + repo-managed config template
- HAProxy launchd plist
- Color-aware FastAPI + FastMCP wrappers
- Color-specific FastAPI + FastMCP plists (4 plists total)
- Per-color release symlinks (`current-blue`, `current-green`)
- State files (`shared/api-active-color`, `shared/mcp-active-color`)
- Refactored `scripts/deploy-native.sh` with detect/bootstrap/probe/switch/drain/rollback flow
- First-time cutover helper script (idempotent, one-shot)
- Runbook + update to existing Hermes reference
- Unit tests on extracted shell helpers and HAProxy config rendering

**Explicit non-goals (do NOT touch in this PR):**

- worker / scheduler / kis-websocket / upbit-websocket plists or wrappers (stay single-active; only get brief restart during deploy as today)
- FastMCP session-aware proxy / session affinity (long-lived SSE will reconnect; acceptable per issue)
- Cloudflared config changes (still points at 8000/8765 — now HAProxy)
- Live broker/order/watch mutations
- DB schema downgrades (migrations remain expansion-only)
- gunicorn / multi-worker for MCP

---

## Production File Layout (target state)

```
/Users/mgh3326/services/auto_trader/
├── releases/<sha>/                        # full release checkout (unchanged)
├── current        -> releases/<sha>       # used by worker/scheduler/websockets
├── current-blue   -> releases/<sha-X>     # NEW: per-color release pointer for api/mcp
├── current-green  -> releases/<sha-Y>     # NEW
├── shared/
│   ├── .env.prod.native                   # existing
│   ├── api-active-color                   # NEW: file containing "blue" or "green"
│   ├── mcp-active-color                   # NEW: file containing "blue" or "green"
│   └── haproxy/
│       └── haproxy.cfg                    # NEW: generated, atomically swapped
├── plists/
│   ├── com.robinco.auto-trader.haproxy.plist        # NEW
│   ├── com.robinco.auto-trader.api-blue.plist       # NEW
│   ├── com.robinco.auto-trader.api-green.plist      # NEW
│   ├── com.robinco.auto-trader.mcp-blue.plist       # NEW
│   ├── com.robinco.auto-trader.mcp-green.plist      # NEW
│   ├── com.robinco.auto-trader.worker.plist         # unchanged
│   ├── com.robinco.auto-trader.scheduler.plist      # unchanged
│   ├── com.robinco.auto-trader.kis-websocket.plist  # unchanged
│   ├── com.robinco.auto-trader.upbit-websocket.plist# unchanged
│   └── com.robinco.auto-trader.api.plist            # REMOVED after cutover
│   └── com.robinco.auto-trader.mcp.plist            # REMOVED after cutover
├── scripts/                                # synced from release ops/native/scripts/ on deploy
│   ├── common.sh                           # existing
│   ├── healthcheck-native.sh               # updated to support per-color probes
│   ├── run-api.sh                          # rewritten color-aware
│   ├── run-mcp.sh                          # rewritten color-aware
│   ├── haproxy_render.sh                   # NEW
│   ├── haproxy_switch.sh                   # NEW
│   ├── native_bluegreen_lib.sh             # NEW (sourced helpers)
│   └── ...                                 # run-worker.sh etc unchanged
├── logs/
└── state/
```

## Port assignments

| Listener | Port | Owner |
|---|---|---|
| `127.0.0.1:8000` | FastAPI stable | HAProxy |
| `127.0.0.1:8001` | FastAPI blue | api-blue plist |
| `127.0.0.1:8002` | FastAPI green | api-green plist |
| `127.0.0.1:8765` | FastMCP stable | HAProxy |
| `127.0.0.1:8766` | FastMCP blue | mcp-blue plist |
| `127.0.0.1:8767` | FastMCP green | mcp-green plist |

## Repo File Layout (new files / modifications)

```
ops/                                  # NEW top-level dir
└── native/
    ├── haproxy/
    │   └── haproxy.cfg.tmpl          # NEW
    ├── plists/
    │   ├── com.robinco.auto-trader.haproxy.plist
    │   ├── com.robinco.auto-trader.api-blue.plist
    │   ├── com.robinco.auto-trader.api-green.plist
    │   ├── com.robinco.auto-trader.mcp-blue.plist
    │   └── com.robinco.auto-trader.mcp-green.plist
    └── scripts/
        ├── run-api.sh                # rewritten
        ├── run-mcp.sh                # rewritten
        ├── haproxy_render.sh         # NEW
        ├── haproxy_switch.sh         # NEW
        ├── native_bluegreen_lib.sh   # NEW
        └── healthcheck-native.sh     # updated (existing currently under services/, repo copy NEW)

scripts/
├── deploy-native.sh                  # MODIFIED (blue/green flow)
└── native_haproxy_first_cutover.sh   # NEW (one-shot migration)

tests/scripts/
├── test_native_bluegreen_lib.py      # NEW
├── test_haproxy_render.py            # NEW
├── test_haproxy_switch.py            # NEW
└── test_deploy_native_bluegreen.py   # NEW

docs/runbooks/
└── native-haproxy-blue-green.md      # NEW
```

---

## Pre-flight Checklist (for executor)

Before starting:

- [ ] Confirm working on branch `rob-259` (or feature branch off main)
- [ ] Confirm `git status` is clean
- [ ] Confirm `/Users/mgh3326/services/auto_trader/` exists with current production layout
- [ ] Confirm `~/.cloudflared/config.yml` currently points `trader.robinco.dev` → `127.0.0.1:8000` and `trader-mcp.robinco.dev` → `127.0.0.1:8765`
- [ ] HAProxy NOT yet installed (`brew list haproxy` returns nothing) — install happens as part of cutover, not as part of this code PR
- [ ] Tests use `uv run pytest` per project convention (CLAUDE.md)

---

## Task 1: Add HAProxy config template + render script

**Files:**

- Create: `ops/native/haproxy/haproxy.cfg.tmpl`
- Create: `ops/native/scripts/haproxy_render.sh`
- Test: `tests/scripts/test_haproxy_render.py`

- [ ] **Step 1: Write the failing test**

Create `tests/scripts/test_haproxy_render.py`:

```python
"""ROB-259 HAProxy config render tests."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
RENDER = REPO_ROOT / "ops" / "native" / "scripts" / "haproxy_render.sh"
TEMPLATE = REPO_ROOT / "ops" / "native" / "haproxy" / "haproxy.cfg.tmpl"


def _render(api_color: str, mcp_color: str, out_path: Path) -> subprocess.CompletedProcess:
    env = {**os.environ, "AUTO_TRADER_API_ACTIVE_COLOR": api_color, "AUTO_TRADER_MCP_ACTIVE_COLOR": mcp_color}
    return subprocess.run(
        ["bash", str(RENDER), str(TEMPLATE), str(out_path)],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )


def test_render_blue_blue(tmp_path: Path) -> None:
    out = tmp_path / "haproxy.cfg"
    proc = _render("blue", "blue", out)
    assert proc.returncode == 0, proc.stderr
    body = out.read_text()
    # Stable listeners always present
    assert "bind 127.0.0.1:8000" in body
    assert "bind 127.0.0.1:8765" in body
    # API blue active backend is :8001, green is backup
    assert "server api_blue 127.0.0.1:8001 check" in body
    assert "server api_green 127.0.0.1:8002 check backup" in body
    # MCP same
    assert "server mcp_blue 127.0.0.1:8766 check" in body
    assert "server mcp_green 127.0.0.1:8767 check backup" in body


def test_render_blue_green_mixed(tmp_path: Path) -> None:
    out = tmp_path / "haproxy.cfg"
    proc = _render("blue", "green", out)
    assert proc.returncode == 0, proc.stderr
    body = out.read_text()
    assert "server api_blue 127.0.0.1:8001 check\n" in body
    assert "server api_green 127.0.0.1:8002 check backup\n" in body
    assert "server mcp_green 127.0.0.1:8767 check\n" in body
    assert "server mcp_blue 127.0.0.1:8766 check backup\n" in body


def test_render_rejects_invalid_color(tmp_path: Path) -> None:
    out = tmp_path / "haproxy.cfg"
    proc = _render("purple", "blue", out)
    assert proc.returncode != 0
    assert "invalid color" in proc.stderr.lower()


@pytest.mark.skipif(shutil.which("haproxy") is None, reason="haproxy not installed")
def test_rendered_config_validates(tmp_path: Path) -> None:
    out = tmp_path / "haproxy.cfg"
    _render("blue", "blue", out)
    proc = subprocess.run(
        ["haproxy", "-c", "-f", str(out)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
```

- [ ] **Step 2: Run test, verify failure**

```
uv run pytest tests/scripts/test_haproxy_render.py -v
```

Expected: 3 failures (file-not-found), 1 skip.

- [ ] **Step 3: Create the HAProxy template**

Create `ops/native/haproxy/haproxy.cfg.tmpl`:

```
global
    log /dev/log local0 info
    maxconn 4096
    daemon
    master-worker

defaults
    log     global
    mode    http
    option  httplog
    option  dontlognull
    option  http-server-close
    timeout connect 5s
    timeout client  60s
    timeout server  60s
    timeout http-request 10s
    timeout queue 30s
    retries 3
    option  redispatch

resolvers local
    nameserver dns1 127.0.0.1:53
    accepted_payload_size 8192

frontend ft_api
    bind 127.0.0.1:8000
    default_backend bk_api

frontend ft_mcp
    bind 127.0.0.1:8765
    timeout client 1h
    default_backend bk_mcp

backend bk_api
    option httpchk GET /healthz
    http-check expect status 200
    default-server inter 2s fall 2 rise 1
    {{API_PRIMARY_LINE}}
    {{API_BACKUP_LINE}}

backend bk_mcp
    option httpchk GET /mcp
    http-check expect status 400,401
    timeout server 1h
    timeout tunnel 1h
    default-server inter 5s fall 2 rise 1
    {{MCP_PRIMARY_LINE}}
    {{MCP_BACKUP_LINE}}
```

- [ ] **Step 4: Create the render script**

Create `ops/native/scripts/haproxy_render.sh`:

```bash
#!/usr/bin/env bash
# ROB-259: render HAProxy config from template with active color env vars.
# Usage: haproxy_render.sh <template> <out>
# Env:
#   AUTO_TRADER_API_ACTIVE_COLOR = blue | green
#   AUTO_TRADER_MCP_ACTIVE_COLOR = blue | green
set -Eeuo pipefail

TEMPLATE="${1:?template path required}"
OUT="${2:?output path required}"
API_COLOR="${AUTO_TRADER_API_ACTIVE_COLOR:?AUTO_TRADER_API_ACTIVE_COLOR required}"
MCP_COLOR="${AUTO_TRADER_MCP_ACTIVE_COLOR:?AUTO_TRADER_MCP_ACTIVE_COLOR required}"

validate_color() {
  local kind="$1" color="$2"
  if [[ "$color" != "blue" && "$color" != "green" ]]; then
    echo "invalid color for $kind: $color (expected blue|green)" >&2
    exit 64
  fi
}

validate_color api "$API_COLOR"
validate_color mcp "$MCP_COLOR"

api_primary_line() {
  if [[ "$API_COLOR" == "blue" ]]; then
    echo "server api_blue 127.0.0.1:8001 check"
  else
    echo "server api_green 127.0.0.1:8002 check"
  fi
}

api_backup_line() {
  if [[ "$API_COLOR" == "blue" ]]; then
    echo "server api_green 127.0.0.1:8002 check backup"
  else
    echo "server api_blue 127.0.0.1:8001 check backup"
  fi
}

mcp_primary_line() {
  if [[ "$MCP_COLOR" == "blue" ]]; then
    echo "server mcp_blue 127.0.0.1:8766 check"
  else
    echo "server mcp_green 127.0.0.1:8767 check"
  fi
}

mcp_backup_line() {
  if [[ "$MCP_COLOR" == "blue" ]]; then
    echo "server mcp_green 127.0.0.1:8767 check backup"
  else
    echo "server mcp_blue 127.0.0.1:8766 check backup"
  fi
}

TMP="$(mktemp -t haproxy-render.XXXXXX)"
trap 'rm -f "$TMP"' EXIT

sed \
  -e "s|{{API_PRIMARY_LINE}}|$(api_primary_line)|g" \
  -e "s|{{API_BACKUP_LINE}}|$(api_backup_line)|g" \
  -e "s|{{MCP_PRIMARY_LINE}}|$(mcp_primary_line)|g" \
  -e "s|{{MCP_BACKUP_LINE}}|$(mcp_backup_line)|g" \
  "$TEMPLATE" >"$TMP"

mv "$TMP" "$OUT"
```

Then `chmod +x ops/native/scripts/haproxy_render.sh`.

- [ ] **Step 5: Run tests, verify pass**

```
chmod +x ops/native/scripts/haproxy_render.sh
uv run pytest tests/scripts/test_haproxy_render.py -v
```

Expected: 3 PASS, 1 skip (validate-with-haproxy is skipped because haproxy not installed locally — that's fine; it will run on the prod Mac after install).

- [ ] **Step 6: Commit**

```
git add ops/native/haproxy/ ops/native/scripts/haproxy_render.sh tests/scripts/test_haproxy_render.py
git commit -m "feat(ops): add HAProxy config template + render script (ROB-259)"
```

---

## Task 2: Add native_bluegreen_lib.sh helper + tests

**Files:**

- Create: `ops/native/scripts/native_bluegreen_lib.sh`
- Test: `tests/scripts/test_native_bluegreen_lib.py`

This file holds the small color-detection/switch helpers used by both deploy-native.sh and the first-time cutover script.

- [ ] **Step 1: Write the failing test**

Create `tests/scripts/test_native_bluegreen_lib.py`:

```python
"""ROB-259 color detection/switch helpers."""

from __future__ import annotations

import subprocess
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
LIB = REPO_ROOT / "ops" / "native" / "scripts" / "native_bluegreen_lib.sh"


def _bash(snippet: str, base: Path) -> subprocess.CompletedProcess:
    """Source the lib then evaluate snippet with AUTO_TRADER_BASE pointing at base."""
    script = f'set -Eeuo pipefail\nexport AUTO_TRADER_BASE="{base}"\nsource "{LIB}"\n{snippet}\n'
    return subprocess.run(["bash", "-c", script], check=False, capture_output=True, text=True)


def test_detect_active_color_defaults_blue(tmp_path: Path) -> None:
    (tmp_path / "shared").mkdir()
    proc = _bash('detect_active_color api', tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "blue"


def test_detect_active_color_reads_file(tmp_path: Path) -> None:
    shared = tmp_path / "shared"
    shared.mkdir()
    (shared / "api-active-color").write_text("green\n")
    proc = _bash('detect_active_color api', tmp_path)
    assert proc.stdout.strip() == "green"


def test_detect_active_color_rejects_garbage(tmp_path: Path) -> None:
    shared = tmp_path / "shared"
    shared.mkdir()
    (shared / "api-active-color").write_text("purple\n")
    proc = _bash('detect_active_color api', tmp_path)
    assert proc.returncode != 0
    assert "invalid" in proc.stderr.lower()


def test_inactive_color_inverts(tmp_path: Path) -> None:
    proc = _bash('inactive_color blue && inactive_color green', tmp_path)
    assert proc.returncode == 0
    lines = proc.stdout.strip().splitlines()
    assert lines == ["green", "blue"]


def test_set_active_color_atomic(tmp_path: Path) -> None:
    (tmp_path / "shared").mkdir()
    proc = _bash('set_active_color api green', tmp_path)
    assert proc.returncode == 0
    assert (tmp_path / "shared" / "api-active-color").read_text().strip() == "green"


def test_color_port_lookup(tmp_path: Path) -> None:
    proc = _bash(
        'color_port api blue; color_port api green; color_port mcp blue; color_port mcp green',
        tmp_path,
    )
    assert proc.returncode == 0
    assert proc.stdout.strip().splitlines() == ["8001", "8002", "8766", "8767"]


def test_color_port_invalid_service(tmp_path: Path) -> None:
    proc = _bash('color_port worker blue', tmp_path)
    assert proc.returncode != 0


def test_color_label(tmp_path: Path) -> None:
    proc = _bash('color_label api blue; color_label mcp green', tmp_path)
    assert proc.returncode == 0
    assert proc.stdout.strip().splitlines() == [
        "com.robinco.auto-trader.api-blue",
        "com.robinco.auto-trader.mcp-green",
    ]
```

- [ ] **Step 2: Run test, verify failure**

```
uv run pytest tests/scripts/test_native_bluegreen_lib.py -v
```

Expected: all fail (lib doesn't exist).

- [ ] **Step 3: Implement the library**

Create `ops/native/scripts/native_bluegreen_lib.sh`:

```bash
#!/usr/bin/env bash
# ROB-259: shared helpers for blue/green color detection + state file writes.
# Source-only; do not execute directly.
#
# Required env:
#   AUTO_TRADER_BASE  - production base dir (e.g. /Users/mgh3326/services/auto_trader)

if [[ -z "${AUTO_TRADER_BASE:-}" ]]; then
  echo "native_bluegreen_lib: AUTO_TRADER_BASE not set" >&2
  return 1 2>/dev/null || exit 1
fi

_bg_shared_dir() {
  echo "$AUTO_TRADER_BASE/shared"
}

_bg_color_file() {
  local service="$1"
  echo "$(_bg_shared_dir)/${service}-active-color"
}

_bg_validate_color() {
  local color="$1"
  if [[ "$color" != "blue" && "$color" != "green" ]]; then
    echo "invalid color: $color (expected blue|green)" >&2
    return 64
  fi
}

_bg_validate_service() {
  local service="$1"
  if [[ "$service" != "api" && "$service" != "mcp" ]]; then
    echo "invalid service: $service (expected api|mcp)" >&2
    return 64
  fi
}

# detect_active_color <service>
# Prints "blue" or "green". Defaults to "blue" if the state file is missing.
detect_active_color() {
  local service="$1"
  _bg_validate_service "$service"
  local file
  file="$(_bg_color_file "$service")"
  if [[ -f "$file" ]]; then
    local raw
    raw="$(tr -d '[:space:]' <"$file")"
    _bg_validate_color "$raw"
    echo "$raw"
  else
    echo "blue"
  fi
}

# inactive_color <color>
# Echo the opposite color.
inactive_color() {
  local color="$1"
  _bg_validate_color "$color"
  if [[ "$color" == "blue" ]]; then
    echo "green"
  else
    echo "blue"
  fi
}

# set_active_color <service> <color>
# Atomically replace the state file.
set_active_color() {
  local service="$1" color="$2"
  _bg_validate_service "$service"
  _bg_validate_color "$color"
  local file tmp
  file="$(_bg_color_file "$service")"
  mkdir -p "$(dirname "$file")"
  tmp="$(mktemp "${file}.XXXXXX")"
  echo "$color" >"$tmp"
  mv "$tmp" "$file"
}

# color_port <service> <color>
color_port() {
  local service="$1" color="$2"
  _bg_validate_service "$service"
  _bg_validate_color "$color"
  case "${service}_${color}" in
    api_blue)   echo 8001 ;;
    api_green)  echo 8002 ;;
    mcp_blue)   echo 8766 ;;
    mcp_green)  echo 8767 ;;
  esac
}

# color_label <service> <color>
color_label() {
  local service="$1" color="$2"
  _bg_validate_service "$service"
  _bg_validate_color "$color"
  echo "com.robinco.auto-trader.${service}-${color}"
}

# color_current_symlink <color>
# Path to the per-color "current" symlink consumed by run-api.sh/run-mcp.sh.
color_current_symlink() {
  local color="$1"
  _bg_validate_color "$color"
  echo "$AUTO_TRADER_BASE/current-${color}"
}
```

- [ ] **Step 4: Run tests, verify pass**

```
uv run pytest tests/scripts/test_native_bluegreen_lib.py -v
```

Expected: 8 PASS.

- [ ] **Step 5: Commit**

```
git add ops/native/scripts/native_bluegreen_lib.sh tests/scripts/test_native_bluegreen_lib.py
git commit -m "feat(ops): add native blue/green helper lib (ROB-259)"
```

---

## Task 3: Add HAProxy switch script + tests

**Files:**

- Create: `ops/native/scripts/haproxy_switch.sh`
- Test: `tests/scripts/test_haproxy_switch.py`

The switch script: render config to a temp file, run `haproxy -c -f` validation, atomically move into the live path, send SIGUSR2 to the master process (via launchctl) to trigger zero-downtime reload. Validation failure leaves the live config untouched.

- [ ] **Step 1: Write failing test**

Create `tests/scripts/test_haproxy_switch.py`:

```python
"""ROB-259 haproxy_switch tests."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SWITCH = REPO_ROOT / "ops" / "native" / "scripts" / "haproxy_switch.sh"
TEMPLATE = REPO_ROOT / "ops" / "native" / "haproxy" / "haproxy.cfg.tmpl"


def _run(env_extra: dict[str, str], base: Path) -> subprocess.CompletedProcess:
    env = {
        **os.environ,
        "AUTO_TRADER_BASE": str(base),
        "AUTO_TRADER_HAPROXY_TEMPLATE": str(TEMPLATE),
        # Default: skip the actual launchctl reload so the test doesn't need root/launchd.
        "AUTO_TRADER_HAPROXY_RELOAD": "skip",
        **env_extra,
    }
    return subprocess.run(["bash", str(SWITCH)], check=False, capture_output=True, text=True, env=env)


def _setup_base(tmp_path: Path) -> Path:
    (tmp_path / "shared" / "haproxy").mkdir(parents=True)
    (tmp_path / "shared").joinpath("api-active-color").write_text("blue\n")
    (tmp_path / "shared").joinpath("mcp-active-color").write_text("blue\n")
    return tmp_path


def test_switch_writes_live_config(tmp_path: Path) -> None:
    base = _setup_base(tmp_path)
    proc = _run({}, base)
    assert proc.returncode == 0, proc.stderr
    live = base / "shared" / "haproxy" / "haproxy.cfg"
    body = live.read_text()
    assert "bind 127.0.0.1:8000" in body
    assert "server api_blue 127.0.0.1:8001 check\n" in body


def test_switch_atomically_replaces_existing(tmp_path: Path) -> None:
    base = _setup_base(tmp_path)
    live = base / "shared" / "haproxy" / "haproxy.cfg"
    live.write_text("stale\n")
    stat_before = live.stat()
    proc = _run({}, base)
    assert proc.returncode == 0, proc.stderr
    stat_after = live.stat()
    # mv-based replace must change the inode
    assert stat_before.st_ino != stat_after.st_ino
    assert "bind 127.0.0.1:8000" in live.read_text()


def test_switch_rolls_back_on_validation_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    base = _setup_base(tmp_path)
    live = base / "shared" / "haproxy" / "haproxy.cfg"
    live.write_text("KEEP-ME\n")
    # Force the renderer to error out by passing an invalid color.
    (base / "shared" / "api-active-color").write_text("purple\n")
    proc = _run({}, base)
    assert proc.returncode != 0
    assert live.read_text() == "KEEP-ME\n"
```

- [ ] **Step 2: Run test, verify failure**

```
uv run pytest tests/scripts/test_haproxy_switch.py -v
```

Expected: 3 fail (script doesn't exist).

- [ ] **Step 3: Implement the switch script**

Create `ops/native/scripts/haproxy_switch.sh`:

```bash
#!/usr/bin/env bash
# ROB-259: validate and atomically swap HAProxy config, then reload.
#
# Env:
#   AUTO_TRADER_BASE                  (required)
#   AUTO_TRADER_HAPROXY_TEMPLATE      defaults to <base>/scripts/haproxy/haproxy.cfg.tmpl
#                                     (deploy syncs this from release ops/native/haproxy/)
#   AUTO_TRADER_HAPROXY_LIVE          defaults to <base>/shared/haproxy/haproxy.cfg
#   AUTO_TRADER_HAPROXY_RELOAD        "launchctl" (default on prod), "skip" (tests)
#   AUTO_TRADER_HAPROXY_LABEL         defaults to com.robinco.auto-trader.haproxy
set -Eeuo pipefail

BASE="${AUTO_TRADER_BASE:?AUTO_TRADER_BASE required}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# shellcheck source=native_bluegreen_lib.sh
source "$SCRIPT_DIR/native_bluegreen_lib.sh"

DEFAULT_TEMPLATE="$BASE/scripts/haproxy/haproxy.cfg.tmpl"
TEMPLATE="${AUTO_TRADER_HAPROXY_TEMPLATE:-$DEFAULT_TEMPLATE}"
LIVE="${AUTO_TRADER_HAPROXY_LIVE:-$BASE/shared/haproxy/haproxy.cfg}"
RELOAD_MODE="${AUTO_TRADER_HAPROXY_RELOAD:-launchctl}"
LABEL="${AUTO_TRADER_HAPROXY_LABEL:-com.robinco.auto-trader.haproxy}"

API_COLOR="$(detect_active_color api)"
MCP_COLOR="$(detect_active_color mcp)"

TMP_OUT="$(mktemp -t haproxy-cfg.XXXXXX)"
trap 'rm -f "$TMP_OUT"' EXIT

AUTO_TRADER_API_ACTIVE_COLOR="$API_COLOR" \
AUTO_TRADER_MCP_ACTIVE_COLOR="$MCP_COLOR" \
  bash "$SCRIPT_DIR/haproxy_render.sh" "$TEMPLATE" "$TMP_OUT"

# Validate only when haproxy is installed. Tests usually don't have it.
if command -v haproxy >/dev/null 2>&1; then
  haproxy -c -f "$TMP_OUT" >/dev/null
fi

mkdir -p "$(dirname "$LIVE")"
mv "$TMP_OUT" "$LIVE"
trap - EXIT

case "$RELOAD_MODE" in
  skip)
    ;;
  launchctl)
    uid_num="$(id -u)"
    # SIGUSR2 to master triggers seamless reload when master-worker is in haproxy.cfg.
    launchctl kill SIGUSR2 "gui/$uid_num/$LABEL"
    ;;
  *)
    echo "unknown AUTO_TRADER_HAPROXY_RELOAD: $RELOAD_MODE" >&2
    exit 64
    ;;
esac

echo "haproxy switched: api=$API_COLOR mcp=$MCP_COLOR live=$LIVE"
```

- [ ] **Step 4: Run tests, verify pass**

```
chmod +x ops/native/scripts/haproxy_switch.sh
uv run pytest tests/scripts/test_haproxy_switch.py -v
```

Expected: 3 PASS.

- [ ] **Step 5: Commit**

```
git add ops/native/scripts/haproxy_switch.sh tests/scripts/test_haproxy_switch.py
git commit -m "feat(ops): add haproxy_switch with atomic validate+reload (ROB-259)"
```

---

## Task 4: Add HAProxy launchd plist

**Files:**

- Create: `ops/native/plists/com.robinco.auto-trader.haproxy.plist`

Plain XML file; validated via `plutil -lint` in a tiny test.

- [ ] **Step 1: Write failing test**

Append to `tests/scripts/test_haproxy_render.py` (or new `tests/scripts/test_native_plists.py`):

Create `tests/scripts/test_native_plists.py`:

```python
"""ROB-259 plist lint tests."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
PLIST_DIR = REPO_ROOT / "ops" / "native" / "plists"

PLISTS = [
    "com.robinco.auto-trader.haproxy.plist",
    "com.robinco.auto-trader.api-blue.plist",
    "com.robinco.auto-trader.api-green.plist",
    "com.robinco.auto-trader.mcp-blue.plist",
    "com.robinco.auto-trader.mcp-green.plist",
]


@pytest.mark.parametrize("name", PLISTS)
def test_plist_exists(name: str) -> None:
    assert (PLIST_DIR / name).is_file(), f"missing {name}"


@pytest.mark.skipif(shutil.which("plutil") is None, reason="plutil not available")
@pytest.mark.parametrize("name", PLISTS)
def test_plist_lints(name: str) -> None:
    proc = subprocess.run(["plutil", "-lint", str(PLIST_DIR / name)], check=False, capture_output=True, text=True)
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_haproxy_plist_label() -> None:
    body = (PLIST_DIR / "com.robinco.auto-trader.haproxy.plist").read_text()
    assert "<string>com.robinco.auto-trader.haproxy</string>" in body
    assert "haproxy" in body  # ProgramArguments references haproxy binary
    assert "shared/haproxy/haproxy.cfg" in body


def test_api_blue_plist_port() -> None:
    body = (PLIST_DIR / "com.robinco.auto-trader.api-blue.plist").read_text()
    assert "AUTO_TRADER_COLOR" in body and "<string>blue</string>" in body
    assert "AUTO_TRADER_API_PORT" in body and "<string>8001</string>" in body
    assert "current-blue" in body  # WorkingDirectory


def test_api_green_plist_port() -> None:
    body = (PLIST_DIR / "com.robinco.auto-trader.api-green.plist").read_text()
    assert "<string>green</string>" in body
    assert "<string>8002</string>" in body
    assert "current-green" in body


def test_mcp_blue_plist_port() -> None:
    body = (PLIST_DIR / "com.robinco.auto-trader.mcp-blue.plist").read_text()
    assert "<string>blue</string>" in body
    assert "<string>8766</string>" in body
    assert "current-blue" in body


def test_mcp_green_plist_port() -> None:
    body = (PLIST_DIR / "com.robinco.auto-trader.mcp-green.plist").read_text()
    assert "<string>green</string>" in body
    assert "<string>8767</string>" in body
    assert "current-green" in body
```

- [ ] **Step 2: Run test, verify failure**

```
uv run pytest tests/scripts/test_native_plists.py -v
```

Expected: all fail.

- [ ] **Step 3: Create the HAProxy plist**

Create `ops/native/plists/com.robinco.auto-trader.haproxy.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.robinco.auto-trader.haproxy</string>
  <key>ProgramArguments</key>
  <array>
    <string>/opt/homebrew/bin/haproxy</string>
    <string>-W</string>
    <string>-f</string>
    <string>/Users/mgh3326/services/auto_trader/shared/haproxy/haproxy.cfg</string>
  </array>
  <key>WorkingDirectory</key>
  <string>/Users/mgh3326/services/auto_trader/shared/haproxy</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>HOME</key>
    <string>/Users/mgh3326</string>
    <key>PATH</key>
    <string>/Users/mgh3326/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
  </dict>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>ThrottleInterval</key>
  <integer>10</integer>
  <key>StandardOutPath</key>
  <string>/Users/mgh3326/services/auto_trader/logs/com.robinco.auto-trader.haproxy.out.log</string>
  <key>StandardErrorPath</key>
  <string>/Users/mgh3326/services/auto_trader/logs/com.robinco.auto-trader.haproxy.err.log</string>
</dict>
</plist>
```

(Note: `-W` plus `master-worker` in cfg lets haproxy run under launchd while still supporting SIGUSR2 seamless reload of workers.)

- [ ] **Step 4: Commit**

```
git add ops/native/plists/com.robinco.auto-trader.haproxy.plist tests/scripts/test_native_plists.py
git commit -m "feat(ops): add HAProxy launchd plist (ROB-259)"
```

(The 5 parameterized test cases still fail because color plists haven't been added yet — fix in Task 5.)

---

## Task 5: Add color-specific FastAPI + FastMCP plists

**Files:**

- Create: `ops/native/plists/com.robinco.auto-trader.api-blue.plist`
- Create: `ops/native/plists/com.robinco.auto-trader.api-green.plist`
- Create: `ops/native/plists/com.robinco.auto-trader.mcp-blue.plist`
- Create: `ops/native/plists/com.robinco.auto-trader.mcp-green.plist`

These differ from the existing api/mcp plists in three ways:

1. Label suffix `-blue` / `-green`
2. `WorkingDirectory` is `current-blue` / `current-green` (not `current`)
3. New env vars `AUTO_TRADER_COLOR`, `AUTO_TRADER_API_PORT` / `AUTO_TRADER_MCP_PORT`

- [ ] **Step 1: Create `com.robinco.auto-trader.api-blue.plist`**

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.robinco.auto-trader.api-blue</string>
  <key>ProgramArguments</key>
  <array>
    <string>/Users/mgh3326/services/auto_trader/scripts/run-api.sh</string>
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
    <key>AUTO_TRADER_COLOR</key>
    <string>blue</string>
    <key>AUTO_TRADER_API_PORT</key>
    <string>8001</string>
  </dict>
  <key>RunAtLoad</key>
  <false/>
  <key>KeepAlive</key>
  <true/>
  <key>ThrottleInterval</key>
  <integer>10</integer>
  <key>StandardOutPath</key>
  <string>/Users/mgh3326/services/auto_trader/logs/com.robinco.auto-trader.api-blue.out.log</string>
  <key>StandardErrorPath</key>
  <string>/Users/mgh3326/services/auto_trader/logs/com.robinco.auto-trader.api-blue.err.log</string>
</dict>
</plist>
```

Note `RunAtLoad=false`: deploy decides when to bootstrap which color. The currently-active color is left bootstrapped between deploys; the inactive one stays bootout.

- [ ] **Step 2: Create `com.robinco.auto-trader.api-green.plist`**

Identical to api-blue plist, with these field replacements:

- `<string>com.robinco.auto-trader.api-blue</string>` → `<string>com.robinco.auto-trader.api-green</string>`
- `current-blue` → `current-green`
- `AUTO_TRADER_COLOR` value `blue` → `green`
- `AUTO_TRADER_API_PORT` value `8001` → `8002`
- log paths `api-blue` → `api-green`

- [ ] **Step 3: Create `com.robinco.auto-trader.mcp-blue.plist`**

Same shape as api-blue plist but for MCP. Differences from api-blue:

- Label `com.robinco.auto-trader.mcp-blue`
- `ProgramArguments` → `run-mcp.sh` (not `run-api.sh`)
- Env vars: `AUTO_TRADER_COLOR=blue`, `AUTO_TRADER_MCP_PORT=8766` (no API_PORT)
- Adds `SoftResourceLimits` and `HardResourceLimits` `NumberOfFiles=4096` blocks (mirror existing mcp plist behavior)
- log paths `mcp-blue`

Full file:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.robinco.auto-trader.mcp-blue</string>
  <key>ProgramArguments</key>
  <array>
    <string>/Users/mgh3326/services/auto_trader/scripts/run-mcp.sh</string>
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
    <key>AUTO_TRADER_COLOR</key>
    <string>blue</string>
    <key>AUTO_TRADER_MCP_PORT</key>
    <string>8766</string>
  </dict>
  <key>RunAtLoad</key>
  <false/>
  <key>KeepAlive</key>
  <true/>
  <key>ThrottleInterval</key>
  <integer>10</integer>
  <key>SoftResourceLimits</key>
  <dict>
    <key>NumberOfFiles</key>
    <integer>4096</integer>
  </dict>
  <key>HardResourceLimits</key>
  <dict>
    <key>NumberOfFiles</key>
    <integer>4096</integer>
  </dict>
  <key>StandardOutPath</key>
  <string>/Users/mgh3326/services/auto_trader/logs/com.robinco.auto-trader.mcp-blue.out.log</string>
  <key>StandardErrorPath</key>
  <string>/Users/mgh3326/services/auto_trader/logs/com.robinco.auto-trader.mcp-blue.err.log</string>
</dict>
</plist>
```

- [ ] **Step 4: Create `com.robinco.auto-trader.mcp-green.plist`**

Same as mcp-blue with: label `mcp-green`, `current-green`, `AUTO_TRADER_COLOR=green`, `AUTO_TRADER_MCP_PORT=8767`, log paths `mcp-green`.

- [ ] **Step 5: Run plist tests, verify pass**

```
uv run pytest tests/scripts/test_native_plists.py -v
```

Expected: all PASS (5 existence + 5 lint + 5 content = 15 total).

- [ ] **Step 6: Commit**

```
git add ops/native/plists/com.robinco.auto-trader.api-blue.plist ops/native/plists/com.robinco.auto-trader.api-green.plist ops/native/plists/com.robinco.auto-trader.mcp-blue.plist ops/native/plists/com.robinco.auto-trader.mcp-green.plist
git commit -m "feat(ops): add color-specific api/mcp launchd plists (ROB-259)"
```

---

## Task 6: Rewrite run-api.sh and run-mcp.sh color-aware

**Files:**

- Create: `ops/native/scripts/run-api.sh`
- Create: `ops/native/scripts/run-mcp.sh`
- Test: `tests/scripts/test_native_run_wrappers.py`

The new wrappers honor `AUTO_TRADER_COLOR` and `AUTO_TRADER_{API,MCP}_PORT` with sensible defaults that match the existing behavior (color=blue, api=8000, mcp=8765) so they're backward-compatible if accidentally invoked outside launchd. Backward-compat defaults exist only so a developer running the script directly doesn't get an error; on prod the plists always set the env vars.

- [ ] **Step 1: Write failing test**

Create `tests/scripts/test_native_run_wrappers.py`:

```python
"""ROB-259 run-api / run-mcp wrapper smoke tests (no actual server start)."""

from __future__ import annotations

import os
import subprocess
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
RUN_API = REPO_ROOT / "ops" / "native" / "scripts" / "run-api.sh"
RUN_MCP = REPO_ROOT / "ops" / "native" / "scripts" / "run-mcp.sh"


def _run_with_uv_stub(script: Path, color: str, port: str | None, env_var: str, tmp_path: Path) -> subprocess.CompletedProcess:
    """Run wrapper with a fake `uv` on PATH that just echoes its argv and exits 0."""
    stub_dir = tmp_path / "bin"
    stub_dir.mkdir()
    stub = stub_dir / "uv"
    stub.write_text("#!/usr/bin/env bash\nprintf '%s\\n' \"$@\"\n")
    stub.chmod(0o755)
    # Provide a minimal common.sh shim so the wrapper sources cleanly.
    services_scripts = tmp_path / "services" / "auto_trader" / "scripts"
    services_scripts.mkdir(parents=True)
    (services_scripts / "common.sh").write_text("# noop common.sh stub\n")
    env = {
        "PATH": f"{stub_dir}:{os.environ['PATH']}",
        "HOME": str(tmp_path),
        "AUTO_TRADER_BASE": str(tmp_path / "services" / "auto_trader"),
        "AUTO_TRADER_COLOR": color,
    }
    if port is not None:
        env[env_var] = port
    return subprocess.run(["bash", str(script)], check=False, capture_output=True, text=True, env=env)


def test_run_api_uses_color_port(tmp_path: Path) -> None:
    proc = _run_with_uv_stub(RUN_API, "blue", "8001", "AUTO_TRADER_API_PORT", tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert "--port" in proc.stdout
    assert "8001" in proc.stdout


def test_run_api_default_port_blue(tmp_path: Path) -> None:
    proc = _run_with_uv_stub(RUN_API, "blue", None, "AUTO_TRADER_API_PORT", tmp_path)
    assert proc.returncode == 0
    assert "8001" in proc.stdout


def test_run_api_default_port_green(tmp_path: Path) -> None:
    proc = _run_with_uv_stub(RUN_API, "green", None, "AUTO_TRADER_API_PORT", tmp_path)
    assert proc.returncode == 0
    assert "8002" in proc.stdout


def test_run_mcp_uses_color_port(tmp_path: Path) -> None:
    proc = _run_with_uv_stub(RUN_MCP, "blue", "8766", "AUTO_TRADER_MCP_PORT", tmp_path)
    assert proc.returncode == 0, proc.stderr
    # Wrapper exports MCP_HTTP_PORT before exec'ing python; the stub captures argv only,
    # so we assert the env-export side effect via a separate path:
    assert "app.mcp_server.main" in proc.stdout or "python" in proc.stdout


def test_run_mcp_default_port_green(tmp_path: Path) -> None:
    proc = _run_with_uv_stub(RUN_MCP, "green", None, "AUTO_TRADER_MCP_PORT", tmp_path)
    assert proc.returncode == 0
```

- [ ] **Step 2: Run test, verify failure**

```
uv run pytest tests/scripts/test_native_run_wrappers.py -v
```

Expected: all 5 fail.

- [ ] **Step 3: Implement run-api.sh**

Create `ops/native/scripts/run-api.sh`:

```bash
#!/bin/zsh
# ROB-259: color-aware FastAPI launcher.
set -euo pipefail

source "${HOME:-/Users/mgh3326}/services/auto_trader/scripts/common.sh"

COLOR="${AUTO_TRADER_COLOR:-blue}"
case "$COLOR" in
  blue)  DEFAULT_PORT=8001 ;;
  green) DEFAULT_PORT=8002 ;;
  *)
    echo "run-api.sh: invalid AUTO_TRADER_COLOR=$COLOR" >&2
    exit 64
    ;;
esac

PORT="${AUTO_TRADER_API_PORT:-$DEFAULT_PORT}"

exec uv run python -m uvicorn app.main:api --host 127.0.0.1 --port "$PORT"
```

- [ ] **Step 4: Implement run-mcp.sh**

Create `ops/native/scripts/run-mcp.sh`:

```bash
#!/bin/zsh
# ROB-259: color-aware FastMCP launcher.
set -euo pipefail

source "${HOME:-/Users/mgh3326}/services/auto_trader/scripts/common.sh"
_export_selected_env_prefixes MCP_

COLOR="${AUTO_TRADER_COLOR:-blue}"
case "$COLOR" in
  blue)  DEFAULT_PORT=8766 ;;
  green) DEFAULT_PORT=8767 ;;
  *)
    echo "run-mcp.sh: invalid AUTO_TRADER_COLOR=$COLOR" >&2
    exit 64
    ;;
esac

PORT="${AUTO_TRADER_MCP_PORT:-$DEFAULT_PORT}"
# FastMCP picks up MCP_HTTP_PORT via app config; export so the server binds the per-color port.
export MCP_HTTP_PORT="$PORT"

exec uv run python -m app.mcp_server.main
```

- [ ] **Step 5: chmod + run tests**

```
chmod +x ops/native/scripts/run-api.sh ops/native/scripts/run-mcp.sh
uv run pytest tests/scripts/test_native_run_wrappers.py -v
```

Expected: all PASS.

- [ ] **Step 6: Verify FastMCP actually reads `MCP_HTTP_PORT`**

Look at `app/mcp_server/main.py` (or wherever the FastMCP HTTP server binds) and confirm the port env var name. If it's different (e.g. `FASTMCP_PORT`, hardcoded `8765`), do one of:

- (a) Add a settings override that prefers `MCP_HTTP_PORT` over the default. Add a test confirming the override path.
- (b) Update the env var name used by `run-mcp.sh` to match.

Document the chosen approach with a one-line comment in `run-mcp.sh`.

- [ ] **Step 7: Commit**

```
git add ops/native/scripts/run-api.sh ops/native/scripts/run-mcp.sh tests/scripts/test_native_run_wrappers.py
# include any app/mcp_server change from step 6
git commit -m "feat(ops): color-aware run-api/run-mcp wrappers (ROB-259)"
```

---

## Task 7: Update healthcheck to support per-color probes

**Files:**

- Modify: existing `scripts/healthcheck-native.sh`-equivalent OR create new `ops/native/scripts/healthcheck-native.sh`

The existing healthcheck lives in `$AUTO_TRADER_BASE/scripts/healthcheck-native.sh` (server-side). We add a repo-managed copy that the deploy syncs in, and extend it with two new modes:

- Default: probe HAProxy stable ports (8000/8765) — the public-facing path
- `--direct blue` / `--direct green`: probe the named color's app ports directly, bypassing HAProxy

- [ ] **Step 1: Write failing test**

Create `tests/scripts/test_healthcheck_native.py`:

```python
"""ROB-259 healthcheck-native --direct mode."""

from __future__ import annotations

import os
import subprocess
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
HC = REPO_ROOT / "ops" / "native" / "scripts" / "healthcheck-native.sh"


def _curl_stub_dir(tmp_path: Path, port_map: dict[int, tuple[int, str]]) -> Path:
    """Build a curl shim that returns scripted (status, body) per port."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub = bin_dir / "curl"
    cases = "\n".join(
        f'    *127.0.0.1:{port}*) echo "{body}"; exit {0 if status == 200 or status == 401 else 22};;'
        for port, (status, body) in port_map.items()
    )
    stub.write_text(textwrap.dedent(f"""\
        #!/usr/bin/env bash
        # Echo body + exit with 0 for accepted statuses; -fsS would have exited non-zero otherwise.
        case "$*" in
{cases}
            *) echo "no stub for $*" >&2; exit 7 ;;
        esac
        """))
    stub.chmod(0o755)
    return bin_dir


def test_direct_blue_probes_8001_and_8766(tmp_path: Path) -> None:
    bin_dir = _curl_stub_dir(tmp_path, {8001: (200, "ok"), 8766: (401, "")})
    services_scripts = tmp_path / "services" / "auto_trader" / "scripts"
    services_scripts.mkdir(parents=True)
    (services_scripts / "common.sh").write_text("# noop\n")
    env = {
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "HOME": str(tmp_path),
        "AUTO_TRADER_BASE": str(tmp_path / "services" / "auto_trader"),
        # Skip websocket heartbeat check in tests:
        "AUTO_TRADER_HEALTHCHECK_SKIP_WS": "1",
    }
    proc = subprocess.run(["bash", str(HC), "--direct", "blue"], check=False, capture_output=True, text=True, env=env)
    assert proc.returncode == 0, proc.stderr + proc.stdout


def test_default_mode_probes_stable_ports(tmp_path: Path) -> None:
    bin_dir = _curl_stub_dir(tmp_path, {8000: (200, "ok"), 8765: (401, "")})
    services_scripts = tmp_path / "services" / "auto_trader" / "scripts"
    services_scripts.mkdir(parents=True)
    (services_scripts / "common.sh").write_text("# noop\n")
    env = {
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "HOME": str(tmp_path),
        "AUTO_TRADER_BASE": str(tmp_path / "services" / "auto_trader"),
        "AUTO_TRADER_HEALTHCHECK_SKIP_WS": "1",
    }
    proc = subprocess.run(["bash", str(HC)], check=False, capture_output=True, text=True, env=env)
    assert proc.returncode == 0, proc.stderr + proc.stdout
```

- [ ] **Step 2: Run test, verify failure**

```
uv run pytest tests/scripts/test_healthcheck_native.py -v
```

Expected: 2 fail.

- [ ] **Step 3: Implement healthcheck-native.sh**

Create `ops/native/scripts/healthcheck-native.sh`:

```bash
#!/bin/zsh
# ROB-259: healthcheck-native.sh with direct-color and stable modes.
#
# Usage:
#   healthcheck-native.sh                    # probe HAProxy stable ports (8000, 8765)
#   healthcheck-native.sh --direct blue      # probe :8001 + :8766 directly
#   healthcheck-native.sh --direct green     # probe :8002 + :8767 directly
set -euo pipefail

source "${HOME:-/Users/mgh3326}/services/auto_trader/scripts/common.sh"

MODE="stable"
COLOR=""

while (( $# > 0 )); do
  case "$1" in
    --direct)
      MODE="direct"
      COLOR="${2:?--direct requires color}"
      shift 2
      ;;
    *)
      echo "unknown arg: $1" >&2
      exit 64
      ;;
  esac
done

if [[ "$MODE" == "direct" ]]; then
  case "$COLOR" in
    blue)  API_PORT=8001; MCP_PORT=8766 ;;
    green) API_PORT=8002; MCP_PORT=8767 ;;
    *) echo "invalid color: $COLOR" >&2; exit 64 ;;
  esac
else
  API_PORT=8000
  MCP_PORT=8765
fi

rc=0
if ! curl -fsS "http://127.0.0.1:${API_PORT}/healthz" >/dev/null; then
  echo "api healthz failed at :${API_PORT}" >&2
  rc=1
fi

code=$(curl -sS -o /dev/null -w '%{http_code}' -H 'Accept: text/event-stream' "http://127.0.0.1:${MCP_PORT}/mcp" || true)
if [[ "$code" != "401" && "$code" != "400" ]]; then
  echo "mcp unexpected status at :${MCP_PORT}: $code" >&2
  rc=1
fi

if [[ "${AUTO_TRADER_HEALTHCHECK_SKIP_WS:-0}" != "1" && "$MODE" == "stable" ]]; then
  WS_MONITOR_HEARTBEAT_PATH="$AUTO_TRADER_BASE/state/heartbeat/kis.json" WS_MONITOR_EXPECT_MODE=kis \
    uv run python scripts/websocket_healthcheck.py || rc=1
  WS_MONITOR_HEARTBEAT_PATH="$AUTO_TRADER_BASE/state/heartbeat/upbit.json" WS_MONITOR_EXPECT_MODE=upbit \
    uv run python scripts/websocket_healthcheck.py || rc=1
fi

exit $rc
```

- [ ] **Step 4: Update curl stub status semantics in test**

Note: the test stub above uses a simplified mapping. If the stub's exit-code logic doesn't match `curl -fsS` semantics (fail on 4xx/5xx), tighten the stub: 200 → exit 0, 401/400 → still exit 0 if `-fsS` is NOT present (the MCP probe does NOT use `-fsS`), and 5xx → exit 22. Adjust the test to differentiate.

- [ ] **Step 5: Run tests, verify pass**

```
chmod +x ops/native/scripts/healthcheck-native.sh
uv run pytest tests/scripts/test_healthcheck_native.py -v
```

Expected: 2 PASS.

- [ ] **Step 6: Commit**

```
git add ops/native/scripts/healthcheck-native.sh tests/scripts/test_healthcheck_native.py
git commit -m "feat(ops): healthcheck-native --direct color mode (ROB-259)"
```

---

## Task 8: Refactor scripts/deploy-native.sh to blue/green flow

**Files:**

- Modify: `scripts/deploy-native.sh`
- Test: `tests/scripts/test_deploy_native_bluegreen.py`

This is the largest task. The strategy:

1. **Extract** the deploy-script logic that is testable in isolation into pure-bash functions in a new file `ops/native/scripts/native_deploy_lib.sh` and write subprocess-driven pytest cases.
2. **Refactor** `scripts/deploy-native.sh` to source the lib and the bluegreen lib, replacing `restart_services()`'s single-pass loop with the blue/green flow.
3. **Keep** the existing worker/scheduler/websocket restart path intact (still single-active).

Because deploy-native.sh actually invokes `launchctl`, `git`, `uv`, `npm`, etc. that can't run in a unit test, the testable boundary is the bluegreen flow functions: `detect_active_color`, `inactive_color`, `sync_release_to_color_symlink`, `bootstrap_color`, `probe_color_direct`, `haproxy_switch`, `drain_color`. The launchctl-invoking parts get a `LAUNCHCTL_BIN` indirection that defaults to `/bin/launchctl` but tests can override to a stub.

- [ ] **Step 1: Write failing test**

Create `tests/scripts/test_deploy_native_bluegreen.py`:

```python
"""ROB-259 deploy-native blue/green flow unit tests.

We test the extracted helper lib (ops/native/scripts/native_deploy_lib.sh) end-to-end
under a fake AUTO_TRADER_BASE with stubbed launchctl/curl/haproxy.
"""

from __future__ import annotations

import os
import subprocess
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
LIB = REPO_ROOT / "ops" / "native" / "scripts" / "native_deploy_lib.sh"


def _stub_dir(tmp_path: Path) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    # launchctl stub: log all invocations to $LAUNCHCTL_LOG
    (bin_dir / "launchctl").write_text(textwrap.dedent("""\
        #!/usr/bin/env bash
        echo "launchctl $*" >>"$LAUNCHCTL_LOG"
        exit 0
        """))
    (bin_dir / "launchctl").chmod(0o755)
    # curl stub: succeeds for any 127.0.0.1 probe
    (bin_dir / "curl").write_text(textwrap.dedent("""\
        #!/usr/bin/env bash
        # mimic enough of curl -fsS for our probes
        for arg in "$@"; do
          case "$arg" in
            -fsS) ;;
            -sS) ;;
            -o) ;;
            -w) ;;
            -H) ;;
            -*) ;;
            *)
              url="$arg"
              ;;
          esac
        done
        case "$url" in
          *127.0.0.1*) exit 0 ;;
          *) exit 6 ;;
        esac
        """))
    (bin_dir / "curl").chmod(0o755)
    # haproxy stub: -c always succeeds
    (bin_dir / "haproxy").write_text("#!/usr/bin/env bash\nexit 0\n")
    (bin_dir / "haproxy").chmod(0o755)
    return bin_dir


def _setup_base(tmp_path: Path) -> Path:
    base = tmp_path / "services" / "auto_trader"
    for sub in ["releases/sha-old", "releases/sha-new", "shared/haproxy", "scripts", "plists", "logs"]:
        (base / sub).mkdir(parents=True, exist_ok=True)
    (base / "shared" / "api-active-color").write_text("blue\n")
    (base / "shared" / "mcp-active-color").write_text("blue\n")
    (base / "current-blue").symlink_to(base / "releases" / "sha-old")
    # Copy library + plists + scripts into the prod-like base
    for src in [
        REPO_ROOT / "ops" / "native" / "scripts" / "native_bluegreen_lib.sh",
        REPO_ROOT / "ops" / "native" / "scripts" / "haproxy_render.sh",
        REPO_ROOT / "ops" / "native" / "scripts" / "haproxy_switch.sh",
        REPO_ROOT / "ops" / "native" / "scripts" / "healthcheck-native.sh",
        REPO_ROOT / "ops" / "native" / "scripts" / "native_deploy_lib.sh",
    ]:
        dst = base / "scripts" / src.name
        dst.write_text(src.read_text())
        dst.chmod(0o755)
    (base / "scripts" / "common.sh").write_text("# noop common.sh stub for tests\n")
    (base / "scripts" / "haproxy").mkdir(exist_ok=True)
    (base / "scripts" / "haproxy" / "haproxy.cfg.tmpl").write_text(
        (REPO_ROOT / "ops" / "native" / "haproxy" / "haproxy.cfg.tmpl").read_text()
    )
    for p in (REPO_ROOT / "ops" / "native" / "plists").glob("*.plist"):
        (base / "plists" / p.name).write_text(p.read_text())
    return base


def _run_bash(snippet: str, base: Path, tmp_path: Path) -> subprocess.CompletedProcess:
    bin_dir = _stub_dir(tmp_path)
    log = tmp_path / "launchctl.log"
    log.write_text("")
    env = {
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "HOME": str(tmp_path),
        "AUTO_TRADER_BASE": str(base),
        "AUTO_TRADER_HAPROXY_RELOAD": "skip",
        "LAUNCHCTL_LOG": str(log),
        "AUTO_TRADER_HEALTHCHECK_SKIP_WS": "1",
    }
    script = f'set -Eeuo pipefail\nsource "{LIB}"\n{snippet}\n'
    proc = subprocess.run(["bash", "-c", script], check=False, capture_output=True, text=True, env=env)
    proc.launchctl_log = log.read_text()  # type: ignore[attr-defined]
    return proc


def test_sync_release_to_color_symlink_creates(tmp_path: Path) -> None:
    base = _setup_base(tmp_path)
    proc = _run_bash(f'sync_release_to_color_symlink green "{base}/releases/sha-new"', base, tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert (base / "current-green").resolve() == (base / "releases" / "sha-new")


def test_bootstrap_color_invokes_launchctl(tmp_path: Path) -> None:
    base = _setup_base(tmp_path)
    proc = _run_bash('bootstrap_color api green', base, tmp_path)
    assert proc.returncode == 0, proc.stderr
    log = proc.launchctl_log  # type: ignore[attr-defined]
    assert "bootstrap" in log and "api-green" in log


def test_drain_color_bootouts(tmp_path: Path) -> None:
    base = _setup_base(tmp_path)
    proc = _run_bash('drain_color api blue', base, tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert "bootout" in proc.launchctl_log and "api-blue" in proc.launchctl_log  # type: ignore[attr-defined]


def test_probe_color_direct_passes(tmp_path: Path) -> None:
    base = _setup_base(tmp_path)
    proc = _run_bash('probe_color_direct green', base, tmp_path)
    assert proc.returncode == 0, proc.stderr


def test_haproxy_swap_to_color_updates_state_and_writes_live(tmp_path: Path) -> None:
    base = _setup_base(tmp_path)
    proc = _run_bash('haproxy_swap_to_color api green', base, tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert (base / "shared" / "api-active-color").read_text().strip() == "green"
    assert (base / "shared" / "haproxy" / "haproxy.cfg").is_file()


def test_full_deploy_flow_dry_run(tmp_path: Path) -> None:
    """End-to-end happy path: bootstrap green, probe, swap, drain blue."""
    base = _setup_base(tmp_path)
    snippet = textwrap.dedent(f'''\
        deploy_bluegreen_flow "{base}/releases/sha-new"
    ''')
    proc = _run_bash(snippet, base, tmp_path)
    assert proc.returncode == 0, proc.stderr + proc.stdout
    # Final active color for both is green
    assert (base / "shared" / "api-active-color").read_text().strip() == "green"
    assert (base / "shared" / "mcp-active-color").read_text().strip() == "green"
    # current-green points at new release
    assert (base / "current-green").resolve() == (base / "releases" / "sha-new")
    # blue was bootout
    log = proc.launchctl_log  # type: ignore[attr-defined]
    assert "bootout" in log and "api-blue" in log
    assert "bootstrap" in log and "api-green" in log


def test_full_deploy_rolls_back_on_probe_failure(tmp_path: Path) -> None:
    """If green direct probe fails, no HAProxy swap, no state-file change."""
    base = _setup_base(tmp_path)
    # Sabotage curl so green probe fails: replace curl stub to exit 22 for :8002 / :8767
    bin_dir = _stub_dir(tmp_path)
    (bin_dir / "curl").write_text(textwrap.dedent("""\
        #!/usr/bin/env bash
        for arg in "$@"; do
          case "$arg" in
            -*) ;;
            *) url="$arg" ;;
          esac
        done
        case "$url" in
          *8002* | *8767*) exit 22 ;;
          *127.0.0.1*) exit 0 ;;
          *) exit 6 ;;
        esac
        """))
    (bin_dir / "curl").chmod(0o755)
    log = tmp_path / "launchctl.log"
    log.write_text("")
    env = {
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "HOME": str(tmp_path),
        "AUTO_TRADER_BASE": str(base),
        "AUTO_TRADER_HAPROXY_RELOAD": "skip",
        "LAUNCHCTL_LOG": str(log),
        "AUTO_TRADER_HEALTHCHECK_SKIP_WS": "1",
    }
    script = f'set -Eeuo pipefail\nsource "{LIB}"\ndeploy_bluegreen_flow "{base}/releases/sha-new"\n'
    proc = subprocess.run(["bash", "-c", script], check=False, capture_output=True, text=True, env=env)
    assert proc.returncode != 0
    assert (base / "shared" / "api-active-color").read_text().strip() == "blue"
    # No swap should have written the live config file in failure path
    assert not (base / "shared" / "haproxy" / "haproxy.cfg").exists() or \
        "ROLLED-BACK" in (base / "shared" / "haproxy" / "haproxy.cfg").read_text()
```

- [ ] **Step 2: Run test, verify failure**

```
uv run pytest tests/scripts/test_deploy_native_bluegreen.py -v
```

Expected: 7 fail.

- [ ] **Step 3: Implement the deploy library**

Create `ops/native/scripts/native_deploy_lib.sh`:

```bash
#!/usr/bin/env bash
# ROB-259: blue/green deploy primitives. Source-only.
#
# Required env:
#   AUTO_TRADER_BASE
# Optional env:
#   AUTO_TRADER_HEALTHCHECK_ATTEMPTS  (default 6)
#   AUTO_TRADER_HEALTHCHECK_INTERVAL_SECONDS (default 5)

SCRIPT_DIR_NDL="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=native_bluegreen_lib.sh
source "$SCRIPT_DIR_NDL/native_bluegreen_lib.sh"

_ndl_uid() { id -u; }

_ndl_plist_path() {
  local service="$1" color="$2"
  echo "$AUTO_TRADER_BASE/plists/com.robinco.auto-trader.${service}-${color}.plist"
}

# sync_release_to_color_symlink <color> <release_path>
sync_release_to_color_symlink() {
  local color="$1" release="$2"
  _bg_validate_color "$color"
  [[ -d "$release" ]] || { echo "release dir missing: $release" >&2; return 78; }
  local symlink
  symlink="$(color_current_symlink "$color")"
  ln -sfn "$release" "$symlink"
}

# bootstrap_color <service> <color>
bootstrap_color() {
  local service="$1" color="$2"
  _bg_validate_service "$service"
  _bg_validate_color "$color"
  local label plist target uid
  label="$(color_label "$service" "$color")"
  plist="$(_ndl_plist_path "$service" "$color")"
  target="$HOME/Library/LaunchAgents/$label.plist"
  uid="$(_ndl_uid)"
  [[ -f "$plist" ]] || { echo "missing plist: $plist" >&2; return 78; }
  install -m 0644 "$plist" "$target"
  launchctl bootout "gui/$uid/$label" 2>/dev/null || true
  launchctl bootstrap "gui/$uid" "$target"
  launchctl enable "gui/$uid/$label"
  launchctl kickstart -k "gui/$uid/$label"
}

# drain_color <service> <color>
# Stop and unload the specified color's launchd job.
drain_color() {
  local service="$1" color="$2"
  _bg_validate_service "$service"
  _bg_validate_color "$color"
  local label uid target
  label="$(color_label "$service" "$color")"
  uid="$(_ndl_uid)"
  target="$HOME/Library/LaunchAgents/$label.plist"
  launchctl bootout "gui/$uid/$label" 2>/dev/null || true
  # Leave plist on disk so re-bootstrap works on next deploy.
}

# probe_color_direct <color>
# Run healthcheck against the named color's app ports.
probe_color_direct() {
  local color="$1"
  _bg_validate_color "$color"
  local attempts="${AUTO_TRADER_HEALTHCHECK_ATTEMPTS:-6}"
  local interval="${AUTO_TRADER_HEALTHCHECK_INTERVAL_SECONDS:-5}"
  local hc="$AUTO_TRADER_BASE/scripts/healthcheck-native.sh"
  local attempt
  for ((attempt = 1; attempt <= attempts; attempt++)); do
    if "$hc" --direct "$color"; then
      return 0
    fi
    if (( attempt < attempts )); then
      sleep "$interval"
    fi
  done
  echo "probe_color_direct: $color failed after $attempts attempts" >&2
  return 1
}

# haproxy_swap_to_color <service> <new_color>
# Update state file then run haproxy_switch.sh.
haproxy_swap_to_color() {
  local service="$1" new_color="$2"
  _bg_validate_service "$service"
  _bg_validate_color "$new_color"
  set_active_color "$service" "$new_color"
  AUTO_TRADER_HAPROXY_TEMPLATE="$AUTO_TRADER_BASE/scripts/haproxy/haproxy.cfg.tmpl" \
    bash "$AUTO_TRADER_BASE/scripts/haproxy_switch.sh"
}

# probe_public
# Use the same healthcheck-native.sh with default (stable) mode + an additional public smoke.
probe_public_stable() {
  local attempts="${AUTO_TRADER_HEALTHCHECK_ATTEMPTS:-6}"
  local interval="${AUTO_TRADER_HEALTHCHECK_INTERVAL_SECONDS:-5}"
  local hc="$AUTO_TRADER_BASE/scripts/healthcheck-native.sh"
  local attempt
  for ((attempt = 1; attempt <= attempts; attempt++)); do
    if "$hc"; then
      return 0
    fi
    if (( attempt < attempts )); then
      sleep "$interval"
    fi
  done
  return 1
}

# deploy_bluegreen_flow <release_path>
# Full happy-path: figure out colors, bootstrap inactive, probe, swap, drain old.
# On any failure before swap: drain new color, exit non-zero.
# On swap success but post-swap failure: swap back, drain new color.
deploy_bluegreen_flow() {
  local release="$1"
  [[ -d "$release" ]] || { echo "release dir missing: $release" >&2; return 78; }

  local api_active mcp_active api_new mcp_new
  api_active="$(detect_active_color api)"
  mcp_active="$(detect_active_color mcp)"
  api_new="$(inactive_color "$api_active")"
  mcp_new="$(inactive_color "$mcp_active")"
  echo "active api=$api_active mcp=$mcp_active; bootstrapping api=$api_new mcp=$mcp_new"

  # Update both inactive symlinks to the new release.
  sync_release_to_color_symlink "$api_new" "$release"
  if [[ "$api_new" != "$mcp_new" ]]; then
    # Same release for both colors when they happen to diverge.
    sync_release_to_color_symlink "$mcp_new" "$release"
  fi

  # Bootstrap new colors.
  if ! bootstrap_color api "$api_new"; then
    echo "bootstrap api-$api_new failed" >&2
    drain_color api "$api_new" || true
    return 1
  fi
  if ! bootstrap_color mcp "$mcp_new"; then
    echo "bootstrap mcp-$mcp_new failed" >&2
    drain_color mcp "$mcp_new" || true
    drain_color api "$api_new" || true
    return 1
  fi

  # Probe new colors directly.
  if ! probe_color_direct "$api_new"; then
    drain_color api "$api_new" || true
    drain_color mcp "$mcp_new" || true
    return 1
  fi

  # Swap HAProxy for both.
  if ! haproxy_swap_to_color api "$api_new"; then
    drain_color api "$api_new" || true
    drain_color mcp "$mcp_new" || true
    return 1
  fi
  if ! haproxy_swap_to_color mcp "$mcp_new"; then
    # Swap api back and bail.
    set_active_color api "$api_active"
    AUTO_TRADER_HAPROXY_TEMPLATE="$AUTO_TRADER_BASE/scripts/haproxy/haproxy.cfg.tmpl" \
      bash "$AUTO_TRADER_BASE/scripts/haproxy_switch.sh" || true
    drain_color api "$api_new" || true
    drain_color mcp "$mcp_new" || true
    return 1
  fi

  # Public smoke. If it fails: roll both colors back, drain new, return non-zero.
  if ! probe_public_stable; then
    set_active_color api "$api_active"
    set_active_color mcp "$mcp_active"
    AUTO_TRADER_HAPROXY_TEMPLATE="$AUTO_TRADER_BASE/scripts/haproxy/haproxy.cfg.tmpl" \
      bash "$AUTO_TRADER_BASE/scripts/haproxy_switch.sh" || true
    drain_color api "$api_new" || true
    drain_color mcp "$mcp_new" || true
    return 1
  fi

  # Drain old colors only after swap + public smoke succeed.
  drain_color api "$api_active"
  drain_color mcp "$mcp_active"

  echo "deploy_bluegreen_flow: success api=$api_new mcp=$mcp_new"
}
```

- [ ] **Step 4: Run library tests, verify pass**

```
uv run pytest tests/scripts/test_deploy_native_bluegreen.py -v
```

Expected: 7 PASS. If any fail, fix the lib or the curl stub semantics — do not relax assertions.

- [ ] **Step 5: Refactor scripts/deploy-native.sh**

Modify `scripts/deploy-native.sh` to:

1. Sync `ops/native/{plists,scripts,haproxy}` from the new release into `$AUTO_TRADER_BASE/{plists,scripts,scripts/haproxy}` BEFORE attempting bootstrap.
2. Source `native_deploy_lib.sh`.
3. Replace the previous "restart all services" loop with two parts:
   - `deploy_bluegreen_flow "$NEW_RELEASE"` for api+mcp
   - existing-style restart for worker/scheduler/websockets only

Concrete diff against the current file:

**Replace** the `LABELS` array (line 45-52) with two separate arrays:

```bash
SINGLE_ACTIVE_LABELS=(
  "com.robinco.auto-trader.worker"
  "com.robinco.auto-trader.scheduler"
  "com.robinco.auto-trader.kis-websocket"
  "com.robinco.auto-trader.upbit-websocket"
)
```

**Add** new function after `apply_mcp_plist_fd_limit()`:

```bash
sync_release_ops_to_base() {
  log "Syncing ops/native plists+scripts+haproxy from release"
  rsync -a --delete "$NEW_RELEASE/ops/native/plists/" "$PLIST_DIR/"
  rsync -a "$NEW_RELEASE/ops/native/scripts/" "$BASE/scripts/"
  mkdir -p "$BASE/scripts/haproxy"
  rsync -a "$NEW_RELEASE/ops/native/haproxy/" "$BASE/scripts/haproxy/"
  chmod +x "$BASE/scripts/"*.sh "$BASE/scripts/"*.sh 2>/dev/null || true
}
```

**Replace** `restart_services()` (line 126-158) with:

```bash
restart_single_active_services() {
  local uid_num label plist target attempt
  uid_num="$(id -u)"
  for label in "${SINGLE_ACTIVE_LABELS[@]}"; do
    plist="$PLIST_DIR/$label.plist"
    target="$HOME/Library/LaunchAgents/$label.plist"
    if [[ ! -f "$plist" ]]; then
      echo "Missing launchd plist: $plist" >&2
      return 78
    fi
    install -m 0644 "$plist" "$target"
    launchctl bootout "gui/$uid_num/$label" 2>/dev/null || true
    for attempt in {1..5}; do
      if launchctl bootstrap "gui/$uid_num" "$target"; then
        break
      fi
      if (( attempt == 5 )); then
        echo "Failed to bootstrap $label after $attempt attempts" >&2
        return 5
      fi
      sleep 1
    done
    launchctl enable "gui/$uid_num/$label"
    launchctl kickstart -k "gui/$uid_num/$label"
  done
}
```

**Replace** `apply_mcp_plist_fd_limit` callers — since the new mcp-blue/mcp-green plists already include the resource-limit blocks at template time, this helper is no longer needed; delete the function and the call from the old `restart_services` loop. (Keep it as dead code only if you fear regressing the standalone `com.robinco.auto-trader.mcp.plist`; this PR removes that plist as part of cutover, so removal is safe.)

**Replace** lines 289-297 (`Switching current symlink` block):

```bash
log "Syncing release ops into base"
sync_release_ops_to_base

log "Running blue/green deploy for api + mcp"
# shellcheck source=/dev/null
source "$BASE/scripts/native_deploy_lib.sh"
deploy_bluegreen_flow "$NEW_RELEASE"

log "Switching current symlink (worker/scheduler/websockets)"
ln -sfn "$NEW_RELEASE" "$CURRENT"
SWITCHED=1

log "Restarting single-active services"
restart_single_active_services

log "Running healthcheck"
run_healthcheck
```

**Update** `rollback()` (line 220-233): when SWITCHED=1 and a previous release exists, revert the `current` symlink as before. For blue/green: rollback inside `deploy_bluegreen_flow` is already handled internally; the outer rollback only owns the `current` symlink for worker/scheduler/websockets.

```bash
rollback() {
  local exit_code=$?
  echo "Deploy failed with exit code $exit_code" >&2
  if [[ "$SWITCHED" == "1" && -n "${PREVIOUS_RELEASE:-}" && -d "$PREVIOUS_RELEASE" ]]; then
    echo "Rolling back current symlink to: $PREVIOUS_RELEASE" >&2
    ln -sfn "$PREVIOUS_RELEASE" "$CURRENT"
    restart_single_active_services || true
  else
    echo "No symlink switch happened, or previous release is unavailable; skipping rollback restart" >&2
  fi
  exit "$exit_code"
}
```

- [ ] **Step 6: Run shellcheck on deploy-native.sh and the new lib**

```
shellcheck scripts/deploy-native.sh ops/native/scripts/*.sh
```

Fix any new warnings. If shellcheck is not installed locally, skip with a TODO note in the PR description; CI may not enforce it but the prod Mac should have it via brew.

- [ ] **Step 7: Commit**

```
git add scripts/deploy-native.sh ops/native/scripts/native_deploy_lib.sh tests/scripts/test_deploy_native_bluegreen.py
git commit -m "feat(deploy): blue/green api+mcp via HAProxy switch (ROB-259)"
```

---

## Task 9: Add first-time cutover script

**Files:**

- Create: `scripts/native_haproxy_first_cutover.sh`

This is a one-shot script run manually on the production Mac the first time we move from "Cloudflare → 8000 directly" to "Cloudflare → HAProxy → blue:8001". It accepts a brief downtime window (~10-30s) because the existing api at :8000 has to be stopped before HAProxy can bind :8000.

The script is idempotent and self-checks each step.

- [ ] **Step 1: Implement the script**

Create `scripts/native_haproxy_first_cutover.sh`:

```bash
#!/usr/bin/env bash
# ROB-259: One-shot migration from direct-port deployment to HAProxy blue/green.
#
# Run this ONCE on the production Mac after the auto_trader release containing
# the ops/native/* assets is on disk under $AUTO_TRADER_BASE/current.
#
# What it does:
#   1) Verifies HAProxy is installed via Homebrew
#   2) Syncs new plists/scripts/haproxy template into $AUTO_TRADER_BASE
#   3) Sets api/mcp active color = blue and writes state files
#   4) Symlinks current-blue -> current
#   5) Drains existing single-port api/mcp plists
#   6) Bootstraps api-blue + mcp-blue on :8001 / :8766
#   7) Direct-probes blue at :8001 and :8766
#   8) Renders + starts HAProxy on :8000 / :8765
#   9) Public-port probes via :8000 / :8765
#  10) Removes the now-stale single-port plists from LaunchAgents/

set -Eeuo pipefail

BASE="${AUTO_TRADER_BASE:-/Users/mgh3326/services/auto_trader}"
export AUTO_TRADER_BASE="$BASE"
RELEASE_OPS="$BASE/current/ops/native"

log() { printf '[%s] %s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$*"; }

require_brew_haproxy() {
  if ! command -v haproxy >/dev/null 2>&1; then
    echo "haproxy not installed. Run: brew install haproxy" >&2
    exit 78
  fi
  log "haproxy: $(haproxy -v 2>&1 | head -1)"
}

sync_repo_assets() {
  log "Syncing ops/native -> $BASE"
  rsync -a --delete "$RELEASE_OPS/plists/" "$BASE/plists/"
  rsync -a "$RELEASE_OPS/scripts/" "$BASE/scripts/"
  mkdir -p "$BASE/scripts/haproxy" "$BASE/shared/haproxy"
  rsync -a "$RELEASE_OPS/haproxy/" "$BASE/scripts/haproxy/"
  chmod +x "$BASE/scripts/"*.sh 2>/dev/null || true
}

init_state_and_symlinks() {
  # shellcheck source=ops/native/scripts/native_bluegreen_lib.sh
  source "$BASE/scripts/native_bluegreen_lib.sh"
  set_active_color api blue
  set_active_color mcp blue
  ln -sfn "$(readlink "$BASE/current")" "$BASE/current-blue"
  # current-green starts unset; first deploy will create it.
  log "active colors set to blue; current-blue -> $(readlink "$BASE/current-blue")"
}

drain_old_single_active_api_mcp() {
  local uid; uid="$(id -u)"
  for label in com.robinco.auto-trader.api com.robinco.auto-trader.mcp; do
    log "Draining $label"
    launchctl bootout "gui/$uid/$label" 2>/dev/null || true
  done
}

bootstrap_blue() {
  source "$BASE/scripts/native_deploy_lib.sh"
  bootstrap_color api blue
  bootstrap_color mcp blue
}

probe_blue_direct() {
  AUTO_TRADER_HEALTHCHECK_ATTEMPTS=12 \
  AUTO_TRADER_HEALTHCHECK_INTERVAL_SECONDS=2 \
    "$BASE/scripts/healthcheck-native.sh" --direct blue
}

start_haproxy() {
  local uid; uid="$(id -u)"
  local label=com.robinco.auto-trader.haproxy
  local plist="$BASE/plists/$label.plist"
  local target="$HOME/Library/LaunchAgents/$label.plist"
  install -m 0644 "$plist" "$target"
  # Render initial config
  AUTO_TRADER_HAPROXY_RELOAD=skip bash "$BASE/scripts/haproxy_switch.sh"
  launchctl bootout "gui/$uid/$label" 2>/dev/null || true
  launchctl bootstrap "gui/$uid" "$target"
  launchctl enable "gui/$uid/$label"
  launchctl kickstart -k "gui/$uid/$label"
}

probe_public_stable() {
  AUTO_TRADER_HEALTHCHECK_ATTEMPTS=12 \
  AUTO_TRADER_HEALTHCHECK_INTERVAL_SECONDS=2 \
    "$BASE/scripts/healthcheck-native.sh"
}

remove_stale_plists() {
  for stale in com.robinco.auto-trader.api com.robinco.auto-trader.mcp; do
    rm -f "$HOME/Library/LaunchAgents/$stale.plist"
    rm -f "$BASE/plists/$stale.plist"
  done
}

main() {
  require_brew_haproxy
  sync_repo_assets
  init_state_and_symlinks
  drain_old_single_active_api_mcp
  bootstrap_blue
  probe_blue_direct
  start_haproxy
  probe_public_stable
  remove_stale_plists
  log "first cutover complete; subsequent deploys use blue/green"
}

main "$@"
```

- [ ] **Step 2: chmod + smoke-document**

```
chmod +x scripts/native_haproxy_first_cutover.sh
```

This script is operator-run, not unit-tested. The runbook (Task 10) documents the prerequisites and the manual smoke-after sequence.

- [ ] **Step 3: Commit**

```
git add scripts/native_haproxy_first_cutover.sh
git commit -m "feat(ops): first-cutover helper for HAProxy install (ROB-259)"
```

---

## Task 10: Write runbook

**Files:**

- Create: `docs/runbooks/native-haproxy-blue-green.md`

- [ ] **Step 1: Write the runbook**

Create `docs/runbooks/native-haproxy-blue-green.md` with the following sections (each ~5-15 lines, no placeholders):

````markdown
# Native HAProxy Blue/Green Deploy (ROB-259)

## Architecture summary

[Diagram: Cloudflare → HAProxy stable :8000/:8765 → api-blue/green + mcp-blue/green per-color plists]

## First-time cutover (one-shot)

Prerequisites:

- The release containing `ops/native/*` is deployed under `$AUTO_TRADER_BASE/current`.
- `brew install haproxy` has been run.

Run:

```
sudo -u mgh3326 /Users/mgh3326/services/auto_trader/current/scripts/native_haproxy_first_cutover.sh
```

Expected window: ~10-30s where `trader.robinco.dev` may briefly 502 while api at :8000 is stopped and HAProxy takes over. Run outside business hours.

Verify after:

- `launchctl list | grep auto-trader` shows `haproxy`, `api-blue`, `mcp-blue`, plus worker/scheduler/websocket.
- `curl http://127.0.0.1:8000/healthz` → 200 (HAProxy → :8001).
- `curl -H 'Accept: text/event-stream' http://127.0.0.1:8765/mcp` → 400/401 (HAProxy → :8766).
- `curl https://trader.robinco.dev/healthz` → 200.

## Normal deploy

```
scripts/deploy-native.sh <sha>
```

The script will:

1. Stage release under `releases/<sha>`
2. Sync `ops/native/*` into `$AUTO_TRADER_BASE`
3. Run alembic migrations (must be expand-only)
4. Detect active color (e.g. blue), bootstrap the inactive color (green) on :8002/:8767
5. Direct-probe green at :8002 and :8767
6. Render new HAProxy config with green primary, SIGUSR2 reload
7. Public smoke via :8000/:8765 (HAProxy now routes to green)
8. Drain blue (bootout)
9. Restart worker/scheduler/websockets against the new `current` symlink

Cloudflare Tunnel sees zero `connection refused` because HAProxy never closes its listeners.

## Rollback (automatic, on failed deploy)

- **Probe green failed (step 5):** green is bootout, HAProxy never swaps. Blue stays active. Deploy exits non-zero.
- **Public smoke failed (step 7):** HAProxy is swapped back to blue (state files written, SIGUSR2). Green is bootout. Worker/scheduler still on old `current` symlink (outer rollback in deploy-native.sh handles this).

## Manual rollback (operator-driven)

If a deploy succeeded but you want to revert to the previous color:

```
cd $AUTO_TRADER_BASE
source scripts/native_bluegreen_lib.sh
set_active_color api blue   # or green
set_active_color mcp blue
bash scripts/haproxy_switch.sh
source scripts/native_deploy_lib.sh
bootstrap_color api blue
bootstrap_color mcp blue
scripts/healthcheck-native.sh
```

## How to inspect active color

```
cat $AUTO_TRADER_BASE/shared/api-active-color
cat $AUTO_TRADER_BASE/shared/mcp-active-color
launchctl list | grep auto-trader
readlink $AUTO_TRADER_BASE/current-blue
readlink $AUTO_TRADER_BASE/current-green
```

## HAProxy ops

```
# Reload config without dropping listeners (after manual cfg edit)
launchctl kill SIGUSR2 gui/$(id -u)/com.robinco.auto-trader.haproxy

# Validate a candidate config
haproxy -c -f /Users/mgh3326/services/auto_trader/shared/haproxy/haproxy.cfg
```

## Cloudflared

No change. `trader.robinco.dev` and `trader-mcp.robinco.dev` still point at `127.0.0.1:8000` and `127.0.0.1:8765` respectively — those are now HAProxy stable listeners.

## Known limitations

- **FastMCP long-lived sessions** still reconnect during HAProxy reload because session state lives in the MCP server process and switching backend kills existing SSE streams. Acceptable per ROB-259 success criteria; clients should retry on `Session terminated`.
- **Worker/scheduler/websockets** still have a brief launchctl bootstrap window. Not in scope for ROB-259.
````

- [ ] **Step 2: Commit**

```
git add docs/runbooks/native-haproxy-blue-green.md
git commit -m "docs(runbooks): native HAProxy blue/green deploy guide (ROB-259)"
```

---

## Task 11: Update Hermes operations workflow reference

**Files:**

- Modify: `/Users/mgh3326/.hermes/skills/devops/auto-trader-operations-workflow/references/native-deploy-api-blue-green.md`

The existing reference recommended "API-only blue/green, not whole-system blue/green" with an HAProxy upstream. Now that we ship both api + mcp blue/green, update the reference to point at the runbook.

- [ ] **Step 1: Replace the file content**

Overwrite with:

```markdown
# Native deploy HAProxy blue/green (ROB-259)

As of ROB-259, auto_trader native deploy uses HAProxy as a stable origin in front of both FastAPI and FastMCP with color-specific blue/green slots.

**Canonical runbook:** `docs/runbooks/native-haproxy-blue-green.md` in the auto_trader repo.

## Architecture

```text
Cloudflare Tunnel
  -> 127.0.0.1:8000  (HAProxy) -> api-blue :8001 | api-green :8002
  -> 127.0.0.1:8765  (HAProxy) -> mcp-blue :8766 | mcp-green :8767
```

Worker/scheduler/websocket services remain single-active.

## Diagnosis quick checks

- `cat $AUTO_TRADER_BASE/shared/api-active-color`
- `cat $AUTO_TRADER_BASE/shared/mcp-active-color`
- `launchctl list | grep auto-trader`
- `tail $AUTO_TRADER_BASE/logs/com.robinco.auto-trader.haproxy.err.log`
- cloudflared `connection refused` count — should be 0 during deploy if HAProxy is healthy.

## Scope boundaries

- Worker/scheduler/websocket monitors stay single-active to avoid duplicate side effects.
- DB migrations must remain expand-only because both colors briefly share the DB.
- FastMCP SSE sessions reconnect during reload — clients should retry.
```

- [ ] **Step 2: Commit (in hermes profile, separately if needed)**

This file lives outside the repo. If the operator manages hermes references in a separate git tree, that's its own commit. Otherwise just note in the PR description that this file was updated alongside ROB-259.

---

## Task 12: Manual smoke checklist + PR description

- [ ] **Step 1: Write PR body checklist**

Draft PR body (to use when opening the PR with `gh pr create`):

```markdown
## Summary

Implements ROB-259: HAProxy-based blue/green deploys for FastAPI + FastMCP on the macOS native production layout.

- New `ops/native/{plists,scripts,haproxy}/` repo-managed deploy assets
- New blue/green helper libs + tests
- `scripts/deploy-native.sh` refactored: api/mcp use blue/green flow, worker/scheduler/websockets stay single-active
- `scripts/native_haproxy_first_cutover.sh` one-shot migration helper
- Runbook at `docs/runbooks/native-haproxy-blue-green.md`

## Risk & review

This is a high-risk change touching production deploy automation. Reviewers please verify:

- [ ] HAProxy template validates on the prod Mac: `haproxy -c -f <rendered>`
- [ ] No existing single-port `com.robinco.auto-trader.api/mcp.plist` plists are required by other tooling
- [ ] Migrations on `main` since the last deploy are expansion-only
- [ ] First-cutover script downtime window (~30s) is acceptable for the scheduled window

## Test plan

- [ ] `uv run pytest tests/scripts/ -v` — all new tests pass
- [ ] `shellcheck scripts/deploy-native.sh ops/native/scripts/*.sh` clean
- [ ] On a staging Mac (or accepted prod window):
  - [ ] Run first-cutover script
  - [ ] Verify HAProxy listening on :8000 + :8765
  - [ ] `curl https://trader.robinco.dev/healthz` → 200
  - [ ] `curl -I https://trader-mcp.robinco.dev/mcp` → 400/401
  - [ ] Trigger a no-op deploy of HEAD: confirm cloudflared shows zero `connection refused` in deploy window
  - [ ] Verify `api-active-color` flipped blue → green
  - [ ] Trigger a second deploy: confirm flip back green → blue
```

- [ ] **Step 2: After merge, schedule first cutover**

Cutover is an operator action, not part of code merge. Coordinate timing separately.

---

## Self-Review

**Spec coverage check** (against ROB-259 acceptance criteria):

- HAProxy runs as stable local launchd service owning :8000 + :8765 — Task 4 plist + Task 9 cutover ✓
- FastAPI on 8001/8002, FastMCP on 8766/8767 — Tasks 5, 6 ✓
- `deploy-native.sh` deploys to inactive color → verify → atomic switch → stop old — Task 8 ✓
- Failed inactive-color healthcheck keeps HAProxy on previous color — Task 8 `deploy_bluegreen_flow` rollback path + test ✓
- Failed post-switch public smoke rolls HAProxy back — Task 8 `deploy_bluegreen_flow` ✓
- Zero `connection refused` during deploy — verified by manual smoke in PR test plan ✓
- Public smoke for `/invest`, `/invest/api/*`, MCP `/mcp` — `healthcheck-native.sh` covers `/healthz` + `/mcp`; manual checks in runbook cover `/invest` page ✓
- Documentation/runbook — Tasks 10, 11 ✓
- Validated HAProxy config (`haproxy -c -f`) before reload — Task 3 `haproxy_switch.sh` ✓
- Graceful reload — Task 3 SIGUSR2 + `-W` master-worker mode (Task 4 plist) ✓
- Migration path from direct-port to proxy — Task 9 first-cutover script ✓
- DB migrations stay expand-only — explicit warning kept in deploy-native.sh comment at `uv run alembic upgrade head` ✓
- No blue/green for worker/scheduler/websocket — Task 8 splits LABELS array ✓

**Placeholder scan:** no TBD/TODO. All test snippets have full code. All plists have full XML.

**Type consistency:** function names `detect_active_color`, `inactive_color`, `set_active_color`, `color_port`, `color_label`, `color_current_symlink`, `bootstrap_color`, `drain_color`, `probe_color_direct`, `haproxy_swap_to_color`, `sync_release_to_color_symlink`, `deploy_bluegreen_flow` are used consistently across Tasks 2, 3, 8, 9. Env vars `AUTO_TRADER_COLOR`, `AUTO_TRADER_API_PORT`, `AUTO_TRADER_MCP_PORT`, `AUTO_TRADER_HAPROXY_TEMPLATE`, `AUTO_TRADER_HAPROXY_LIVE`, `AUTO_TRADER_HAPROXY_RELOAD`, `AUTO_TRADER_HEALTHCHECK_SKIP_WS`, `AUTO_TRADER_API_ACTIVE_COLOR`, `AUTO_TRADER_MCP_ACTIVE_COLOR` are consistent.

**Outstanding ambiguities the executor must resolve at code time:**

1. **FastMCP port env var name** (Task 6 step 6). Plan assumes `MCP_HTTP_PORT`. If the actual app reads a different name, the executor adapts and adds a test.
2. **HAProxy install path** (`/opt/homebrew/bin/haproxy`). Plan assumes Apple Silicon Homebrew. If the prod Mac is Intel, executor adjusts the plist `ProgramArguments`.
3. **MCP `httpchk`** uses `GET /mcp` expecting 400/401. If FastMCP's unauthenticated `/mcp` response class drifts (e.g. 405), executor adjusts the `http-check expect status` line and rebuilds the test fixtures.
