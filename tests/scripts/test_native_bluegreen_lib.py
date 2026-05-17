"""ROB-259 color detection/switch helpers."""

from __future__ import annotations

import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
LIB = REPO_ROOT / "ops" / "native" / "scripts" / "native_bluegreen_lib.sh"


def _bash(snippet: str, base: Path) -> subprocess.CompletedProcess:
    """Source the lib then evaluate snippet with AUTO_TRADER_BASE pointing at base."""
    script = f'set -Eeuo pipefail\nexport AUTO_TRADER_BASE="{base}"\nsource "{LIB}"\n{snippet}\n'
    return subprocess.run(
        ["bash", "-c", script], check=False, capture_output=True, text=True
    )


def test_detect_active_color_defaults_blue(tmp_path: Path) -> None:
    (tmp_path / "shared").mkdir()
    proc = _bash("detect_active_color api", tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "blue"


def test_detect_active_color_reads_file(tmp_path: Path) -> None:
    shared = tmp_path / "shared"
    shared.mkdir()
    (shared / "api-active-color").write_text("green\n")
    proc = _bash("detect_active_color api", tmp_path)
    assert proc.stdout.strip() == "green"


def test_detect_active_color_rejects_garbage(tmp_path: Path) -> None:
    shared = tmp_path / "shared"
    shared.mkdir()
    (shared / "api-active-color").write_text("purple\n")
    proc = _bash("detect_active_color api", tmp_path)
    assert proc.returncode != 0
    assert "invalid" in proc.stderr.lower()


def test_inactive_color_inverts(tmp_path: Path) -> None:
    proc = _bash("inactive_color blue && inactive_color green", tmp_path)
    assert proc.returncode == 0
    lines = proc.stdout.strip().splitlines()
    assert lines == ["green", "blue"]


def test_set_active_color_atomic(tmp_path: Path) -> None:
    (tmp_path / "shared").mkdir()
    proc = _bash("set_active_color api green", tmp_path)
    assert proc.returncode == 0
    assert (tmp_path / "shared" / "api-active-color").read_text().strip() == "green"


def test_color_port_lookup(tmp_path: Path) -> None:
    proc = _bash(
        "color_port api blue; color_port api green; color_port mcp blue; color_port mcp green",
        tmp_path,
    )
    assert proc.returncode == 0
    assert proc.stdout.strip().splitlines() == ["8001", "8002", "8766", "8767"]


def test_color_port_invalid_service(tmp_path: Path) -> None:
    proc = _bash("color_port worker blue", tmp_path)
    assert proc.returncode != 0


def test_color_label(tmp_path: Path) -> None:
    proc = _bash("color_label api blue; color_label mcp green", tmp_path)
    assert proc.returncode == 0
    assert proc.stdout.strip().splitlines() == [
        "com.robinco.auto-trader.api-blue",
        "com.robinco.auto-trader.mcp-green",
    ]
