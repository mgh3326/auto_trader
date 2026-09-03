"""Mac native deployment is permanently fail-closed."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
RETIRED = (
    REPO_ROOT / "scripts" / "deploy-native.sh",
    REPO_ROOT / "ops" / "native" / "scripts" / "native_deploy_lib.sh",
)
RETIREMENT_MESSAGE = (
    "native deployment retired 2026-09-03; serving runs on NCP — "
    "see docs/runbooks/ncp-pull-deploy.md"
)


@pytest.mark.parametrize("arguments", ((), ("sha-deadbeef",), ("--rollback",)))
@pytest.mark.parametrize("script", RETIRED)
def test_native_deployment_entrypoints_fail_closed_without_side_effects(
    tmp_path: Path, arguments: tuple[str, ...], script: Path
) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    calls = tmp_path / "side-effects"
    for command in ("launchctl", "rsync"):
        stub = bin_dir / command
        stub.write_text(f"#!/usr/bin/env bash\nprintf '%s\\n' {command} >> {calls}\n")
        stub.chmod(0o755)

    proc = subprocess.run(
        ["bash", str(script), *arguments],
        check=False,
        capture_output=True,
        text=True,
        env={"PATH": f"{bin_dir}:{os.environ['PATH']}"},
    )

    assert proc.returncode == 70
    assert RETIREMENT_MESSAGE in proc.stderr
    assert not calls.exists(), proc.stdout + proc.stderr


def test_native_launchd_bundle_is_absent() -> None:
    assert not (REPO_ROOT / "ops" / "native" / "plists").exists()
    assert sorted((REPO_ROOT / "ops" / "native" / "scripts").iterdir()) == [
        REPO_ROOT / "ops" / "native" / "scripts" / "native_deploy_lib.sh"
    ]
