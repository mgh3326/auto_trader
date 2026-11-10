"""Interpreter selection for the exact-SHA deploy gate (hk 3862).

NCP's default python3 is 3.6, which cannot even parse deploy_ci_gate.py. The
deploy script must pick a Python >= 3.11 (explicit path, then uv, then python3)
and refuse with exit 78 rather than run the gate on an older interpreter.
"""

from __future__ import annotations

import stat
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "deploy-ncp-pull.sh"


def _gate_python_source() -> str:
    text = SCRIPT.read_text()
    start = text.index("GATE_PYTHON_MIN=")
    end = text.index("verified_deploy_image() {")
    return text[start:end]


def _exec(path: Path, body: str) -> Path:
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def _run(
    tmp_path: Path, *, env_extra: dict[str, str], bindir: Path
) -> subprocess.CompletedProcess[str]:
    snippet = tmp_path / "snippet.sh"
    snippet.write_text(
        _gate_python_source()
        + '\nGATE_PYTHON=()\ngate_python; rc=$?\nprintf "%s\\n" "${GATE_PYTHON[*]}"\nexit $rc\n'
    )
    env = {
        "PATH": f"{bindir}:/usr/bin:/bin",
        "HOME": str(tmp_path / "home"),
        **env_extra,
    }
    return subprocess.run(
        ["bash", str(snippet)], env=env, capture_output=True, text=True, check=False
    )


def _old_python(path: Path) -> Path:
    # Answers the version probe like Python 3.6 would: not >= 3.11.
    return _exec(path, "#!/bin/sh\nexit 1\n")


def test_explicit_interpreter_wins(tmp_path: Path) -> None:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    result = _run(
        tmp_path, env_extra={"AT_DEPLOY_GATE_PYTHON": sys.executable}, bindir=bindir
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == sys.executable


def test_old_explicit_interpreter_refuses_78(tmp_path: Path) -> None:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    old = _old_python(tmp_path / "py36")
    result = _run(
        tmp_path, env_extra={"AT_DEPLOY_GATE_PYTHON": str(old)}, bindir=bindir
    )
    assert result.returncode == 78
    assert "requires Python >= 3.11" in result.stderr


def test_uv_in_home_is_used_when_not_on_path(tmp_path: Path) -> None:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    _old_python(bindir / "python3")
    uv = tmp_path / "home" / ".local" / "bin" / "uv"
    uv.parent.mkdir(parents=True)
    log = tmp_path / "uv.log"
    _exec(
        uv,
        f'#!/bin/sh\nprintf "%s\\n" "$*" >> {log}\n'
        # drop "run --quiet --no-project --no-python-downloads --python >=3.11 python"
        f'shift 7\nexec {sys.executable} "$@"\n',
    )
    if any((Path(p) / "uv").exists() for p in ("/usr/bin", "/bin")):
        return  # a system uv would be found first; the PATH case is covered below
    result = _run(tmp_path, env_extra={}, bindir=bindir)
    assert result.returncode == 0, result.stderr
    assert result.stdout.split()[0] == str(uv)
    assert "--no-python-downloads --python >=3.11 python" in log.read_text()


def test_old_python3_without_uv_refuses_78(tmp_path: Path) -> None:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    _old_python(bindir / "python3")
    if any((Path(p) / "uv").exists() for p in ("/usr/bin", "/bin")):
        return  # a system uv would be chosen before python3
    result = _run(tmp_path, env_extra={}, bindir=bindir)
    # bindir is first on PATH, so the old python3 is chosen and must refuse.
    assert result.returncode == 78, (result.stdout, result.stderr)
    assert "requires Python >= 3.11" in result.stderr
