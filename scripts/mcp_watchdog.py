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
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s"
    )
    args = _parse_args(argv)
    base = os.getenv("AUTO_TRADER_BASE", os.path.expanduser("~/services/auto_trader"))
    hb_dir = args.heartbeat_dir or Path(base) / "state" / "heartbeat"
    uid = os.getuid()
    logger.info(
        "mcp.watchdog.start dir=%s interval_s=%s stale_s=%s dry_run=%s",
        hb_dir,
        args.interval_s,
        args.stale_threshold_s,
        args.dry_run,
    )
    while True:
        check_once(
            hb_dir,
            stale_threshold_s=args.stale_threshold_s,
            dry_run=args.dry_run,
            uid=uid,
        )
        if args.once:
            return 0
        time.sleep(args.interval_s)


if __name__ == "__main__":
    sys.exit(main())
