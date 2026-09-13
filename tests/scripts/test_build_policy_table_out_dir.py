"""Output-path guard for the policy-table builder."""

from __future__ import annotations

import asyncio
import json
import os
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


@pytest.mark.unit
def test_prefect_crypto_mode_preserves_slot_worker_latest_pointer(
    tmp_path: Path,
) -> None:
    args = build_policy_table._parse_args(
        [
            "--market",
            "crypto",
            "--out-dir",
            str(tmp_path / "isolated-tables"),
            "--preserve-latest-pointer",
        ]
    )
    assert args.preserve_latest_pointer is True

    with pytest.raises(SystemExit):
        build_policy_table._parse_args(["--market", "kr", "--preserve-latest-pointer"])


@pytest.mark.unit
def test_crypto_build_does_not_touch_slot_worker_pointer_in_preserve_mode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    output = tmp_path / "policy-tables"
    output.mkdir()
    latest = output / "latest-crypto.json"
    latest.write_text('{"owner":"slot-worker"}\n', encoding="utf-8")

    async def fake_fetch_raw_inputs(*, top_n: int) -> object:
        assert top_n == build_policy_table.crypto_adapter.DEFAULT_TOP_N
        return object()

    monkeypatch.setattr(
        build_policy_table.crypto_adapter,
        "fetch_raw_inputs",
        fake_fetch_raw_inputs,
    )
    monkeypatch.setattr(
        build_policy_table.crypto_adapter,
        "compute_policy_table",
        lambda raw, *, top_n: {"generated_at": "fixture", "market": "crypto"},
    )
    monkeypatch.setattr(
        build_policy_table,
        "_build_stamps",
        lambda payload: {"policy_table_hash": "fixture"},
    )
    monkeypatch.setattr(
        build_policy_table,
        "_render_summary_md",
        lambda payload: "# fixture summary\n",
    )
    monkeypatch.setattr(
        build_policy_table,
        "canonical_json_bytes",
        lambda payload: json.dumps(payload, sort_keys=True).encode(),
    )
    args = build_policy_table._parse_args(
        [
            "--market",
            "crypto",
            "--out-dir",
            str(output),
            "--fixed-ts",
            "20260910T000000Z",
            "--preserve-latest-pointer",
        ]
    )

    assert asyncio.run(build_policy_table._run(args)) == 0
    assert latest.read_text(encoding="utf-8") == '{"owner":"slot-worker"}\n'
    assert (output / "20260910T000000Z-crypto.json").is_file()
    manifest_line = next(
        line
        for line in capsys.readouterr().out.splitlines()
        if line.startswith("BUILD_OUTPUT_JSON=")
    )
    manifest = json.loads(manifest_line.removeprefix("BUILD_OUTPUT_JSON="))
    assert manifest["latest_pointer_updated"] is False


@pytest.mark.unit
def test_crypto_failure_never_consults_or_mutates_slot_worker_pointer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = tmp_path / "policy-tables"
    output.mkdir()
    slot_worker_artifact = output / "slot-worker-crypto.json"
    slot_worker_bytes = b'{"owner":"slot-worker","fixture":true}\n'
    slot_worker_artifact.write_bytes(slot_worker_bytes)
    latest = output / "latest-crypto.json"
    latest.symlink_to(slot_worker_artifact.name)
    before_mode = latest.lstat().st_mode
    before_target = os.readlink(latest)

    async def fail_fetch_raw_inputs(*, top_n: int) -> object:
        raise RuntimeError(f"fixture fetch failure for top_n={top_n}")

    def forbidden_pointer_access(*args: object, **kwargs: object) -> None:
        raise AssertionError("slot-worker latest pointer must remain unconsulted")

    monkeypatch.setattr(
        build_policy_table.crypto_adapter,
        "fetch_raw_inputs",
        fail_fetch_raw_inputs,
    )
    monkeypatch.setattr(
        build_policy_table,
        "_latest_pointer_last_good",
        forbidden_pointer_access,
    )
    monkeypatch.setattr(
        build_policy_table,
        "_replace_latest_pointer",
        forbidden_pointer_access,
    )
    args = build_policy_table._parse_args(
        [
            "--market",
            "crypto",
            "--out-dir",
            str(output),
            "--preserve-latest-pointer",
        ]
    )

    assert asyncio.run(build_policy_table._run(args)) == 1
    assert latest.is_symlink()
    assert latest.lstat().st_mode == before_mode
    assert os.readlink(latest) == before_target
    assert slot_worker_artifact.read_bytes() == slot_worker_bytes
    stale = json.loads((output / "latest-crypto.STALE").read_text())
    assert stale["last_good_artifact"] is None
    assert stale["latest_pointer_disposition"] == "unconsulted_slot_worker_owned"
