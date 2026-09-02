#!/usr/bin/env python3
"""NCP systemd MCP watchdog: restart only the active wedged color.

It preserves the native watchdog's conservative predicate: a heartbeat must say
the server is running and be stale. Missing or cleanly stopped heartbeats never
cause a restart. Docker restart is deliberately scoped to the active color so a
draining/inactive stream is not disturbed.
"""

import argparse
import json
import logging
import subprocess
import time
from pathlib import Path

logger = logging.getLogger("ncp_mcp_watchdog")
COLORS = ("blue", "green")


def read_heartbeat(path):
    try:
        with path.open(encoding="utf-8") as handle:
            value = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def evaluate_heartbeat(data, now, stale_threshold_s):
    if data is None:
        return "missing"
    if data.get("is_running") is not True:
        return "stopped"
    updated = data.get("updated_at_unix")
    if type(updated) not in (int, float):
        return "missing"
    return "wedged" if now - float(updated) > stale_threshold_s else "healthy"


def active_color(path):
    try:
        value = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return value if value in COLORS else None


def docker_running(container):
    try:
        result = subprocess.run(
            ["docker", "inspect", "--format", "{{.State.Running}}", container],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except subprocess.TimeoutExpired:
        logger.error("mcp.watchdog.docker_inspect_timeout container=%s", container)
        return False
    return result.returncode == 0 and result.stdout.strip() == "true"


def restart(container):
    try:
        result = subprocess.run(
            ["docker", "restart", container],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except subprocess.TimeoutExpired:
        logger.error("mcp.watchdog.docker_restart_timeout container=%s", container)
        return False
    if result.returncode:
        logger.error("mcp.watchdog.docker_restart_failed container=%s", container)
        return False
    return True


def check_once(run_directory, stale_threshold_s, now=None):
    color = active_color(run_directory / "mcp-active-color")
    if color is None:
        logger.error("mcp.watchdog.invalid_or_missing_active_color")
        return "inactive"
    now = time.time() if now is None else now
    status = evaluate_heartbeat(
        read_heartbeat(run_directory / "mcp-heartbeat" / f"mcp-{color}.json"),
        now=now,
        stale_threshold_s=stale_threshold_s,
    )
    container = f"at-mcp-{color}"
    if status != "wedged":
        logger.info("mcp.watchdog.skip color=%s status=%s", color, status)
        return status
    if not docker_running(container):
        logger.info("mcp.watchdog.skip color=%s wedged-but-not-running", color)
        return "not-running"
    if restart(container):
        logger.warning("mcp.watchdog.restarted color=%s stale-heartbeat", color)
        return "restarted"
    return "restart-failed"


def main(argv=None):
    parser = argparse.ArgumentParser(prog="ncp_mcp_watchdog")
    parser.add_argument("--run-directory", type=Path, default=Path("/root/at-run"))
    parser.add_argument("--stale-threshold-s", type=float, default=45.0)
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s"
    )
    return (
        0
        if check_once(args.run_directory, stale_threshold_s=args.stale_threshold_s)
        != "restart-failed"
        else 1
    )


if __name__ == "__main__":
    raise SystemExit(main())
