"""The D2 remediation CLI — gates, modes, and the flags it deliberately lacks.

The point of these tests is what the CLI *cannot* do. It has no way to name a
symbol, a side, a quantity, or a price, so an operator holding this script
cannot widen the one-shot even by typing carefully.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from app.services.brokers.binance.spot_demo.d2_remediation_single import (
    D2_BOUND_ORDERS,
    D2_PRE_SNAPSHOT_HASH,
    D2_REMEDIATION_ENABLED_ENV,
)
from scripts.binance_spot_demo_d2_remediation import build_parser, main
from tests.services.brokers.binance.spot_demo.test_d2_remediation_single import (
    sealed_payload,
)

pytestmark = pytest.mark.unit

_SPOT_DEMO_ENABLED_ENV = "BINANCE_SPOT_DEMO_ENABLED"


@pytest.fixture(autouse=True)
def _gates_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """Both gates start off in every test; a test that needs one arms it."""

    monkeypatch.delenv(D2_REMEDIATION_ENABLED_ENV, raising=False)
    monkeypatch.delenv(_SPOT_DEMO_ENABLED_ENV, raising=False)


@pytest.fixture
def payload_file(tmp_path: Path) -> Path:
    path = tmp_path / "binding-payload-proposed.json"
    path.write_text(json.dumps(sealed_payload()), encoding="utf-8")
    return path


def _stdout_json(capsys: pytest.CaptureFixture[str]) -> dict[str, Any]:
    return json.loads(capsys.readouterr().out)


def test_no_mode_selected_exits_clean_and_does_nothing(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main([]) == 0
    body = _stdout_json(capsys)
    assert body["status"] == "NO_MODE_SELECTED"
    assert body["broker_mutation_count"] == 0


def test_plan_only_prints_the_sealed_values(
    payload_file: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["--plan-only", "--sealed-payload", str(payload_file)]) == 0
    body = _stdout_json(capsys)
    assert body["pre_snapshot_hash"] == D2_PRE_SNAPSHOT_HASH
    assert body["broker_mutation_count"] == 0
    assert [op["request_params"] for op in body["operations"]] == [
        order.request_params() for order in D2_BOUND_ORDERS
    ]


def test_plan_only_needs_no_env_gate_because_it_touches_nothing(
    payload_file: Path,
) -> None:
    """No client, no lease, no socket — so the gates have nothing to guard."""

    assert main(["--plan-only", "--sealed-payload", str(payload_file)]) == 0


def test_plan_only_refuses_a_foreign_seal(tmp_path: Path) -> None:
    mutant = sealed_payload()
    mutant["pre_snapshot_hash"] = "sha256:" + "0" * 64
    path = tmp_path / "mutant.json"
    path.write_text(json.dumps(mutant), encoding="utf-8")
    assert main(["--plan-only", "--sealed-payload", str(path)]) == 1


def test_every_mode_requires_a_sealed_payload() -> None:
    assert main(["--plan-only"]) == 1
    assert main(["--dry-run"]) == 1
    assert main(["--confirm"]) == 1


def test_dry_run_refuses_while_the_gates_are_off(payload_file: Path) -> None:
    assert main(["--dry-run", "--sealed-payload", str(payload_file)]) == 1


def test_confirm_refuses_while_the_gates_are_off(payload_file: Path) -> None:
    assert main(["--confirm", "--sealed-payload", str(payload_file)]) == 1


def test_confirm_still_refuses_when_only_one_gate_is_armed(
    payload_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Neither gate stands in for the other."""

    monkeypatch.setenv(_SPOT_DEMO_ENABLED_ENV, "true")
    assert main(["--confirm", "--sealed-payload", str(payload_file)]) == 1
    monkeypatch.delenv(_SPOT_DEMO_ENABLED_ENV)
    monkeypatch.setenv(D2_REMEDIATION_ENABLED_ENV, "true")
    assert main(["--confirm", "--sealed-payload", str(payload_file)]) == 1


@pytest.mark.parametrize(
    "flag", ["--symbol", "--side", "--quantity", "--price", "--order-type"]
)
def test_the_cli_has_no_flag_that_could_widen_the_one_shot(flag: str) -> None:
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args([flag, "SOLUSDT"])


def test_the_three_modes_are_mutually_exclusive() -> None:
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["--dry-run", "--confirm"])
