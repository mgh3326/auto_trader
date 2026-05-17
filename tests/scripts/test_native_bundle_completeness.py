"""ROB-259 review fix: ensure ops/native/ is self-contained.

For every plist under ops/native/plists/, the ProgramArguments[0] script must
either exist in the repo's ops/native/scripts/ bundle (when the path points at
`<base>/scripts/...`) or be an external binary (e.g. /opt/homebrew/bin/haproxy)
that is handled separately (see test_haproxy_binary_consistency.py).

We also verify that every run-* script in ops/native/scripts/ that sources
common.sh has a matching common.sh in the bundle, so a fresh server bootstrap
does not depend on hand-copied files from a prior install.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
PLIST_DIR = REPO_ROOT / "ops" / "native" / "plists"
SCRIPTS_DIR = REPO_ROOT / "ops" / "native" / "scripts"

PROD_SCRIPTS_PREFIX = "/Users/mgh3326/services/auto_trader/scripts/"


def _program_argv(plist_path: Path) -> list[str]:
    tree = ET.parse(plist_path)
    root = tree.getroot()
    # Find the ProgramArguments array
    pa_array = None
    found_key = False
    for child in root.iter():
        if child.tag == "key" and child.text == "ProgramArguments":
            found_key = True
            continue
        if found_key and child.tag == "array":
            pa_array = child
            break
    assert pa_array is not None, f"{plist_path.name}: no ProgramArguments array"
    return [el.text or "" for el in pa_array.findall("string")]


@pytest.mark.parametrize("plist_path", sorted(PLIST_DIR.glob("*.plist")))
def test_plist_script_arg_is_in_bundle(plist_path: Path) -> None:
    """Every plist that runs a script under $BASE/scripts/ must ship that script in the repo."""
    argv = _program_argv(plist_path)
    assert argv, f"{plist_path.name}: empty ProgramArguments"
    script_path = argv[0]
    if not script_path.startswith(PROD_SCRIPTS_PREFIX):
        # External binary (e.g. /opt/homebrew/bin/haproxy) — out of scope for this test.
        return
    basename = script_path[len(PROD_SCRIPTS_PREFIX) :]
    bundled = SCRIPTS_DIR / basename
    assert bundled.is_file(), (
        f"{plist_path.name} references {script_path}, but the bundle is missing "
        f"{bundled}. Add the wrapper to ops/native/scripts/ so fresh server "
        f"bootstrap is self-contained."
    )


def test_common_sh_in_bundle() -> None:
    """common.sh must be shipped because every run-* wrapper sources it."""
    assert (SCRIPTS_DIR / "common.sh").is_file()


@pytest.mark.parametrize(
    "wrapper",
    sorted(p for p in SCRIPTS_DIR.glob("run-*.sh")),
)
def test_run_wrapper_sources_common_sh(wrapper: Path) -> None:
    """Every run-* wrapper sources common.sh from the deployed scripts dir."""
    body = wrapper.read_text()
    assert re.search(r"source\s+\".*?/scripts/common\.sh\"", body), (
        f"{wrapper.name} does not source common.sh; expected `source "
        f'"$AUTO_TRADER_BASE/scripts/common.sh"` so the cd/env bootstrap runs.'
    )


@pytest.mark.parametrize(
    "wrapper",
    sorted(p for p in SCRIPTS_DIR.glob("run-*.sh")),
)
def test_run_wrapper_is_executable(wrapper: Path) -> None:
    """Wrappers must be executable so launchd can run them directly."""
    assert wrapper.stat().st_mode & 0o111, (
        f"{wrapper.name} is not executable; launchd will fail to start the service."
    )


def test_required_single_active_wrappers_present() -> None:
    """The 4 single-active wrappers (worker/scheduler/websockets) must be in the bundle."""
    required = {
        "run-worker.sh",
        "run-scheduler.sh",
        "run-websocket-kis.sh",
        "run-websocket-upbit.sh",
    }
    present = {p.name for p in SCRIPTS_DIR.glob("run-*.sh")}
    missing = required - present
    assert not missing, f"missing single-active wrappers in bundle: {sorted(missing)}"
