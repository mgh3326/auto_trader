"""CLI: seal once, status, interim report, step via an offline parents file."""

from __future__ import annotations

import json
import stat

import pytest

from research.rob1391_us_resubmit_shadow import cli, guards, market_data
from tests.research.rob1391_us_resubmit_shadow.helpers import (
    CODE_SHA,
    FEE_SCHEDULE,
    POLICY,
    SEAL_AT,
    FakeMarket,
)

pytestmark = pytest.mark.unit


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(guards, "git_code_sha", lambda root=None: CODE_SHA)
    monkeypatch.setattr(guards, "current_policy_stamp", lambda: dict(POLICY))
    # The pytest process shares sys.modules with other suites that import
    # app.models.order_proposals (-> app.services.order_proposals); the real
    # guard would then rightly latch. The guard itself is covered by
    # test_engine::test_write_path_reachable_latches and the clean-subprocess
    # test_isolation::test_runtime_closure_loads_no_write_path_module.
    monkeypatch.setattr(guards, "loaded_forbidden_modules", lambda modules=None: [])
    # never construct the real network client in tests
    monkeypatch.setattr(market_data, "AlpacaMarketData", lambda **kw: FakeMarket())
    prereg = tmp_path / "prereg.md"
    prereg.write_text("fixture\n")
    fees = tmp_path / "fees.json"
    fees.write_text(json.dumps(FEE_SCHEDULE))
    return tmp_path, prereg, fees


def seal_args(tmp, prereg, fees, *, version=POLICY["version"]):
    return [
        "seal",
        "--ledger-dir",
        str(tmp / "ledger"),
        "--prereg",
        str(prereg),
        "--approval-ref",
        "fixture",
        "--deployed-policy-version",
        version,
        "--deployed-policy-hash",
        POLICY["content_hash"],
        "--fee-schedule",
        str(fees),
        "--seal-at",
        SEAL_AT.isoformat(),
        "--approve-deviation",
        "D1_exit_hold_to_d20",
        "--approve-deviation",
        "D2_resubmit_gate_subset",
        "--deviation-ref",
        "fixture-deviation-ref",
    ]


def test_seal_once_status_and_interim_report(env, capsys):
    tmp, prereg, fees = env
    assert cli.main(seal_args(tmp, prereg, fees)) == cli.EXIT_OK
    assert cli.main(seal_args(tmp, prereg, fees)) == cli.EXIT_REFUSED  # written once
    assert stat.S_IMODE((tmp / "ledger" / "manifest.json").stat().st_mode) == 0o600
    capsys.readouterr()
    assert cli.main(["status", "--ledger-dir", str(tmp / "ledger")]) == cli.EXIT_OK
    status = json.loads(capsys.readouterr().out)
    assert status["next_due"] == ["step", "2025-11-14"] and status["latches"] == []
    assert (
        cli.main(
            ["report", "--ledger-dir", str(tmp / "ledger"), "--out", str(tmp / "out")]
        )
        == cli.EXIT_OK
    )
    assert "INTERIM" in (tmp / "out" / "report.md").read_text()
    assert stat.S_IMODE((tmp / "out" / "report.md").stat().st_mode) == 0o600


def test_seal_refuses_deployed_policy_mismatch(env):
    tmp, prereg, fees = env
    assert (
        cli.main(seal_args(tmp, prereg, fees, version="2099-01-01.1"))
        == cli.EXIT_REFUSED
    )
    assert not (tmp / "ledger" / "manifest.json").exists()


def test_step_with_parents_file_and_code_sha_refusal(env, monkeypatch, capsys):
    tmp, prereg, fees = env
    cli.main(seal_args(tmp, prereg, fees))
    parents = tmp / "parents.json"
    parents.write_text(
        json.dumps({"order_rows": [], "rung_rows": [], "universe_rows": []})
    )
    args = [
        "step",
        "--ledger-dir",
        str(tmp / "ledger"),
        "--parents-file",
        str(parents),
        "--session",
        "2025-11-14",
    ]
    assert cli.main(args) == cli.EXIT_OK
    assert cli.main(args) == cli.EXIT_OK  # idempotent no-op
    assert "no-op" in capsys.readouterr().out
    monkeypatch.setattr(guards, "git_code_sha", lambda root=None: "0" * 40)
    reg = [
        "register",
        "--ledger-dir",
        str(tmp / "ledger"),
        "--parents-file",
        str(parents),
        "--session",
        "2025-11-14",
    ]
    assert cli.main(reg) == cli.EXIT_REFUSED


def test_policy_change_exits_latched(env, monkeypatch):
    tmp, prereg, fees = env
    cli.main(seal_args(tmp, prereg, fees))
    parents = tmp / "parents.json"
    parents.write_text(
        json.dumps({"order_rows": [], "rung_rows": [], "universe_rows": []})
    )
    monkeypatch.setattr(
        guards, "current_policy_stamp", lambda: {"version": "x", "content_hash": "y"}
    )
    args = [
        "advance",
        "--ledger-dir",
        str(tmp / "ledger"),
        "--parents-file",
        str(parents),
        "--through",
        "2025-11-14",
    ]
    assert cli.main(args) == cli.EXIT_LATCHED


def test_seal_without_mandatory_deviation_approval_is_refused(env):
    tmp, prereg, fees = env
    args = seal_args(tmp, prereg, fees)
    cut = args.index("--approve-deviation")
    assert cli.main(args[:cut]) == cli.EXIT_REFUSED
    assert not (tmp / "ledger" / "manifest.json").exists()
