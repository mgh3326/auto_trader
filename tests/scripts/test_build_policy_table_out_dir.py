"""Output-path guard for the policy-table builder."""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts import build_policy_table


@pytest.mark.unit
def test_prefect_caller_form_keeps_its_default_output_and_returns_zero(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The deployed caller's exact module argv remains compatible.

    The fake async body deliberately performs no table generation; this test
    proves parsing, warning, and the return status for the exact argv used by
    robin_automation.b0x_kickoff.build_policy_table.
    """

    captured: dict[str, str] = {}

    async def fake_run(args) -> int:  # noqa: ANN001
        captured["market"] = args.market
        captured["out_dir"] = args.out_dir
        return 0

    monkeypatch.setattr(build_policy_table, "_run", fake_run)

    assert build_policy_table.main(["--market", "kr"]) == 0
    assert captured == {
        "market": "kr",
        "out_dir": str(build_policy_table.DEFAULT_OUT_DIR),
    }
    stderr = capsys.readouterr().err
    assert "WARNING:" in stderr
    assert "shared operator checkout" in stderr
    assert str(build_policy_table.DEFAULT_OUT_DIR) in stderr


@pytest.mark.unit
def test_explicit_prefect_destination_is_warned_not_rejected(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """§47's direct ``policy-tables/`` path remains valid and visible."""

    args = build_policy_table._parse_args(
        [
            "--market",
            "kr",
            "--out-dir",
            str(build_policy_table.OPERATOR_POLICY_TABLE_DIR),
        ]
    )

    assert (
        Path(args.out_dir).expanduser() == build_policy_table.OPERATOR_POLICY_TABLE_DIR
    )
    assert "WARNING:" in capsys.readouterr().err


@pytest.mark.unit
def test_explicit_isolated_destination_does_not_emit_shared_checkout_warning(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    args = build_policy_table._parse_args(
        ["--market", "kr", "--out-dir", str(tmp_path / "isolated-tables")]
    )

    assert Path(args.out_dir) == tmp_path / "isolated-tables"
    assert capsys.readouterr().err == ""
