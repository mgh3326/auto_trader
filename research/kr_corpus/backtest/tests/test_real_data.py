from __future__ import annotations

import hashlib
import json
from datetime import date
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
import real_data
from real_data import load_real_main_bars, load_real_main_stage_b_input


def _row(*, market: str, symbol: str, session: str) -> dict[str, object]:
    return {
        "session": session,
        "market": market,
        "ticker": symbol,
        "open": 100,
        "high": 101,
        "low": 99,
        "close": 100,
        "volume": 1_000,
        "value": None,
        "price_mode": "adjusted",
        "source_product": "synthetic_fixture",
    }


def _xkrx_sessions(start: date, end: date) -> list[str]:
    import exchange_calendars as xcals

    calendar = xcals.get_calendar("XKRX")
    return [
        session.date().isoformat() for session in calendar.sessions_in_range(start, end)
    ]


def _write_checksum_ledger(root: Path) -> None:
    preflight = root / "preflight.json"
    checksum_paths = [preflight, *sorted((root / "dataset").rglob("*.parquet"))]
    (root / "checksums.sha256").write_text(
        "".join(
            f"{hashlib.sha256(path.read_bytes()).hexdigest()}  "
            f"{path.relative_to(root).as_posix()}\n"
            for path in checksum_paths
        ),
        encoding="utf-8",
    )


def _write_manifest(root: Path) -> None:
    checksum_bytes = (root / "checksums.sha256").read_bytes()
    (root / "manifest.json").write_text(
        json.dumps(
            {
                "scope": "main",
                "files_list_location": "checksums.sha256",
                "checksums_sha256": hashlib.sha256(checksum_bytes).hexdigest(),
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )


def _trust_artifact(
    monkeypatch: pytest.MonkeyPatch,
    artifact_root: Path,
    run_id: str,
) -> None:
    root = artifact_root / "runs" / run_id
    monkeypatch.setattr(
        real_data,
        "_TRUSTED_STAGE_B_ARTIFACTS",
        {
            run_id: real_data._TrustedStageBArtifact(
                checksums_sha256=hashlib.sha256(
                    (root / "checksums.sha256").read_bytes()
                ).hexdigest(),
                manifest_sha256=hashlib.sha256(
                    (root / "manifest.json").read_bytes()
                ).hexdigest(),
            )
        },
    )


def _write_artifact(tmp_path: Path) -> tuple[Path, str, list[str]]:
    artifact_root = tmp_path / "artifact-root"
    run_id = "main-fixture"
    root = artifact_root / "runs" / run_id
    root.mkdir(parents=True)
    sessions = _xkrx_sessions(date(2015, 1, 2), date(2016, 1, 4))
    preflight = root / "preflight.json"
    preflight.write_text(
        json.dumps({"session_calendar": "XKRX", "sessions": sessions}),
        encoding="utf-8",
    )

    for market in ("KOSPI", "KOSDAQ"):
        for symbol in ("000001", "000002"):
            for year, session in ((2015, sessions[0]), (2016, sessions[-1])):
                path = root / "dataset" / f"market={market}" / f"year={year}"
                path = path / f"ticker={symbol}.parquet"
                path.parent.mkdir(parents=True, exist_ok=True)
                pq.write_table(
                    pa.Table.from_pylist(
                        [_row(market=market, symbol=symbol, session=session)]
                    ),
                    path,
                )

    _write_checksum_ledger(root)
    _write_manifest(root)
    return artifact_root, run_id, sessions


def test_symbol_selection_loads_all_yearly_partitions_and_records_coverage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact_root, run_id, sessions = _write_artifact(tmp_path)
    _trust_artifact(monkeypatch, artifact_root, run_id)

    loaded = load_real_main_stage_b_input(
        artifact_root=artifact_root,
        run_id=run_id,
        window_start=date(2015, 1, 1),
        window_end=date(2016, 12, 31),
        markets=("KOSPI", "KOSDAQ"),
        max_symbols=2,
    )

    assert len(loaded.bars) == 4
    assert loaded.market_sessions == {
        "KOSDAQ": tuple(date.fromisoformat(session) for session in sessions),
        "KOSPI": tuple(date.fromisoformat(session) for session in sessions),
    }
    for market in ("KOSPI", "KOSDAQ"):
        coverage = loaded.coverage["markets"][market]
        assert coverage["symbol_count"] == 1
        assert coverage["bar_count"] == 2
        assert coverage["year_count"] == 2
        assert coverage["years"] == [2015, 2016]
        assert coverage["symbols"] == {
            "000001": {
                "bar_count": 2,
                "year_count": 2,
                "years": [2015, 2016],
            }
        }
    assert loaded.coverage["session_reference"]["calendar"] == "XKRX"

    # The legacy list-returning loader remains a compatibility view of the
    # same verified input, rather than reintroducing one-partition selection.
    assert (
        len(
            load_real_main_bars(
                artifact_root=artifact_root,
                run_id=run_id,
                window_start=date(2015, 1, 1),
                window_end=date(2016, 12, 31),
                markets=("KOSPI", "KOSDAQ"),
                max_symbols=2,
            )
        )
        == 4
    )


def test_market_session_reference_requires_a_checksum(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact_root, run_id, _sessions = _write_artifact(tmp_path)
    _trust_artifact(monkeypatch, artifact_root, run_id)
    preflight = artifact_root / "runs" / run_id / "preflight.json"
    preflight.write_text(
        json.dumps({"session_calendar": "XKRX", "sessions": ["2015-01-02"]}),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="checksum mismatch: preflight.json"):
        load_real_main_stage_b_input(
            artifact_root=artifact_root,
            run_id=run_id,
            window_start=date(2015, 1, 1),
            window_end=date(2016, 12, 31),
            markets=("KOSPI",),
            max_symbols=1,
        )


def test_selected_symbol_checks_every_yearly_partition(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact_root, run_id, _sessions = _write_artifact(tmp_path)
    _trust_artifact(monkeypatch, artifact_root, run_id)
    second_year = (
        artifact_root
        / "runs"
        / run_id
        / "dataset/market=KOSPI/year=2016/ticker=000001.parquet"
    )
    second_year.write_bytes(b"tampered")

    with pytest.raises(
        ValueError,
        match="checksum mismatch: dataset/market=KOSPI/year=2016/ticker=000001.parquet",
    ):
        load_real_main_stage_b_input(
            artifact_root=artifact_root,
            run_id=run_id,
            window_start=date(2015, 1, 1),
            window_end=date(2016, 12, 31),
            markets=("KOSPI",),
            max_symbols=1,
        )


def test_checksum_ledger_rejects_duplicate_paths_even_if_other_bindings_match(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact_root, run_id, _sessions = _write_artifact(tmp_path)
    root = artifact_root / "runs" / run_id
    ledger = root / "checksums.sha256"
    preflight_row = next(
        line
        for line in ledger.read_text(encoding="utf-8").splitlines()
        if line.endswith("  preflight.json")
    )
    ledger.write_text(
        ledger.read_text(encoding="utf-8") + preflight_row + "\n",
        encoding="utf-8",
    )
    _write_manifest(root)
    _trust_artifact(monkeypatch, artifact_root, run_id)

    with pytest.raises(
        ValueError, match="checksum ledger duplicate path: preflight.json"
    ):
        load_real_main_stage_b_input(
            artifact_root=artifact_root,
            run_id=run_id,
            window_start=date(2015, 1, 1),
            window_end=date(2016, 12, 31),
            markets=("KOSPI",),
            max_symbols=1,
        )


def test_manifest_checksum_binding_rejects_a_stale_declaration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact_root, run_id, _sessions = _write_artifact(tmp_path)
    root = artifact_root / "runs" / run_id
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    manifest["checksums_sha256"] = "0" * 64
    (root / "manifest.json").write_text(
        json.dumps(manifest, sort_keys=True), encoding="utf-8"
    )
    _trust_artifact(monkeypatch, artifact_root, run_id)

    with pytest.raises(ValueError, match="manifest checksum-list digest mismatch"):
        load_real_main_stage_b_input(
            artifact_root=artifact_root,
            run_id=run_id,
            window_start=date(2015, 1, 1),
            window_end=date(2016, 12, 31),
            markets=("KOSPI",),
            max_symbols=1,
        )


def test_rewritten_preflight_ledger_and_manifest_need_a_source_root_update(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact_root, run_id, sessions = _write_artifact(tmp_path)
    _trust_artifact(monkeypatch, artifact_root, run_id)
    root = artifact_root / "runs" / run_id
    (root / "preflight.json").write_text(
        json.dumps(
            {"session_calendar": "XKRX", "sessions": [sessions[0], *sessions[2:]]}
        ),
        encoding="utf-8",
    )
    _write_checksum_ledger(root)
    _write_manifest(root)

    with pytest.raises(ValueError, match="untrusted checksum ledger digest"):
        load_real_main_stage_b_input(
            artifact_root=artifact_root,
            run_id=run_id,
            window_start=date(2015, 1, 1),
            window_end=date(2016, 12, 31),
            markets=("KOSPI",),
            max_symbols=1,
        )


def test_calendar_crosscheck_rejects_a_wrong_sequence_after_a_hypothetical_repin(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact_root, run_id, sessions = _write_artifact(tmp_path)
    root = artifact_root / "runs" / run_id
    (root / "preflight.json").write_text(
        json.dumps(
            {"session_calendar": "XKRX", "sessions": [sessions[0], *sessions[2:]]}
        ),
        encoding="utf-8",
    )
    _write_checksum_ledger(root)
    _write_manifest(root)
    _trust_artifact(monkeypatch, artifact_root, run_id)

    with pytest.raises(
        ValueError,
        match="market-session reference disagrees with XKRX calendar",
    ):
        load_real_main_stage_b_input(
            artifact_root=artifact_root,
            run_id=run_id,
            window_start=date(2015, 1, 1),
            window_end=date(2016, 12, 31),
            markets=("KOSPI",),
            max_symbols=1,
        )


def test_checksum_ledger_rejects_casefold_path_aliases(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact_root, run_id, _sessions = _write_artifact(tmp_path)
    root = artifact_root / "runs" / run_id
    ledger = root / "checksums.sha256"
    preflight_row = next(
        line
        for line in ledger.read_text(encoding="utf-8").splitlines()
        if line.endswith("  preflight.json")
    )
    ledger.write_text(
        ledger.read_text(encoding="utf-8")
        + preflight_row.replace("preflight.json", "PREFLIGHT.JSON")
        + "\n",
        encoding="utf-8",
    )
    _write_manifest(root)
    _trust_artifact(monkeypatch, artifact_root, run_id)

    with pytest.raises(
        ValueError, match="checksum ledger duplicate path: PREFLIGHT.JSON"
    ):
        load_real_main_stage_b_input(
            artifact_root=artifact_root,
            run_id=run_id,
            window_start=date(2015, 1, 1),
            window_end=date(2016, 12, 31),
            markets=("KOSPI",),
            max_symbols=1,
        )


def test_preflight_symbolic_link_is_rejected_even_with_matching_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact_root, run_id, _sessions = _write_artifact(tmp_path)
    _trust_artifact(monkeypatch, artifact_root, run_id)
    root = artifact_root / "runs" / run_id
    preflight = root / "preflight.json"
    external_preflight = tmp_path / "preflight-copy.json"
    external_preflight.write_bytes(preflight.read_bytes())
    preflight.unlink()
    preflight.symlink_to(external_preflight)

    with pytest.raises(
        ValueError, match="market-session reference must not use a symbolic link"
    ):
        load_real_main_stage_b_input(
            artifact_root=artifact_root,
            run_id=run_id,
            window_start=date(2015, 1, 1),
            window_end=date(2016, 12, 31),
            markets=("KOSPI",),
            max_symbols=1,
        )


def test_frozen_main_snapshot_trust_root_is_code_pinned() -> None:
    assert real_data._TRUSTED_STAGE_B_ARTIFACTS == {
        "kr-corpus-v1-20260803-1001": real_data._TrustedStageBArtifact(
            checksums_sha256=(
                "9704cc72455bca8bc8bdea78506b16de4d0cdff697661d7ee8a349eb4b311a7f"
            ),
            manifest_sha256=(
                "da1ca376ac6693e96d311eda07f9fe96f1cb69fa2e3e8f346ededf96c5d5c54b"
            ),
        )
    }


def test_unlisted_stage_b_run_id_fails_closed(tmp_path: Path) -> None:
    artifact_root, run_id, _sessions = _write_artifact(tmp_path)

    with pytest.raises(ValueError, match="untrusted Stage-B artifact run id"):
        load_real_main_stage_b_input(
            artifact_root=artifact_root,
            run_id=run_id,
            window_start=date(2015, 1, 1),
            window_end=date(2016, 12, 31),
            markets=("KOSPI",),
            max_symbols=1,
        )
