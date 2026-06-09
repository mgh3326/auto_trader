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
    try:
        result = subprocess.run(
            ["launchctl", "print", f"gui/{uid}/{label}"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except subprocess.TimeoutExpired:
        # Fail safe: treat a hung launchctl as not-loaded so we don't kickstart.
        logger.error("mcp.watchdog.launchctl_print_timeout label=%s", label)
        return False
    return result.returncode == 0


def _kickstart(label: str, *, uid: int) -> None:
    try:
        result = subprocess.run(
            ["launchctl", "kickstart", "-k", f"gui/{uid}/{label}"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except subprocess.TimeoutExpired:
        logger.error("mcp.watchdog.kickstart_timeout label=%s", label)
        return
    if result.returncode != 0:
        logger.error(
            "mcp.watchdog.kickstart_failed label=%s rc=%s stderr=%s",
            label,
            result.returncode,
            result.stderr.strip(),
        )


def check_once(
    heartbeat_dir: Path,
    *,
    stale_threshold_s: float,
    dry_run: bool,
    uid: int,
    now: float | None = None,
    last_kickstart_at: dict[str, float] | None = None,
    grace_s: float = 60.0,
    is_loaded=_job_is_loaded,
    kickstart=_kickstart,
) -> dict[str, str]:
    """One pass over both colors. Returns {color: status}.

    Kickstarts a wedged color whose launchd job is loaded, unless ``dry_run`` — but
    SUPPRESSES a repeat kickstart of the same color within ``grace_s``. This closes the
    flap loop: after a kickstart the restarting process keeps the previous (stale)
    heartbeat on disk for ~15s while it boots (init_sentry + 128 tools), during which a
    naive watchdog would re-kickstart a healthy-but-starting process forever. The grace
    window must comfortably exceed cold-start time. ``last_kickstart_at`` carries the
    per-color decision time across poll iterations. ``is_loaded``/``kickstart`` are
    injectable for tests.
    """
    if now is None:
        now = time.time()
    if last_kickstart_at is None:
        last_kickstart_at = {}
    statuses: dict[str, str] = {}
    for color in COLORS:
        label = LABEL_FMT.format(color=color)
        data = read_heartbeat(heartbeat_dir / f"mcp-{color}.json")
        status = evaluate_heartbeat(data, now=now, stale_threshold_s=stale_threshold_s)
        statuses[color] = status
        if status != "wedged":
            continue
        if not is_loaded(label, uid=uid):
            logger.info("mcp.watchdog.skip color=%s wedged-but-not-loaded", color)
            continue
        last = last_kickstart_at.get(color)
        if last is not None and (now - last) < grace_s:
            logger.info(
                "mcp.watchdog.skip color=%s in-grace-period since_s=%.0f",
                color,
                now - last,
            )
            continue
        last_kickstart_at[color] = now
        if dry_run:
            logger.warning("mcp.watchdog.would_kickstart color=%s (dry-run)", color)
        else:
            logger.warning("mcp.watchdog.kickstart color=%s (stale heartbeat)", color)
            kickstart(label, uid=uid)
    return statuses


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="mcp_watchdog")
    p.add_argument("--heartbeat-dir", type=Path, default=None)
    p.add_argument("--interval-s", type=float, default=15.0)
    # 45s comfortably exceeds a clean restart-to-first-heartbeat (~14s import + ~10s
    # launchd throttle), so a normally-restarting process is not mistaken for wedged.
    p.add_argument("--stale-threshold-s", type=float, default=45.0)
    p.add_argument(
        "--grace-s",
        type=float,
        default=90.0,
        help="After a kickstart, suppress re-kickstart of the same color for this "
        "many seconds so a slow restart cannot flap. Must exceed cold-start time.",
    )
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
        "mcp.watchdog.start dir=%s interval_s=%s stale_s=%s grace_s=%s dry_run=%s",
        hb_dir,
        args.interval_s,
        args.stale_threshold_s,
        args.grace_s,
        args.dry_run,
    )
    last_kickstart_at: dict[str, float] = {}
    while True:
        check_once(
            hb_dir,
            stale_threshold_s=args.stale_threshold_s,
            dry_run=args.dry_run,
            uid=uid,
            last_kickstart_at=last_kickstart_at,
            grace_s=args.grace_s,
        )
        if args.once:
            return 0
        time.sleep(args.interval_s)


if __name__ == "__main__":
    sys.exit(main())
