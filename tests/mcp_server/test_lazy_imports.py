"""ROB-1436: low-traffic MCP units defer heavy third-party imports.

The placeholder tests run on a throwaway package so they never disturb the
real pandas in this test process. The boot test runs the real registrars in a
child interpreter, the way ``app.mcp_server.main`` does, and fails when a
module-level access (a ``from pandas import X``, an eagerly evaluated
``pd.DataFrame`` annotation, a ``pd.Timestamp(...)`` constant) starts loading
a deferred package at boot again.
"""

from __future__ import annotations

import importlib
import json
import os
import subprocess
import sys
import textwrap
from collections.abc import Iterator
from pathlib import Path

import pytest

from app.mcp_server import lazy_imports
from app.mcp_server.profiles import McpProfile

pytestmark = pytest.mark.unit
REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def probe_package(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    package = tmp_path / "rob1436_lazyprobe"
    (package / "sub").mkdir(parents=True)
    (package / "__init__.py").write_text(
        "import builtins\n"
        "builtins.ROB1436_PROBE_RUNS = getattr(builtins, 'ROB1436_PROBE_RUNS', 0) + 1\n"
        "VALUE = 42\n"
        "def double(x):\n"
        "    return 2 * x\n"
    )
    (package / "sub" / "__init__.py").write_text("NAME = 'sub'\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    import builtins

    monkeypatch.setattr(builtins, "ROB1436_PROBE_RUNS", 0, raising=False)
    yield "rob1436_lazyprobe"
    for name in [m for m in sys.modules if m.startswith("rob1436_lazyprobe")]:
        del sys.modules[name]


def _runs() -> int:
    import builtins

    return builtins.ROB1436_PROBE_RUNS  # type: ignore[attr-defined]


def test_import_statements_do_not_run_the_package_until_first_use(
    probe_package: str,
) -> None:
    assert lazy_imports._install_one(probe_package) is True
    first = importlib.import_module(probe_package)
    second = importlib.import_module(probe_package)  # a second importer
    assert first is second
    assert lazy_imports.is_deferred(probe_package)
    assert _runs() == 0

    assert first.double(21) == first.VALUE == 42  # first real use
    assert _runs() == 1
    assert not lazy_imports.is_deferred(probe_package)
    real = sys.modules[probe_package]
    assert real is not first and real.VALUE == 42
    assert importlib.import_module(probe_package) is real
    first.VALUE  # noqa: B018 - later access is forwarded, never re-run
    assert _runs() == 1


def test_submodule_import_and_from_import_load_the_real_package(
    probe_package: str,
) -> None:
    assert lazy_imports._install_one(probe_package)
    sub = importlib.import_module(f"{probe_package}.sub")
    assert sub.NAME == "sub"
    assert _runs() == 1
    namespace: dict[str, object] = {}
    exec(f"from {probe_package} import VALUE", namespace)
    assert namespace["VALUE"] == 42
    assert _runs() == 1


def test_already_imported_or_missing_packages_are_left_alone(
    probe_package: str,
) -> None:
    importlib.import_module(probe_package)
    assert lazy_imports._install_one(probe_package) is False
    assert lazy_imports._install_one("rob1436_no_such_package") is False
    assert "rob1436_no_such_package" not in sys.modules


@pytest.mark.parametrize(
    "profile",
    [p for p in McpProfile if p not in lazy_imports.LAZY_IMPORT_PROFILES],
)
def test_other_profiles_stay_eager(profile: McpProfile) -> None:
    assert lazy_imports.install_lazy_imports(profile) == ()


def test_lazy_profiles_are_the_low_traffic_units() -> None:
    assert lazy_imports.LAZY_IMPORT_PROFILES == {
        McpProfile.KIWOOM,
        McpProfile.KIWOOM_KR,
        McpProfile.H3_CRYPTO_PAPER,
        McpProfile.H3_US_PAPER,
    }
    assert McpProfile.DEFAULT not in lazy_imports.LAZY_IMPORT_PROFILES


_BOOT_CHILD = textwrap.dedent(
    """
    import json, sys
    from typing import Any, cast

    from app.mcp_server import lazy_imports
    from app.mcp_server.profiles import McpProfile

    class Recorder:
        def __init__(self):
            self.tools = {}

        def tool(self, *args, **kwargs):
            def register(function):
                self.tools[kwargs.get("name") or function.__name__] = function
                return function

            return register(args[0]) if args and callable(args[0]) else register

    lazy_imports.install_lazy_imports(McpProfile.KIWOOM)
    from app.mcp_server.tooling import register_all_tools

    counts = {}
    for profile in sorted(lazy_imports.LAZY_IMPORT_PROFILES):
        recorder = Recorder()
        register_all_tools(cast(Any, recorder), profile=profile)
        counts[profile.value] = len(recorder.tools)
    print(json.dumps({
        "counts": counts,
        "executed": sorted(
            name for name in lazy_imports.LAZY_MODULES
            if name in sys.modules and not lazy_imports.is_deferred(name)
        ),
    }))
    """
)


def test_registering_every_lazy_profile_leaves_heavy_packages_unexecuted() -> None:
    env = {
        key: value
        for key, value in os.environ.items()
        if key
        in {
            "PATH",
            "HOME",
            "TMPDIR",
            "KIS_APP_KEY",
            "KIS_APP_SECRET",
            "TELEGRAM_TOKEN",
            "OPENDART_API_KEY",
            "UPBIT_ACCESS_KEY",
            "UPBIT_SECRET_KEY",
            "DATABASE_URL",
            "REDIS_URL",
            "SECRET_KEY",
            "ENVIRONMENT",
        }
    }
    env["SENTRY_DSN"] = ""
    completed = subprocess.run(
        [sys.executable, "-c", _BOOT_CHILD],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=900,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr[-3000:]
    result = json.loads(completed.stdout.strip().splitlines()[-1])
    assert all(count > 0 for count in result["counts"].values()), result
    assert result["executed"] == [], (
        "a module-level access loads these packages at boot on the lazy "
        f"profiles again: {result['executed']} (see app/mcp_server/lazy_imports.py)"
    )
