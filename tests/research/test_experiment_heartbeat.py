"""ROB-1429 experiment heartbeat: idle > 3 sessions prints RED (no production access)."""

from __future__ import annotations

import ast
import datetime as dt
import json
from pathlib import Path

import pytest

from research.experiments import heartbeat as hb

REPO = Path(__file__).resolve().parents[2]
UTC = dt.UTC


def _ts(raw: str) -> dt.datetime:
    return dt.datetime.fromisoformat(raw).astimezone(UTC)


def _ledger(tmp_path: Path, *events: tuple[str, str]) -> Path:
    """A ROB-1391-shaped ledger dir: events.jsonl with kind + recorded_at."""

    root = tmp_path / "ledger"
    root.mkdir()
    lines = [
        json.dumps({"seq": i, "kind": kind, "recorded_at": at, "payload": {}})
        for i, (kind, at) in enumerate(events, start=1)
    ]
    (root / "events.jsonl").write_text("\n".join(lines) + "\n")
    return root


def _config(*epochs: dict) -> dict:
    return {"schema_version": 1, "idle_red_after_sessions": 3, "epochs": list(epochs)}


ROB1391 = {
    "id": "ROB-1391",
    "hypothesis": None,
    "description": "test",
    "calendar": "XNYS",
    "source": {
        "kind": "hash_chained_ledger",
        "path": None,
        "advance_kinds": ["register", "step"],
    },
}
H5 = {
    "id": "H5",
    "hypothesis": None,
    "description": "test",
    "calendar": "24/7",
    "source": {
        "kind": "json_timestamps",
        "path": None,
        "select": ["nav_samples", "*", "observed_at"],
    },
}

# Last advance Thu 2026-10-29 after the XNYS close (20:00Z). Later closes:
# Fri 10-30 20:00Z, Mon 11-02 21:00Z, Tue 11-03 21:00Z, Wed 11-04 21:00Z.
LAST_ADVANCE = "2026-10-29T21:05:00+00:00"


@pytest.fixture
def ledger(tmp_path: Path) -> Path:
    return _ledger(
        tmp_path,
        ("sealed", "2026-10-20T13:00:00+00:00"),
        ("step", "2026-10-28T21:05:00+00:00"),
        ("register", LAST_ADVANCE),
    )


@pytest.mark.parametrize(
    ("now", "idle", "status"),
    [
        ("2026-11-02T22:00:00+00:00", 2, "OK"),
        ("2026-11-03T22:00:00+00:00", 3, "OK"),
        ("2026-11-04T22:00:00+00:00", 4, "RED"),
    ],
)
def test_xnys_epoch_idle_sessions(
    ledger: Path, now: str, idle: int, status: str
) -> None:
    [result] = hb.evaluate(_config(ROB1391), {"ROB-1391": ledger}, _ts(now))
    assert result.last_advance == _ts(LAST_ADVANCE)
    assert result.idle_sessions == idle
    assert result.status == status


def test_idle_four_prints_red_and_idle_two_prints_ok(ledger: Path, capsys) -> None:
    config = REPO / "research/experiments/heartbeat.json"
    snapshot = ledger.parent / "h5.json"
    snapshot.write_text(
        json.dumps({"nav_samples": [{"observed_at": "2026-11-02T21:00:00+00:00"}]})
    )

    code = hb.main(
        [
            "--config",
            str(config),
            "--path",
            f"ROB-1391={ledger}",
            "--path",
            f"H5={snapshot}",
            "--now",
            "2026-11-04T22:00:00+00:00",
        ]
    )
    out = capsys.readouterr().out
    assert code == 1
    assert out.splitlines()[0].startswith("RED")
    assert "ROB-1391" in out.splitlines()[0]
    assert "idle_sessions=4" in out.splitlines()[0]

    code = hb.main(
        [
            "--config",
            str(config),
            "--path",
            f"ROB-1391={ledger}",
            "--path",
            f"H5={snapshot}",
            "--now",
            "2026-11-02T22:00:00+00:00",
        ]
    )
    out = capsys.readouterr().out
    assert code == 0
    assert out.splitlines()[0].startswith("OK")
    assert "idle_sessions=2" in out.splitlines()[0]


@pytest.mark.parametrize(
    ("now", "idle", "status"),
    [("2026-10-30T01:00:00+00:00", 2, "OK"), ("2026-11-01T01:00:00+00:00", 4, "RED")],
)
def test_h5_snapshot_epoch_counts_utc_days(
    tmp_path: Path, now: str, idle: int, status: str
) -> None:
    snapshot = tmp_path / "h5.json"
    snapshot.write_text(
        json.dumps(
            {
                "nav_samples": [
                    {"observed_at": "2026-10-27T03:00:00+00:00", "nav_usdt": "1"},
                    {"observed_at": "2026-10-28T03:00:00+00:00", "nav_usdt": "1"},
                ]
            }
        )
    )
    [result] = hb.evaluate(_config(H5), {"H5": snapshot}, _ts(now))
    assert result.last_advance == _ts("2026-10-28T03:00:00+00:00")
    assert (result.idle_sessions, result.status) == (idle, status)


def test_never_advanced_ledger_counts_from_seal(tmp_path: Path) -> None:
    root = _ledger(tmp_path, ("sealed", "2026-10-28T21:05:00+00:00"))
    [result] = hb.evaluate(
        _config(ROB1391), {"ROB-1391": root}, _ts("2026-11-04T22:00:00+00:00")
    )
    assert result.status == "RED"
    assert "never advanced since seal" in result.detail


def test_missing_path_is_unknown_with_exit_2() -> None:
    results = hb.evaluate(_config(ROB1391, H5), {}, _ts("2026-11-04T22:00:00+00:00"))
    assert [r.status for r in results] == ["UNKNOWN", "UNKNOWN"]
    assert hb.exit_code(results) == 2


@pytest.mark.parametrize(
    "content",
    [
        "not json\n",
        json.dumps({"kind": "step", "recorded_at": "2026-10-29T21:00:00"}) + "\n",
        "",
    ],
)
def test_unreadable_ledger_is_unknown(tmp_path: Path, content: str) -> None:
    root = tmp_path / "ledger"
    root.mkdir()
    (root / "events.jsonl").write_text(content)
    [result] = hb.evaluate(
        _config(ROB1391), {"ROB-1391": root}, _ts("2026-11-04T22:00:00+00:00")
    )
    assert result.status == "UNKNOWN"


def test_future_advance_is_unknown(ledger: Path) -> None:
    [result] = hb.evaluate(
        _config(ROB1391), {"ROB-1391": ledger}, _ts("2026-10-29T21:00:00+00:00")
    )
    assert result.status == "UNKNOWN"


def test_repository_config_lists_rob1391_and_h5_without_private_paths() -> None:
    config = json.loads((REPO / "research/experiments/heartbeat.json").read_text())
    epochs = {e["id"]: e for e in config["epochs"]}
    assert {"ROB-1391", "H5"} <= set(epochs)
    assert config["idle_red_after_sessions"] == 3
    assert all(e["source"]["path"] is None for e in epochs.values())


def test_heartbeat_imports_no_app_broker_or_db_module() -> None:
    tree = ast.parse((REPO / "research/experiments/heartbeat.py").read_text())
    modules = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module.split(".")[0])
    allowed = {
        "__future__",
        "argparse",
        "datetime",
        "json",
        "sys",
        "dataclasses",
        "functools",
        "pathlib",
        "typing",
        "exchange_calendars",
    }
    assert modules <= allowed, modules - allowed


@pytest.mark.parametrize(
    "content",
    [
        None,
        "{not json",
        "[]",
        json.dumps({"idle_red_after_sessions": 3}),
        json.dumps(_config({**ROB1391, "calendar": "LSE"})),
    ],
)
def test_bad_config_exits_3_without_traceback(tmp_path: Path, content, capsys) -> None:
    path = tmp_path / "heartbeat.json"
    if content is not None:
        path.write_text(content)
    with pytest.raises(SystemExit) as exc:
        hb.main(["--config", str(path)])
    assert exc.value.code == hb.EXIT_USAGE
    err = capsys.readouterr().err
    assert "config:" in err
    assert "Traceback" not in err


def test_unknown_path_epoch_exits_3(capsys) -> None:
    with pytest.raises(SystemExit) as exc:
        hb.main(["--path", "NOPE=/tmp/x"])
    assert exc.value.code == hb.EXIT_USAGE
