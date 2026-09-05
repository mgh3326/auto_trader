from __future__ import annotations

import ast
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from scripts import lane_event_kickoff as kickoff

pytest_plugins = ("tests.services.test_lane_events",)


NOW = datetime(2026, 9, 5, 14, 59, tzinfo=UTC)


def _configure_emit(
    monkeypatch: pytest.MonkeyPatch, emit_binary_path: Path, tmp_path: Path
) -> Path:
    argv_path = tmp_path / "argv.json"
    monkeypatch.setenv("LANE_EVENT_KICKOFF_ENABLED", "true")
    monkeypatch.setenv("LANE_EVENT_EMIT_BIN", str(emit_binary_path))
    monkeypatch.setenv("LANE_EVENT_EMIT_HOST", "host-a")
    monkeypatch.setenv("LANE_EVENT_EMIT_PANE", "-")
    monkeypatch.setenv("LANE_EVENT_EMIT_INBOX_ROOT", str(tmp_path / "inbox"))
    monkeypatch.setenv("LANE_EVENT_EMIT_TIMEOUT_S", "3")
    monkeypatch.setenv("LANE_EVENT_TEST_ARGV", str(argv_path))
    return argv_path


def _payload(capsys: pytest.CaptureFixture[str]) -> dict[str, object]:
    return json.loads(capsys.readouterr().out)


def test_emits_exact_kickoff_argv_and_json(
    emit_binary: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    argv_path = _configure_emit(monkeypatch, emit_binary, tmp_path)

    assert (
        kickoff.main(
            [
                "--lane",
                "lane-a",
                "--slot",
                "0905",
                "--playbook",
                "prompts/kr-open-trade.md",
            ],
            now=NOW,
        )
        == 0
    )

    assert json.loads(argv_path.read_text(encoding="utf-8")) == [
        str(emit_binary),
        "emit",
        "--kind",
        "lane.event",
        "--lane",
        "lane-a",
        "--event-id",
        "kickoff-0905-2026-09-05",
        "--text",
        "[kickoff] 0905 prompts/kr-open-trade.md date=2026-09-05",
        "--host",
        "host-a",
        "--inbox-root",
        str(tmp_path / "inbox"),
        "--timeout",
        "2s",
    ]
    assert _payload(capsys) == {
        "date": "2026-09-05",
        "dry_run": False,
        "duplicate": False,
        "emitted": True,
        "enabled": True,
        "event_id": "kickoff-0905-2026-09-05",
        "lane": "lane-a",
        "playbook": "prompts/kr-open-trade.md",
        "slot": "0905",
    }


def test_kst_date_uses_korean_calendar_at_midnight() -> None:
    before_midnight_kst = datetime(2026, 9, 5, 14, 59, tzinfo=UTC)
    after_midnight_kst = datetime(2026, 9, 5, 15, 1, tzinfo=UTC)

    assert kickoff.kst_trading_date(before_midnight_kst) == "2026-09-05"
    assert kickoff.kst_trading_date(after_midnight_kst) == "2026-09-06"
    assert (
        kickoff.kickoff_event_id(
            "crypto-0220", kickoff.kst_trading_date(before_midnight_kst)
        )
        == "kickoff-crypto-0220-2026-09-05"
    )
    assert (
        kickoff.kickoff_event_id(
            "crypto-0220", kickoff.kst_trading_date(after_midnight_kst)
        )
        == "kickoff-crypto-0220-2026-09-06"
    )
    assert (
        before_midnight_kst.date().isoformat() == after_midnight_kst.date().isoformat()
    )


def test_duplicate_is_a_successful_idempotent_result(
    emit_binary: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _configure_emit(monkeypatch, emit_binary, tmp_path)
    monkeypatch.setenv("LANE_EVENT_TEST_MODE", "duplicate")

    assert (
        kickoff.main(
            [
                "--lane",
                "lane-a",
                "--slot",
                "crypto-0220",
                "--playbook",
                "prompts/crypto-session-trade.md",
            ],
            now=NOW,
        )
        == 0
    )
    payload = _payload(capsys)
    assert payload["duplicate"] is True
    assert payload["emitted"] is False
    assert payload["event_id"] == "kickoff-crypto-0220-2026-09-05"


@pytest.mark.parametrize("enabled,dry_run", [(True, True), (False, False)])
def test_dry_run_and_disabled_gate_never_start_emitter(
    emit_binary: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    enabled: bool,
    dry_run: bool,
) -> None:
    argv_path = _configure_emit(monkeypatch, emit_binary, tmp_path)
    monkeypatch.setenv("LANE_EVENT_KICKOFF_ENABLED", "true" if enabled else "false")
    args = [
        "--lane",
        "lane-a",
        "--slot",
        "0905",
        "--playbook",
        "prompts/kr-open-trade.md",
    ]
    if dry_run:
        args.append("--dry-run")

    assert kickoff.main(args, now=NOW) == 0
    payload = _payload(capsys)
    assert payload["dry_run"] is True
    assert payload["emitted"] is False
    assert payload["enabled"] is enabled
    assert not argv_path.exists()


@pytest.mark.parametrize(
    ("binary", "mode", "timeout_s", "reason"),
    [
        ("emit_binary", "failed", "3", "exit_70"),
        ("emit_binary", "hang", "1.1", "timeout"),
        ("missing", "success", "3", "binary_not_found"),
    ],
)
def test_emitter_failures_are_exit_one_with_reason(
    emit_binary: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    binary: str,
    mode: str,
    timeout_s: str,
    reason: str,
) -> None:
    argv_path = _configure_emit(monkeypatch, emit_binary, tmp_path)
    monkeypatch.setenv("LANE_EVENT_EMIT_TIMEOUT_S", timeout_s)
    monkeypatch.setenv("LANE_EVENT_TEST_MODE", mode)
    if binary == "missing":
        monkeypatch.setenv("LANE_EVENT_EMIT_BIN", str(tmp_path / "missing-panewire"))

    assert (
        kickoff.main(
            [
                "--lane",
                "lane-a",
                "--slot",
                "0905",
                "--playbook",
                "prompts/kr-open-trade.md",
            ],
            now=NOW,
        )
        == 1
    )
    payload = _payload(capsys)
    assert payload["reason"] == reason
    if binary == "missing":
        assert not argv_path.exists()


def test_invalid_operational_inputs_fail_closed_without_emitting(
    emit_binary: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    argv_path = _configure_emit(monkeypatch, emit_binary, tmp_path)

    with pytest.raises(SystemExit) as invalid_date:
        kickoff.main(
            [
                "--lane",
                "lane-a",
                "--slot",
                "0905",
                "--playbook",
                "prompts/kr-open-trade.md",
                "--date",
                "2026-9-5",
            ],
            now=NOW,
        )
    assert invalid_date.value.code == 2
    with pytest.raises(SystemExit) as invalid_playbook:
        kickoff.main(
            [
                "--lane",
                "lane-a",
                "--slot",
                "0905",
                "--playbook",
                "../outside.md",
            ],
            now=NOW,
        )
    assert invalid_playbook.value.code == 2
    assert (
        kickoff.main(
            [
                "--lane",
                "lane-a",
                "--slot",
                "0905",
                "--playbook",
                f"{'a' * 2048}.md",
            ],
            now=NOW,
        )
        == 2
    )
    payload = _payload(capsys)
    assert payload["reason"] == "text_too_long"
    assert payload["enabled"] is True
    assert not argv_path.exists()


def test_kickoff_cli_has_only_the_lane_event_service_dependency() -> None:
    source_path = Path(kickoff.__file__)
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    app_imports = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        and node.module is not None
        and node.module.startswith("app")
    }
    direct_imports = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    assert app_imports == {"app.services.lane_events"}
    assert "subprocess" not in direct_imports
    assert "app.core.db" not in source_path.read_text(encoding="utf-8")
