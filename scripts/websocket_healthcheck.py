#!/usr/bin/env python3
"""
WebSocket Monitor Healthcheck Script.

Checks heartbeat file for websocket monitor health status.
Used by Docker healthcheck to determine container health.

Exit codes:
  0: Healthy (heartbeat fresh, running, connected)
  1: Unhealthy (missing/stale heartbeat, not running, not connected)

Environment variables:
  WS_MONITOR_HEARTBEAT_PATH: Path to heartbeat JSON file
    (default: /tmp/websocket_monitor_heartbeat.json)
  WS_MONITOR_EXPECT_MODE: Expected mode ("upbit", "kis", or "both")
    (default: both)
  WS_MONITOR_HEARTBEAT_STALE_SECONDS: Seconds before heartbeat considered stale
    (default: 90)
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

# Default configuration
DEFAULT_HEARTBEAT_PATH = "/tmp/websocket_monitor_heartbeat.json"
DEFAULT_STALE_SECONDS = 90.0
FUTURE_SKEW_TOLERANCE_SECONDS = 1.0


def get_config() -> tuple[str, str, float]:
    """Get configuration from environment variables."""
    heartbeat_path = os.environ.get("WS_MONITOR_HEARTBEAT_PATH", DEFAULT_HEARTBEAT_PATH)
    expect_mode = os.environ.get("WS_MONITOR_EXPECT_MODE", "both")
    stale_seconds = float(
        os.environ.get("WS_MONITOR_HEARTBEAT_STALE_SECONDS", str(DEFAULT_STALE_SECONDS))
    )
    return heartbeat_path, expect_mode, stale_seconds


def check_health() -> tuple[bool, str]:
    """
    Check websocket monitor health based on heartbeat file.

    Returns:
        Tuple of (is_healthy, error_message)
    """
    heartbeat_path, expect_mode, stale_seconds = get_config()
    path = Path(heartbeat_path)

    # Check file exists
    if not path.exists():
        return False, f"Heartbeat file not found: {heartbeat_path}"

    # Read and parse JSON
    try:
        with open(path) as f:
            data = json.load(f)
    except json.JSONDecodeError as e:
        return False, f"Failed to parse heartbeat JSON: {e}"
    except OSError as e:
        return False, f"Failed to read heartbeat file: {e}"

    # Check required fields
    required_fields = ["updated_at_unix", "mode", "is_running"]
    for field in required_fields:
        if field not in data:
            return False, f"Missing required field in heartbeat: {field}"

    # Check staleness
    updated_at = float(data["updated_at_unix"])
    age_seconds = time.time() - updated_at
    if age_seconds < -FUTURE_SKEW_TOLERANCE_SECONDS:
        return False, f"Heartbeat timestamp is in the future (age={age_seconds:.1f}s)"
    if age_seconds > stale_seconds:
        return False, (
            f"Heartbeat is stale (age={age_seconds:.1f}s, threshold={stale_seconds}s)"
        )

    # Check is_running
    if not data["is_running"]:
        return False, "Monitor is not running (is_running=false)"

    # Check mode match
    actual_mode = data["mode"]
    if expect_mode != "both" and actual_mode != expect_mode:
        return False, f"Mode mismatch: expected={expect_mode}, actual={actual_mode}"

    # Check connection status based on expected mode
    if expect_mode in ("kis", "both"):
        kis_connected = data.get("kis_connected", "n/a")
        if kis_connected is not True:
            return False, f"KIS not connected: {kis_connected}"

    if expect_mode in ("upbit", "both"):
        upbit_connected = data.get("upbit_connected", "n/a")
        if upbit_connected is not True:
            return False, f"Upbit not connected: {upbit_connected}"

    return True, f"Healthy (mode={actual_mode}, age={age_seconds:.1f}s)"


def main() -> int:
    """Main entry point."""
    is_healthy, message = check_health()

    if is_healthy:
        print(f"OK: {message}")
        return 0
    else:
        print(f"FAIL: {message}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
