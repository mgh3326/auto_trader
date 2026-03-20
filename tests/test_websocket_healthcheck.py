"""Tests for websocket healthcheck script."""

import json
import os
import subprocess
import sys
import time
from pathlib import Path


def run_healthcheck(
    heartbeat_path: str,
    expect_mode: str | None = "kis",
    stale_seconds: float = 90,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run healthcheck script with given parameters."""
    script_path = Path(__file__).parent.parent / "scripts" / "websocket_healthcheck.py"
    full_env = os.environ.copy()
    full_env.update(
        {
            "WS_MONITOR_HEARTBEAT_PATH": heartbeat_path,
            "WS_MONITOR_HEARTBEAT_STALE_SECONDS": str(stale_seconds),
        }
    )
    if expect_mode is not None:
        full_env["WS_MONITOR_EXPECT_MODE"] = expect_mode
    else:
        full_env.pop("WS_MONITOR_EXPECT_MODE", None)
    if env:
        full_env.update(env)

    return subprocess.run(
        [sys.executable, str(script_path)],
        capture_output=True,
        text=True,
        env=full_env,
    )


def write_heartbeat(
    path: str,
    *,
    updated_at_unix: float | None = None,
    mode: str = "kis",
    is_running: bool = True,
    upbit_connected: bool | str = "n/a",
    kis_connected: bool | str = True,
) -> None:
    """Write a heartbeat file with given values."""
    if updated_at_unix is None:
        updated_at_unix = time.time()

    data = {
        "updated_at_unix": updated_at_unix,
        "mode": mode,
        "is_running": is_running,
        "upbit_connected": upbit_connected,
        "kis_connected": kis_connected,
    }
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f)


class TestWebsocketHealthcheck:
    """Healthcheck script behavior tests."""

    def test_pass_fresh_heartbeat_kis_connected(self, tmp_path: Path) -> None:
        """Should pass with fresh heartbeat and KIS connected."""
        heartbeat_path = str(tmp_path / "heartbeat.json")
        write_heartbeat(
            heartbeat_path,
            mode="kis",
            is_running=True,
            kis_connected=True,
        )

        result = run_healthcheck(heartbeat_path, expect_mode="kis")

        assert result.returncode == 0, f"Expected pass, got: {result.stderr}"

    def test_pass_fresh_heartbeat_upbit_connected(self, tmp_path: Path) -> None:
        """Should pass with fresh heartbeat and Upbit connected."""
        heartbeat_path = str(tmp_path / "heartbeat.json")
        write_heartbeat(
            heartbeat_path,
            mode="upbit",
            is_running=True,
            upbit_connected=True,
            kis_connected="n/a",
        )

        result = run_healthcheck(heartbeat_path, expect_mode="upbit")

        assert result.returncode == 0, f"Expected pass, got: {result.stderr}"

    def test_fail_missing_heartbeat_file(self, tmp_path: Path) -> None:
        """Should fail when heartbeat file doesn't exist."""
        heartbeat_path = str(tmp_path / "nonexistent.json")

        result = run_healthcheck(heartbeat_path, expect_mode="kis")

        assert result.returncode == 1
        assert "not found" in result.stderr.lower()

    def test_fail_stale_heartbeat(self, tmp_path: Path) -> None:
        """Should fail when heartbeat is too old."""
        heartbeat_path = str(tmp_path / "heartbeat.json")
        # Write heartbeat that's 120 seconds old (stale by default 90s)
        write_heartbeat(
            heartbeat_path,
            updated_at_unix=time.time() - 120,
            mode="kis",
            is_running=True,
            kis_connected=True,
        )

        result = run_healthcheck(heartbeat_path, expect_mode="kis", stale_seconds=90)

        assert result.returncode == 1
        assert "stale" in result.stderr.lower()

    def test_fail_future_heartbeat_timestamp(self, tmp_path: Path) -> None:
        heartbeat_path = str(tmp_path / "heartbeat.json")
        write_heartbeat(
            heartbeat_path,
            updated_at_unix=time.time() + 120,
            mode="kis",
            is_running=True,
            kis_connected=True,
        )

        result = run_healthcheck(heartbeat_path, expect_mode="kis")

        assert result.returncode == 1
        assert "future" in result.stderr.lower()

    def test_pass_small_future_skew_within_tolerance(self, tmp_path: Path) -> None:
        heartbeat_path = str(tmp_path / "heartbeat.json")
        write_heartbeat(
            heartbeat_path,
            updated_at_unix=time.time() + 0.5,
            mode="kis",
            is_running=True,
            kis_connected=True,
        )

        result = run_healthcheck(heartbeat_path, expect_mode="kis")

        assert result.returncode == 0

    def test_fail_not_running(self, tmp_path: Path) -> None:
        """Should fail when is_running is False."""
        heartbeat_path = str(tmp_path / "heartbeat.json")
        write_heartbeat(
            heartbeat_path,
            mode="kis",
            is_running=False,
            kis_connected=True,
        )

        result = run_healthcheck(heartbeat_path, expect_mode="kis")

        assert result.returncode == 1
        assert "not running" in result.stderr.lower()

    def test_fail_mode_mismatch(self, tmp_path: Path) -> None:
        """Should fail when mode doesn't match expected."""
        heartbeat_path = str(tmp_path / "heartbeat.json")
        write_heartbeat(
            heartbeat_path,
            mode="upbit",
            is_running=True,
            upbit_connected=True,
            kis_connected="n/a",
        )

        result = run_healthcheck(heartbeat_path, expect_mode="kis")

        assert result.returncode == 1
        assert "mode" in result.stderr.lower()

    def test_fail_not_connected(self, tmp_path: Path) -> None:
        """Should fail when expected connection is not established."""
        heartbeat_path = str(tmp_path / "heartbeat.json")
        write_heartbeat(
            heartbeat_path,
            mode="kis",
            is_running=True,
            kis_connected=False,
        )

        result = run_healthcheck(heartbeat_path, expect_mode="kis")

        assert result.returncode == 1
        assert "not connected" in result.stderr.lower()

    def test_fail_invalid_json(self, tmp_path: Path) -> None:
        """Should fail when heartbeat file has invalid JSON."""
        heartbeat_path = str(tmp_path / "heartbeat.json")
        Path(heartbeat_path).parent.mkdir(parents=True, exist_ok=True)
        with open(heartbeat_path, "w") as f:
            f.write("{ invalid json }")

        result = run_healthcheck(heartbeat_path, expect_mode="kis")

        assert result.returncode == 1
        assert "parse" in result.stderr.lower() or "json" in result.stderr.lower()

    def test_pass_custom_stale_threshold(self, tmp_path: Path) -> None:
        """Should pass with custom stale threshold."""
        heartbeat_path = str(tmp_path / "heartbeat.json")
        # 50 seconds old, stale threshold 60
        write_heartbeat(
            heartbeat_path,
            updated_at_unix=time.time() - 50,
            mode="kis",
            is_running=True,
            kis_connected=True,
        )

        result = run_healthcheck(heartbeat_path, expect_mode="kis", stale_seconds=60)

        assert result.returncode == 0

    def test_pass_both_mode_checks_relevant_connection(self, tmp_path: Path) -> None:
        """For mode=both, should check both connections."""
        heartbeat_path = str(tmp_path / "heartbeat.json")
        write_heartbeat(
            heartbeat_path,
            mode="both",
            is_running=True,
            upbit_connected=True,
            kis_connected=True,
        )

        # When expecting both, both should be connected
        result = run_healthcheck(heartbeat_path, expect_mode="both")
        assert result.returncode == 0

    def test_fail_both_mode_one_disconnected(self, tmp_path: Path) -> None:
        """For mode=both, should fail if either is disconnected."""
        heartbeat_path = str(tmp_path / "heartbeat.json")
        write_heartbeat(
            heartbeat_path,
            mode="both",
            is_running=True,
            upbit_connected=True,
            kis_connected=False,
        )

        result = run_healthcheck(heartbeat_path, expect_mode="both")
        assert result.returncode == 1

    def test_pass_default_expect_mode_when_heartbeat_mode_is_both(
        self, tmp_path: Path
    ) -> None:
        heartbeat_path = str(tmp_path / "heartbeat.json")
        write_heartbeat(
            heartbeat_path,
            mode="both",
            is_running=True,
            upbit_connected=True,
            kis_connected=True,
        )

        result = run_healthcheck(heartbeat_path, expect_mode=None)
        assert result.returncode == 0, f"Expected pass, got: {result.stderr}"
