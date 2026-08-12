"""Output-path guard for the policy-table builder."""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts import build_policy_table


@pytest.mark.unit
def test_shared_operator_checkout_is_never_a_silent_cli_default(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """An omitted destination must abort before the builder can write anything."""

    with pytest.raises(SystemExit) as excinfo:
        build_policy_table._parse_args(["--market", "kr"])

    assert excinfo.value.code == 2
    assert "--out-dir" in capsys.readouterr().err


@pytest.mark.unit
def test_prefect_can_still_name_its_canonical_table_directory_explicitly() -> None:
    """§47's direct ``policy-tables/`` write path remains a valid invocation."""

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
