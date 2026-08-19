from __future__ import annotations

import asyncio
import importlib.util
import os
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
EQUALITY_GATE_PATH = REPOSITORY_ROOT / "research" / "kr_backfill" / "equality_gate.py"


@pytest.fixture
def equality_gate_module() -> ModuleType:
    module_name = "kr_backfill_equality_gate_build_clients_test"
    sys.path.insert(0, str(EQUALITY_GATE_PATH.parent))
    try:
        spec = importlib.util.spec_from_file_location(module_name, EQUALITY_GATE_PATH)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
        yield module
    finally:
        sys.modules.pop(module_name, None)
        sys.path.remove(str(EQUALITY_GATE_PATH.parent))


def _minimal_subprocess_env() -> dict[str, str]:
    env = {
        key: os.environ[key]
        for key in ("HOME", "LANG", "LC_ALL", "PATH", "TMPDIR")
        if key in os.environ
    }
    env.update(
        {
            # Settings otherwise reads the developer checkout's .env even
            # though this subprocess deliberately supplies a minimal env.
            # Point dotenv loading at an empty OS file so "without env" is a
            # stable test condition on configured workstations too.
            "ENV_FILE": os.devnull,
            "KIWOOM_MOCK_APP_KEY": "test-kiwoom-key",
            "KIWOOM_MOCK_APP_SECRET": "test-kiwoom-secret",
        }
    )
    return env


def _run_build_clients(source_expression: str) -> subprocess.CompletedProcess[str]:
    script = f"""
import asyncio
import sys
sys.path.insert(0, {str(EQUALITY_GATE_PATH.parent)!r})
from equality_gate import build_clients

async def main():
    try:
        clients = await build_clients({source_expression})
    except Exception as exc:
        print(f"FAIL_CLOSED={{type(exc).__name__}}")
        return 17
    print("CLIENTS=" + ",".join(sorted(clients)))
    print("KIS_IMPORTED=" + str("app.services.brokers.kis.client" in sys.modules))
    print("TOSS_IMPORTED=" + str("app.services.brokers.toss.client" in sys.modules))
    print("SETTINGS_IMPORTED=" + str("app.core.config" in sys.modules))
    return 0

raise SystemExit(asyncio.run(main()))
"""
    return subprocess.run(
        [sys.executable, "-c", script],
        cwd=REPOSITORY_ROOT,
        env=_minimal_subprocess_env(),
        check=False,
        capture_output=True,
        text=True,
    )


def test_kiwoom_only_does_not_import_other_providers_or_settings() -> None:
    result = _run_build_clients("['kiwoom']")

    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.splitlines() == [
        "CLIENTS=kiwoom",
        "KIS_IMPORTED=False",
        "TOSS_IMPORTED=False",
        "SETTINGS_IMPORTED=False",
    ]


@pytest.mark.parametrize("sources", ["['kis']", "['toss']", "['kiwoom', 'kis']"])
def test_selected_configured_providers_still_fail_closed_without_env(
    sources: str,
) -> None:
    result = _run_build_clients(sources)

    assert result.returncode == 17, result.stdout + result.stderr
    assert result.stdout.startswith("FAIL_CLOSED=ValidationError")


@pytest.mark.asyncio
async def test_default_callsite_still_requests_all_sources(
    equality_gate_module: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[str] = []

    class _Kiwoom:
        def __init__(self, **kwargs: object) -> None:
            del kwargs
            seen.append("kiwoom")

    class _KIS:
        def __init__(self, *, is_mock: bool) -> None:
            assert is_mock is True
            seen.append("kis")

    class _Toss:
        @classmethod
        def from_settings(cls) -> _Toss:
            seen.append("toss")
            return cls()

    fake_modules = {
        "app.services.brokers.kiwoom.constants": ModuleType("constants"),
        "app.services.brokers.kiwoom.client": ModuleType("kiwoom_client"),
        "app.services.brokers.kis.client": ModuleType("kis_client"),
        "app.services.brokers.toss.client": ModuleType("toss_client"),
    }
    fake_modules[
        "app.services.brokers.kiwoom.constants"
    ].MOCK_BASE_URL = "https://mockapi.kiwoom.com"
    fake_modules["app.services.brokers.kiwoom.client"].KiwoomMockClient = _Kiwoom
    fake_modules["app.services.brokers.kis.client"].KISClient = _KIS
    fake_modules["app.services.brokers.toss.client"].TossReadClient = _Toss
    for name, module in fake_modules.items():
        monkeypatch.setitem(sys.modules, name, module)
    monkeypatch.setenv("KIWOOM_MOCK_APP_KEY", "test-key")
    monkeypatch.setenv("KIWOOM_MOCK_APP_SECRET", "test-secret")

    clients = await equality_gate_module.build_clients()

    assert set(clients) == {"kiwoom", "kis", "toss"}
    assert seen == ["kiwoom", "kis", "toss"]


@pytest.mark.parametrize("sources", [[], ["unknown"]])
def test_invalid_source_selection_fails_before_provider_imports(
    equality_gate_module: ModuleType, sources: list[str]
) -> None:
    with pytest.raises(ValueError):
        asyncio.run(equality_gate_module.build_clients(sources))
