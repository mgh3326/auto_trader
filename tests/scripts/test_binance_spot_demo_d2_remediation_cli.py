"""The D2 remediation CLI — gates, modes, and the flags it deliberately lacks.

The point of these tests is what the CLI *cannot* do. It has no way to name a
symbol, a side, a quantity, or a price, no way to skip the broker-side
order-shape check, and no way to reach ``--confirm`` under the current unsigned
seal — so an operator holding this script cannot widen the one-shot even by
typing carefully.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from app.services.brokers.binance.spot_demo import d2_remediation_single as d2
from app.services.brokers.binance.spot_demo.d2_remediation_single import (
    D2_BOUND_ORDERS,
    D2_PRE_SNAPSHOT_HASH,
    D2_REMEDIATION_ENABLED_ENV,
)
from scripts.binance_spot_demo_d2_remediation import build_parser, main
from tests.services.brokers.binance.spot_demo.test_d2_remediation_single import (
    sealed_payload,
    write_sealed,
)

pytestmark = pytest.mark.unit

_SPOT_DEMO_ENABLED_ENV = "BINANCE_SPOT_DEMO_ENABLED"


@pytest.fixture(autouse=True)
def _gates_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """Both gates start off in every test; a test that needs one arms it."""

    monkeypatch.delenv(D2_REMEDIATION_ENABLED_ENV, raising=False)
    monkeypatch.delenv(_SPOT_DEMO_ENABLED_ENV, raising=False)


@pytest.fixture
def payload_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    return write_sealed(tmp_path, monkeypatch, sealed_payload())


def _stdout_json(capsys: pytest.CaptureFixture[str]) -> dict[str, Any]:
    return json.loads(capsys.readouterr().out)


def test_no_mode_selected_exits_clean_and_does_nothing(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main([]) == 0
    body = _stdout_json(capsys)
    assert body["status"] == "NO_MODE_SELECTED"
    assert body["broker_mutation_count"] == 0


def test_plan_only_prints_the_sealed_values_and_the_blockers(
    payload_file: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["--plan-only", "--sealed-payload", str(payload_file)]) == 0
    body = _stdout_json(capsys)
    assert body["pre_snapshot_hash"] == D2_PRE_SNAPSHOT_HASH
    assert body["broker_mutation_count"] == 0
    assert [op["request_params"] for op in body["operations"]] == [
        order.request_params() for order in D2_BOUND_ORDERS
    ]
    assert [op["client_order_id"] for op in body["operations"]] == [
        order.client_order_id for order in D2_BOUND_ORDERS
    ]
    assert body["dispatch_block_reasons"]


def test_plan_only_needs_no_env_gate_because_it_touches_nothing(
    payload_file: Path,
) -> None:
    """No client, no lease, no socket — so the gates have nothing to guard."""

    assert main(["--plan-only", "--sealed-payload", str(payload_file)]) == 0


def test_plan_only_refuses_an_unregistered_payload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = write_sealed(tmp_path, monkeypatch, sealed_payload(), register=False)
    assert main(["--plan-only", "--sealed-payload", str(path)]) == 1


def test_plan_only_refuses_a_foreign_seal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mutant = sealed_payload()
    mutant["pre_snapshot_hash"] = "sha256:" + "0" * 64
    path = write_sealed(tmp_path, monkeypatch, mutant)
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


def test_confirm_with_both_gates_armed_still_refuses_the_unsigned_seal(
    payload_file: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The B1 fix, end to end.

    Arming both env gates was previously enough to walk a null-authorization
    payload into the dispatch path. Now the refusal happens before a client, a
    lease, or a database session is opened — so this test needs neither.
    """

    monkeypatch.setenv(_SPOT_DEMO_ENABLED_ENV, "true")
    monkeypatch.setenv(D2_REMEDIATION_ENABLED_ENV, "true")
    assert main(["--confirm", "--sealed-payload", str(payload_file)]) == 1
    body = _stdout_json(capsys)
    assert body["status"] == "DISPATCH_NOT_AUTHORIZED"
    assert body["broker_mutation_count"] == 0
    reasons = " | ".join(body["dispatch_block_reasons"])
    assert "operator_authorization is null" in reasons
    assert "dispatch_authorized=false" in reasons


def test_the_shipped_registry_reaches_confirm_only_through_the_r8_authority(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Exactly one registered payload authorizes dispatch, and it is r8's.

    The structural version of §134차's "그 외 봉인은 계속 거부한다": read from the
    shipped constant rather than a fixture, so a registry edit that widens the
    permission fails here as well as at import.
    """

    authorized = {
        digest
        for digest, record in d2.D2_KNOWN_SEALED_PAYLOADS.items()
        if record.dispatch_authorized
    }
    assert authorized == {d2.D2_R8_EXECUTION_AUTHORITY_SHA256}
    assert d2.D2_DISPATCH_AUTHORIZED_DIGESTS == authorized


@pytest.mark.parametrize(
    "flag",
    ["--symbol", "--side", "--quantity", "--price", "--order-type"],
)
def test_the_cli_has_no_flag_that_could_widen_the_one_shot(flag: str) -> None:
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args([flag, "SOLUSDT"])


def test_the_order_shape_check_can_no_longer_be_skipped() -> None:
    """SHOULD-1 from the adversarial review.

    ``--no-order-test`` removed the only broker-side proof that the sealed
    filters still describe the market, which mattered most in exactly the mode
    that sends real orders. The flag is gone.
    """

    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["--no-order-test"])
    assert "--no-order-test" not in parser.format_help()


def test_the_lease_is_released_in_a_finally_block() -> None:
    """SHOULD-2 from the adversarial review.

    The CLI previously closed the execution client and left the advisory lease
    held. It now releases it last — after the proof epochs — and reports
    whether the release was *proven*, exiting non-zero when it was not.
    """

    source = Path("scripts/binance_spot_demo_d2_remediation.py").read_text(
        encoding="utf-8"
    )
    assert "_release_lease(lease)" in source
    assert "finally:" in source
    assert "unreleased_authority_hold" in source


def test_the_three_modes_are_mutually_exclusive() -> None:
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["--dry-run", "--confirm"])
