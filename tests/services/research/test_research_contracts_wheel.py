"""The built wheel ships the research contracts and imports without the checkout.

The child runs with normal Python startup so the ROB-1296 socket guard's
``sitecustomize`` hook still installs -- ``-I`` would drop it, and the guard
rejects an isolated interpreter outright. Isolation is instead achieved *inside*
the child: the guard puts ``PROJECT_ROOT`` on ``PYTHONPATH``, so the script
strips every checkout-derived entry from ``sys.path`` before importing anything,
leaving the wheel plus the standard library and site-packages.

That ordering matters. Merely asserting a handful of ``__file__`` values, with
the checkout still importable, is weaker than the ``-I`` it replaced: a module
missing from the wheel resolves from the source tree and the assertion never
fires for the modules you forgot to name. ``test_checkout_fallback_is_what_the_
path_scrub_prevents`` pins exactly that difference against a wheel with a member
deliberately removed.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
PINNED_DIGEST = "ba383d20d8aa8fb134ca475b1439329e97ac400f91ea957db0484deaa7df8854"

REQUIRED_CONTRACT_MEMBERS = (
    "research_contracts/canonical_hash.py",
    "research_contracts/evaluation_windows.py",
    "research_contracts/frozen_config.py",
    "research_contracts/honest_offline_gate.py",
    "research_contracts/jsonb_numbers.py",
    "research_contracts/trial_evidence.py",
)

# Strips the checkout from ``sys.path``, then proves every first-party module
# that ended up loaded came out of the wheel.
_PROBE = f"""
import os
import sys

wheel = sys.argv[1]
scrub = sys.argv[2] == "scrub"
project_root = os.path.realpath(sys.argv[3])

if scrub:
    kept = []
    for entry in sys.path:
        resolved = os.path.realpath(entry or os.getcwd())
        # The virtualenv lives *inside* the checkout, so "under project_root" on
        # its own would also throw away site-packages and the wheel would be
        # tested against a crippled interpreter. Installed distributions are
        # explicitly preserved; only importable checkout source is removed --
        # the project root itself, the guard's startup-hook directory, and the
        # implicit cwd entry.
        is_installed_tree = (
            "site-packages" in resolved.split(os.sep)
            or "dist-packages" in resolved.split(os.sep)
        )
        under_checkout = resolved == project_root or resolved.startswith(
            project_root + os.sep
        )
        if under_checkout and not is_installed_tree:
            continue
        kept.append(entry)
    sys.path[:] = kept

    # Prove the scrub actually did its job before drawing conclusions from it.
    for entry in sys.path:
        resolved = os.path.realpath(entry or os.getcwd())
        if resolved == project_root:
            raise SystemExit("path scrub failed: checkout still importable")

sys.path.insert(0, wheel)

from app.schemas.research_backtest import StrategyExperimentIdentity
from app.services import research_offline_gate_service
from app.services.research_canonical_hash import canonical_sha256

import research_contracts.canonical_hash  # noqa: F401
import research_contracts.evaluation_windows  # noqa: F401
import research_contracts.frozen_config  # noqa: F401
import research_contracts.honest_offline_gate  # noqa: F401
import research_contracts.jsonb_numbers  # noqa: F401
import research_contracts.trial_evidence  # noqa: F401

assert StrategyExperimentIdentity
assert research_offline_gate_service.finalize_offline_gate
assert canonical_sha256({{'b': 2, 'a': 1}}) == {PINNED_DIGEST!r}

# Sweep *everything* first-party that got loaded, not a hand-picked few -- the
# transitive imports are exactly where a checkout fallback would hide.
offenders = []
namespace_packages = []
for name, module in sorted(sys.modules.items()):
    if not (name == "app" or name.startswith("app.")
            or name == "research_contracts" or name.startswith("research_contracts.")):
        continue
    origin = getattr(module, "__file__", None)
    if origin is None:
        # A namespace package (or a builtin) has no file of its own. Its
        # submodules are checked on their own terms, so record and move on
        # rather than silently skipping.
        namespace_packages.append(name)
        continue
    if not os.path.realpath(origin).startswith(os.path.realpath(wheel)):
        offenders.append((name, origin))

if offenders:
    raise SystemExit("resolved outside the wheel: " + repr(offenders))

print("checked=%d namespace=%d" % (
    len([n for n in sys.modules if n.split('.')[0] in ('app', 'research_contracts')]),
    len(namespace_packages),
))
"""


def _build_wheel(tmp_path: Path) -> Path:
    output = tmp_path / "dist"
    subprocess.run(
        ["uv", "build", "--wheel", "--out-dir", str(output)],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return next(output.glob("*.whl"))


def _run_probe(
    wheel: Path, tmp_path: Path, *, scrub: bool
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            "-c",
            _PROBE,
            str(wheel),
            "scrub" if scrub else "inherit",
            str(ROOT),
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )


def _wheel_without(wheel: Path, tmp_path: Path, member: str) -> Path:
    """A copy of *wheel* with a member (or whole directory) removed."""

    mutated = tmp_path / f"mutated-{wheel.name}"
    with zipfile.ZipFile(wheel) as source, zipfile.ZipFile(mutated, "w") as target:
        for item in source.infolist():
            if item.filename == member or item.filename.startswith(member):
                continue
            target.writestr(item, source.read(item.filename))
    return mutated


@pytest.mark.integration
def test_built_wheel_ships_small_research_contract_and_clean_imports(
    tmp_path: Path,
) -> None:
    wheel = _build_wheel(tmp_path)

    with zipfile.ZipFile(wheel) as archive:
        names = set(archive.namelist())
    for member in REQUIRED_CONTRACT_MEMBERS:
        assert member in names
    assert not any(name.startswith("research/nautilus_scalping/") for name in names)

    result = _run_probe(wheel, tmp_path, scrub=True)

    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.startswith("checked="), result.stdout


@pytest.mark.integration
def test_checkout_fallback_is_what_the_path_scrub_prevents(tmp_path: Path) -> None:
    """Mutation control: a wheel missing a module must not pass by accident.

    With the checkout scrubbed the missing module is a hard import failure. With
    it left on ``sys.path`` the very same wheel imports cleanly -- from the source
    tree -- which is the false green the old ``__file__``-only check allowed.
    """

    wheel = _build_wheel(tmp_path)
    # The whole top-level package, not a single submodule: a regular package's
    # ``__path__`` is pinned to the distribution that provided it, so dropping
    # one file can never fall back to the checkout. Losing the package outright
    # is the packaging slip that can.
    mutated = _wheel_without(wheel, tmp_path, "research_contracts/")

    scrubbed = _run_probe(mutated, tmp_path, scrub=True)
    assert scrubbed.returncode != 0, scrubbed.stdout
    combined = scrubbed.stdout + scrubbed.stderr
    assert (
        "ModuleNotFoundError" in combined or "resolved outside the wheel" in combined
    ), combined

    inherited = _run_probe(mutated, tmp_path, scrub=False)
    assert inherited.returncode != 0, (
        "the un-scrubbed run was expected to resolve the missing module from the "
        "checkout and be caught by the provenance sweep"
    )
    assert "resolved outside the wheel" in inherited.stdout + inherited.stderr


@pytest.mark.integration
def test_probe_child_keeps_the_socket_guard_installed(tmp_path: Path) -> None:
    """The scrub must not cost the child its guard.

    ``sitecustomize`` runs before the script, so stripping the checkout
    afterwards leaves the already-imported guard in place -- which is the whole
    reason this is done inside the child rather than with ``-I``.
    """

    _ = tmp_path
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import os, sys\n"
            "from tests._socket_guard import is_installed\n"
            "root = os.path.realpath(sys.argv[1])\n"
            "sys.path[:] = [p for p in sys.path\n"
            "               if not os.path.realpath(p or os.getcwd()).startswith(root)]\n"
            "print(is_installed())\n",
            str(ROOT),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.strip() == "True"


def test_uv_is_available_for_the_wheel_build() -> None:
    """Guards against a silent skip: these tests are meaningless without it."""

    assert shutil.which("uv") is not None
