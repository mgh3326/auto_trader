"""Native API wrapper smoke tests (no server start)."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
RUN_API = REPO_ROOT / "ops" / "native" / "scripts" / "run-api.sh"


def _run_api(
    tmp_path: Path, color: str, port_env: dict[str, str]
) -> subprocess.CompletedProcess[str]:
    base = tmp_path / "services" / "auto_trader"
    (base / f"current-{color}").mkdir(parents=True)
    (base / "shared").mkdir()
    (base / "shared" / ".env.prod.native").write_text("# test env\n")
    (base / "scripts").mkdir()
    (base / "scripts" / "common.sh").write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        'cd "${AUTO_TRADER_CURRENT}"\n'
        "_export_selected_env_prefixes() { :; }\n"
    )
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "uv").write_text(
        '#!/usr/bin/env bash\nprintf "argv=%s\\nPWD=%s\\n" "$*" "$PWD"\n'
    )
    (bin_dir / "uv").chmod(0o755)
    return subprocess.run(
        ["bash", str(RUN_API)],
        check=False,
        capture_output=True,
        text=True,
        env={
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "HOME": str(tmp_path),
            "AUTO_TRADER_BASE": str(base),
            "AUTO_TRADER_COLOR": color,
            **port_env,
        },
    )


def test_run_api_uses_explicit_port_and_color_current(tmp_path: Path) -> None:
    proc = _run_api(tmp_path, "green", {"AUTO_TRADER_API_PORT": "8002"})
    assert proc.returncode == 0, proc.stderr
    assert "--port 8002" in proc.stdout
    assert "current-green" in proc.stdout


def test_run_api_rejects_invalid_color(tmp_path: Path) -> None:
    proc = _run_api(tmp_path, "purple", {})
    assert proc.returncode != 0
