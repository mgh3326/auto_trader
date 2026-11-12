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


def shiftgate_active_color():
    """Return at-mcp's active shiftgate color, or None when it is unavailable."""
    try:
        result = subprocess.run(
            ["shiftgate", "status", "--json"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, UnicodeError, subprocess.TimeoutExpired) as exc:
        logger.error(
            "mcp.watchdog.shiftgate_status_failed error_class=%s",
            type(exc).__name__,
        )
        return None
    if result.returncode:
        logger.error("mcp.watchdog.shiftgate_status_failed returncode=%s", result.returncode)
        return None

    try:
        status = json.loads(result.stdout)
    except (json.JSONDecodeError, TypeError):
        logger.error("mcp.watchdog.shiftgate_status_parse_failed")
        return None
    if isinstance(status, list):
        services = status
    elif isinstance(status, dict):
        services = status.get("services")
        if services is None:
            # A valid status without service state has nothing to watch yet.
            return None
        if isinstance(services, dict):
            service = services.get("at-mcp")
            services = None
    else:
        logger.error("mcp.watchdog.shiftgate_status_parse_failed")
        return None

    if services is not None:
        if not isinstance(services, list):
            logger.error("mcp.watchdog.shiftgate_status_parse_failed")
            return None
        if any(not isinstance(item, dict) for item in services):
            logger.error("mcp.watchdog.shiftgate_status_parse_failed")
            return None
        matches = [
            item
            for item in services
            if item.get("name") == "at-mcp"
        ]
        if len(matches) > 1:
            logger.error("mcp.watchdog.shiftgate_status_parse_failed")
            return None
        service = matches[0] if matches else None

    if service is None:
        return None
    if not isinstance(service, dict):
        logger.error("mcp.watchdog.shiftgate_status_parse_failed")
        return None
    state = service.get("state")
    if state is None:
        return None
    if not isinstance(state, dict):
        logger.error("mcp.watchdog.shiftgate_status_parse_failed")
        return None
    active = state.get("active")
    if active is None:
        return None
    if not isinstance(active, dict):
        logger.error("mcp.watchdog.shiftgate_status_parse_failed")
        return None
    color = active.get("color")
    if color is None:
        return None
    if color not in COLORS:
        logger.error("mcp.watchdog.shiftgate_status_parse_failed")
        return None
    return color


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
        legacy_status = "inactive"
    else:
        now = time.time() if now is None else now
        legacy_status = check_color(
            run_directory,
            source="legacy",
            color=color,
            heartbeat_name=f"mcp-{color}.json",
            container=f"at-mcp-{color}",
            stale_threshold_s=stale_threshold_s,
            now=now,
        )

    now = time.time() if now is None else now
    shiftgate_color = shiftgate_active_color()
    if shiftgate_color is None:
        return legacy_status
    shiftgate_status = check_color(
        run_directory,
        source="shiftgate",
        color=shiftgate_color,
        heartbeat_name=f"mcp-sg-{shiftgate_color}.json",
        container=f"shiftgate-at-mcp-{shiftgate_color}",
        stale_threshold_s=stale_threshold_s,
        now=now,
    )
    if "restart-failed" in (legacy_status, shiftgate_status):
        return "restart-failed"
    return legacy_status


def check_color(
    run_directory,
    *,
    source,
    color,
    heartbeat_name,
    container,
    stale_threshold_s,
    now,
):
    heartbeat_path = run_directory / "mcp-heartbeat" / heartbeat_name
    try:
        heartbeat = read_heartbeat(heartbeat_path)
    except UnicodeError:
        if source != "shiftgate":
            raise
        logger.error(
            "mcp.watchdog.heartbeat_unavailable source=%s color=%s",
            source,
            color,
        )
        return "missing"
    status = evaluate_heartbeat(
        heartbeat,
        now=now,
        stale_threshold_s=stale_threshold_s,
    )
    if source == "shiftgate" and status == "missing":
        logger.error(
            "mcp.watchdog.heartbeat_unavailable source=%s color=%s",
            source,
            color,
        )
    if status != "wedged":
        if source == "legacy":
            logger.info("mcp.watchdog.skip color=%s status=%s", color, status)
        else:
            logger.info(
                "mcp.watchdog.skip source=%s color=%s status=%s",
                source,
                color,
                status,
            )
        return status
    if not docker_running(container):
        if source == "legacy":
            logger.info("mcp.watchdog.skip color=%s wedged-but-not-running", color)
        else:
            logger.info(
                "mcp.watchdog.skip source=%s color=%s wedged-but-not-running",
                source,
                color,
            )
        return "not-running"
    if restart(container):
        if source == "legacy":
            logger.warning("mcp.watchdog.restarted color=%s stale-heartbeat", color)
        else:
            logger.warning(
                "mcp.watchdog.restarted source=%s color=%s stale-heartbeat",
                source,
                color,
            )
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
