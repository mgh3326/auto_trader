"""Contract §2-2 — 표 부재/STALE 이면 그 사이클은 주문 0, 사유 기록."""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pytest

from scripts.b0x.table_source import (
    MAX_TABLE_AGE,
    PolicyTable,
    TableReason,
    TableUnavailable,
    load_policy_table,
)
from tests.scripts.b0x._table_fixtures import (
    make_payload,
    make_row,
    write_stale_marker,
    write_table,
)

pytestmark = pytest.mark.unit

NOW = dt.datetime(2026, 8, 8, 12, 0, tzinfo=dt.UTC)


def _good_payload(*, generated_at: dt.datetime | None = None) -> dict:
    return make_payload(
        rows=[
            make_row(
                symbol="KRW-BTC",
                previous_close="100",
                buy_l1="97",
                sell_r1="103",
                sell_r2="106",
            )
        ],
        generated_at=generated_at or (NOW - dt.timedelta(hours=1)),
    )


def test_happy_path_returns_a_validated_table(tmp_path: Path) -> None:
    write_table(tmp_path, _good_payload())
    result = load_policy_table(market="crypto", now=NOW, table_dir=tmp_path)
    assert isinstance(result, PolicyTable)
    assert result.policy_table_hash.startswith("sha256:")
    assert len(result.rows) == 1


def test_missing_table_yields_zero_orders(tmp_path: Path) -> None:
    result = load_policy_table(market="crypto", now=NOW, table_dir=tmp_path)
    assert isinstance(result, TableUnavailable)
    assert result.reason == TableReason.TABLE_MISSING


def test_stale_marker_beats_a_present_table(tmp_path: Path) -> None:
    """A readable, fresh, hash-valid table is still refused when the generator
    flagged STALE — the marker is the generator saying "do not trust me"."""

    write_table(tmp_path, _good_payload())
    write_stale_marker(tmp_path)
    result = load_policy_table(market="crypto", now=NOW, table_dir=tmp_path)
    assert isinstance(result, TableUnavailable)
    assert result.reason == TableReason.STALE_MARKER_PRESENT
    assert "builder failed" in result.detail


def test_edited_artifact_fails_the_hash_check(tmp_path: Path) -> None:
    """Hand-editing a row after generation must not silently change orders."""

    latest = write_table(tmp_path, _good_payload())
    target = latest.resolve()
    payload = json.loads(target.read_text())
    payload["rows"][0]["A_buy_side"]["buy_l1"]["price"] = "1"  # a far better entry
    target.write_text(json.dumps(payload, indent=2))

    result = load_policy_table(market="crypto", now=NOW, table_dir=tmp_path)
    assert isinstance(result, TableUnavailable)
    assert result.reason == TableReason.HASH_MISMATCH


def test_table_older_than_max_age_is_stale(tmp_path: Path) -> None:
    old = NOW - MAX_TABLE_AGE["crypto"] - dt.timedelta(minutes=1)
    write_table(tmp_path, _good_payload(generated_at=old))
    result = load_policy_table(market="crypto", now=NOW, table_dir=tmp_path)
    assert isinstance(result, TableUnavailable)
    assert result.reason == TableReason.STALE_BY_AGE


def test_table_exactly_at_max_age_still_passes(tmp_path: Path) -> None:
    edge = NOW - MAX_TABLE_AGE["crypto"]
    write_table(tmp_path, _good_payload(generated_at=edge))
    assert isinstance(
        load_policy_table(market="crypto", now=NOW, table_dir=tmp_path), PolicyTable
    )


def test_future_stamped_table_is_refused(tmp_path: Path) -> None:
    write_table(tmp_path, _good_payload(generated_at=NOW + dt.timedelta(hours=3)))
    result = load_policy_table(market="crypto", now=NOW, table_dir=tmp_path)
    assert isinstance(result, TableUnavailable)
    assert result.reason == TableReason.STALE_BY_AGE


def test_wrong_market_is_refused(tmp_path: Path) -> None:
    payload = make_payload(
        rows=[make_row(symbol="005930", previous_close="100", buy_l1="97")],
        generated_at=NOW - dt.timedelta(hours=1),
        market="kr",
    )
    # Write the KR payload under the crypto filename — the market field is what
    # decides, not the filename.
    directory = tmp_path
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "latest-crypto.json").write_text(json.dumps(payload))
    result = load_policy_table(market="crypto", now=NOW, table_dir=directory)
    assert isinstance(result, TableUnavailable)
    assert result.reason == TableReason.SCHEMA_MISMATCH


def test_unparseable_table_is_refused(tmp_path: Path) -> None:
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "latest-crypto.json").write_text("{not json")
    result = load_policy_table(market="crypto", now=NOW, table_dir=tmp_path)
    assert isinstance(result, TableUnavailable)
    assert result.reason == TableReason.UNREADABLE


def test_loader_never_raises_for_any_broken_input(tmp_path: Path) -> None:
    """Every failure mode is a recordable outcome, not an exception."""

    for content in (
        "",
        "null",
        "[]",
        '{"schema": "other"}',
        '{"schema": "policy_table.v1"}',
    ):
        (tmp_path / "latest-crypto.json").write_text(content)
        result = load_policy_table(market="crypto", now=NOW, table_dir=tmp_path)
        assert isinstance(result, TableUnavailable), content
