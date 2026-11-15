"""hk 4823: TREND-TSMOM-v1 tick export + the heartbeat read of its snapshot.

The exporter turns ``docker logs --timestamps`` text into the timestamp snapshot
the experiment heartbeat reads for epoch ``TREND-TSMOM-v1``. The fixtures here
are captured-format docker lines (RFC 3339 nanos + payload), not live logs.
"""

from __future__ import annotations

import ast
import datetime as dt
import io
import json
import sys
from pathlib import Path
from typing import Any

import pytest

from research.experiments import heartbeat as hb
from scripts import binance_trend_tick_export as exporter

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[2]
MODULE = REPO / "scripts/binance_trend_tick_export.py"
UTC = dt.UTC

T0 = dt.datetime(2026, 11, 16, 9, 0, tzinfo=UTC)
DECISION_TS = 1790000000000
TICK = {
    "decision_ts": DECISION_TS,
    "events": [{"symbol": "BTCUSDT", "event": "no_signal", "detail": None}],
}
BLOCKED = {"event": "blocked", "error_class": "ConnectionError"}


def _stamp(value: dt.datetime) -> str:
    """Docker prints nanoseconds; keep 9 fractional digits like the real log."""

    return value.strftime("%Y-%m-%dT%H:%M:%S") + f".{value.microsecond:06d}789Z"


def _line(value: dt.datetime, payload: Any) -> str:
    return f"{_stamp(value)} {json.dumps(payload, sort_keys=True)}"


def _seconds(value: int) -> dt.datetime:
    return T0 + dt.timedelta(seconds=value)


def _run(tmp_path: Path, text: str, out: Path | None = None) -> int:
    source = tmp_path / f"source-{abs(hash(text))}.log"
    source.write_text(text, encoding="utf-8")
    target = tmp_path / "snapshot.json" if out is None else out
    return exporter.main(["--input", str(source), "--out", str(target)])


def test_export_keeps_only_runner_ticks_sorted_and_deduplicated(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    text = "\n".join(
        [
            f"{_stamp(T0)} starting the trend runner",  # log noise
            "a line with no docker timestamp prefix",  # noise
            _line(_seconds(1), TICK),
            _line(_seconds(2), BLOCKED),
            _line(_seconds(1), TICK),  # same observed_at: duplicate
            _line(_seconds(3), {"event": "started"}),  # not a runner tick
            f"{_stamp(_seconds(4))} {{'decision_ts': 1",  # cut-off JSON
            _line(_seconds(5), TICK),
        ]
    )
    out = tmp_path / "snapshot.json"
    assert _run(tmp_path, text, out) == exporter.EXIT_OK

    snapshot = json.loads(out.read_text(encoding="utf-8"))
    assert set(snapshot) == {"schema", "lane", "exported_at", "ticks"}
    assert snapshot["schema"] == "trend-tick-export/v1"
    assert snapshot["lane"] == "TREND-TSMOM-v1" == exporter.LANE
    assert dt.datetime.fromisoformat(snapshot["exported_at"]).tzinfo is not None
    assert snapshot["ticks"] == [
        {"observed_at": "2026-11-16T09:00:01+00:00", "decision_ts": DECISION_TS},
        {"observed_at": "2026-11-16T09:00:02+00:00", "decision_ts": None},
        {"observed_at": "2026-11-16T09:00:05+00:00", "decision_ts": DECISION_TS},
    ]

    summary = json.loads(capsys.readouterr().out)
    assert summary["ticks"] == 3
    assert summary["duplicate_lines"] == 1
    assert summary["ignored_lines"] == 4
    assert summary["written"] is True


@pytest.mark.parametrize(
    "payload",
    [
        {"decision_ts": "1790000000000", "events": []},
        {"decision_ts": 1790000000000.0, "events": []},
        {"decision_ts": True, "events": []},
        {"decision_ts": DECISION_TS, "events": [], "extra": 1},
        {"decision_ts": DECISION_TS},
        {"decision_ts": DECISION_TS, "events": {}},
        {"decision_ts": DECISION_TS, "events": ["no_signal"]},
        {
            "decision_ts": DECISION_TS,
            "events": [{"symbol": "BTCUSDT", "event": "no_signal", "extra": 1}],
        },
        {"event": "blocked"},
        {"event": "blocked", "error_class": 3},
        {"event": "started", "error_class": "ConnectionError"},
        [1, 2, 3],
        "no_signal",
    ],
)
def test_a_line_whose_payload_is_not_the_closed_tick_shape_is_ignored(
    payload: Any,
) -> None:
    text = "\n".join(
        [
            _line(_seconds(1), payload),
            _line(_seconds(2), TICK),
            _line(_seconds(3), BLOCKED),
        ]
    )
    parsed = exporter.parse_lines(text.splitlines())
    assert [tick["decision_ts"] for tick in parsed.ticks] == [DECISION_TS, None]
    assert parsed.ignored_lines == 1
    assert parsed.duplicate_lines == 0


@pytest.mark.parametrize(
    ("stamp", "expected"),
    [
        ("2026-11-16T09:00:00Z", "2026-11-16T09:00:00+00:00"),
        ("2026-11-16T09:00:00.5Z", "2026-11-16T09:00:00.500000+00:00"),
        ("2026-11-16T09:00:00.123456789Z", "2026-11-16T09:00:00.123456+00:00"),
    ],
)
def test_the_docker_timestamp_is_read_as_utc_microseconds(
    stamp: str, expected: str
) -> None:
    parsed = exporter.parse_lines([f"{stamp} {json.dumps(TICK)}"])
    assert parsed.ticks == [{"observed_at": expected, "decision_ts": DECISION_TS}]
    assert parsed.ignored_lines == 0


def test_an_unparseable_docker_timestamp_is_ignored() -> None:
    parsed = exporter.parse_lines(
        [f"2026-02-30T09:00:00.000000000Z {json.dumps(TICK)}"]
    )
    assert parsed.ticks == []
    assert parsed.ignored_lines == 1


def test_zero_ticks_exits_2_and_never_writes_an_empty_snapshot(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "snapshot.json"
    previous = '{"schema": "trend-tick-export/v1", "ticks": [{"old": true}]}\n'
    out.write_text(previous, encoding="utf-8")

    text = "\n".join([f"{_stamp(T0)} container started", "noise without a prefix"])
    assert _run(tmp_path, text, out) == exporter.EXIT_NO_TICKS

    assert out.read_text(encoding="utf-8") == previous
    assert [path.name for path in tmp_path.iterdir() if path.suffix == ".partial"] == []
    summary = json.loads(capsys.readouterr().out)
    assert summary["written"] is False
    assert summary["ticks"] == 0


def test_stdin_is_read_when_no_input_file_is_given(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "stdin", io.StringIO(_line(_seconds(1), TICK) + "\n"))
    out = tmp_path / "snapshot.json"
    assert exporter.main(["--out", str(out)]) == exporter.EXIT_OK
    snapshot = json.loads(out.read_text(encoding="utf-8"))
    assert len(snapshot["ticks"]) == 1


def test_a_reexport_replaces_the_snapshot_without_leaving_staging_files(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "snapshot.json"
    assert _run(tmp_path, _line(_seconds(1), TICK), out) == exporter.EXIT_OK
    first = json.loads(out.read_text(encoding="utf-8"))
    assert [tick["observed_at"] for tick in first["ticks"]] == [
        "2026-11-16T09:00:01+00:00"
    ]

    assert _run(tmp_path, _line(_seconds(5), TICK), out) == exporter.EXIT_OK
    capsys.readouterr()
    second = json.loads(out.read_text(encoding="utf-8"))
    assert [tick["observed_at"] for tick in second["ticks"]] == [
        "2026-11-16T09:00:05+00:00"
    ]
    assert [path for path in tmp_path.iterdir() if path.name.startswith(".")] == []


def test_errors_exit_3(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as missing_out:
        exporter.main([])
    assert missing_out.value.code == exporter.EXIT_USAGE

    out = tmp_path / "snapshot.json"
    code = exporter.main(["--input", str(tmp_path / "absent.log"), "--out", str(out)])
    assert code == exporter.EXIT_USAGE
    assert not out.exists()
    assert "Traceback" not in capsys.readouterr().err


def _heartbeat_config() -> dict[str, Any]:
    config = hb.load_config(hb.DEFAULT_CONFIG)
    epochs = [e for e in config["epochs"] if e["id"] == "TREND-TSMOM-v1"]
    assert len(epochs) == 1
    return dict(config, epochs=epochs)


def _export(tmp_path: Path, *lines: str) -> Path:
    out = tmp_path / "trend-ticks.json"
    assert _run(tmp_path, "\n".join(lines), out) == exporter.EXIT_OK
    return out


def test_heartbeat_reads_a_fresh_export_as_ok(tmp_path: Path) -> None:
    out = _export(
        tmp_path,
        _line(dt.datetime(2026, 11, 16, 0, 0, tzinfo=UTC), TICK),
        _line(dt.datetime(2026, 11, 16, 0, 1, tzinfo=UTC), TICK),
    )
    [result] = hb.evaluate(
        _heartbeat_config(),
        {"TREND-TSMOM-v1": out},
        dt.datetime(2026, 11, 17, 12, 0, tzinfo=UTC),
    )
    assert (result.status, result.idle_sessions) == ("OK", 1)
    assert result.last_advance == dt.datetime(2026, 11, 16, 0, 1, tzinfo=UTC)
    assert result.detail == "latest of 2 timestamp(s)"


def test_heartbeat_reads_a_stale_export_as_red(tmp_path: Path) -> None:
    out = _export(tmp_path, _line(dt.datetime(2026, 11, 16, 0, 0, tzinfo=UTC), BLOCKED))
    [result] = hb.evaluate(
        _heartbeat_config(),
        {"TREND-TSMOM-v1": out},
        dt.datetime(2026, 11, 21, 0, 0, tzinfo=UTC),
    )
    assert (result.status, result.idle_sessions) == ("RED", 5)
    assert hb.exit_code([result]) == 1


def test_export_imports_stdlib_only() -> None:
    tree = ast.parse(MODULE.read_text(encoding="utf-8"))
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0, "relative import in a top-level script"
            assert node.module, "import without a module"
            roots.add(node.module.split(".")[0])
    assert roots <= set(sys.stdlib_module_names), roots - set(sys.stdlib_module_names)
    assert not roots & {"app", "research", "scripts", "alembic"}, roots
    assert not [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Name) and node.id == "__import__"
    ]
