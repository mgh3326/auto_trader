from __future__ import annotations

import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
PINNED_DIGEST = "ba383d20d8aa8fb134ca475b1439329e97ac400f91ea957db0484deaa7df8854"


@pytest.mark.integration
def test_built_wheel_ships_small_research_contract_and_clean_imports(
    tmp_path: Path,
) -> None:
    output = tmp_path / "dist"
    subprocess.run(
        ["uv", "build", "--wheel", "--out-dir", str(output)],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    wheel = next(output.glob("*.whl"))
    with zipfile.ZipFile(wheel) as archive:
        names = set(archive.namelist())
    assert "research_contracts/canonical_hash.py" in names
    assert "research_contracts/evaluation_windows.py" in names
    assert "research_contracts/frozen_config.py" in names
    assert "research_contracts/honest_offline_gate.py" in names
    assert "research_contracts/jsonb_numbers.py" in names
    assert "research_contracts/trial_evidence.py" in names
    assert not any(name.startswith("research/nautilus_scalping/") for name in names)

    # Provenance is asserted explicitly rather than by isolating the child with
    # ``-I``. An isolated interpreter also drops the hermetic-test socket guard's
    # ``sitecustomize`` startup hook (ROB-1296), so the guard rejects ``-I``
    # outright. Checking every import's ``__file__`` is the stronger check
    # anyway: ``-I`` only kept the source tree off ``sys.path``, whereas this
    # fails loudly if any module silently resolves outside the wheel.
    script = f"""
import sys
wheel = sys.argv[1]
sys.path.insert(0, wheel)

import app.schemas.research_backtest as research_backtest
import app.services.research_canonical_hash as research_canonical_hash
import research_contracts.canonical_hash as contracts_canonical_hash
from app.schemas.research_backtest import StrategyExperimentIdentity
from app.services import research_offline_gate_service
from app.services.research_canonical_hash import canonical_sha256

for module in (
    research_backtest,
    research_canonical_hash,
    research_offline_gate_service,
    contracts_canonical_hash,
):
    assert module.__file__.startswith(wheel), (module.__name__, module.__file__)

assert StrategyExperimentIdentity
assert research_offline_gate_service.finalize_offline_gate
assert canonical_sha256({{'b': 2, 'a': 1}}) == {PINNED_DIGEST!r}
"""
    subprocess.run(
        [sys.executable, "-c", script, str(wheel)],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    )
