from __future__ import annotations

import subprocess
from pathlib import Path

RUNNER = Path("ops/ncp/bin/at-job.sh")


def _run(
    tmp_path: Path, docker_body: str, *, flock_body: str = "exit 0"
) -> subprocess.CompletedProcess[str]:
    fakebin = tmp_path / "bin"
    fakebin.mkdir()
    (fakebin / "docker").write_text("#!/usr/bin/env bash\n" + docker_body)
    (fakebin / "flock").write_text("#!/usr/bin/env bash\n" + flock_body)
    (fakebin / "timeout").write_text('#!/usr/bin/env bash\nshift 2\n"$@"')
    for command in fakebin.iterdir():
        command.chmod(0o755)
    # Do not create real deployment files; the wrapper stops at the tested
    # digest boundary before it checks them.
    return subprocess.run(
        ["/bin/bash", str(RUNNER), "scripts.sync_toss_warnings"],
        text=True,
        capture_output=True,
        env={
            "PATH": f"{fakebin}:/usr/bin:/bin",
            "AT_JOB_LOCK_DIRECTORY": str(tmp_path / "locks"),
        },
        check=False,
    )


def test_digest_failure_does_not_fall_back_to_main(tmp_path: Path) -> None:
    result = _run(
        tmp_path, "#!/usr/bin/env bash\necho ghcr.io/mgh3326/auto_trader:main"
    )
    assert result.returncode == 78
    assert ":main" not in result.stdout


def test_flock_duplicate_is_refused(tmp_path: Path) -> None:
    result = _run(tmp_path, "#!/usr/bin/env bash\nexit 99", flock_body="exit 1")
    assert result.returncode == 75
    assert '"rc":75' in result.stdout
