"""ROB-1438 acceptance: allowlisted columns, joinable hashes, nothing identifying."""

from __future__ import annotations

import csv
import hashlib
import json
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pyarrow.parquet as pq
import pytest

from app.models import order_proposals as order_proposals_model
from app.services.dots_history_export.projection import (
    EXPIRED_INFERENCE_MARKER,
    RefHasher,
    SaltError,
)
from app.services.dots_history_export.publish import MANIFEST_NAME, publish
from app.services.dots_history_export.spec import TABLE_BY_NAME, TABLES
from app.services.order_proposals.kis_leftover_inference import (
    EXPIRED_INFERENCE_VOID_REASON,
)
from tests.services.dots_history_export._fixtures import (
    ACCOUNT_NUMBER,
    CREATED,
    KIS_ORDER_NO,
    RAW_IDENTIFIERS,
    SALT,
    TOSS_ORDER_ID,
    WINDOW,
    alpaca_row,
    fill_row,
    kis_live_row,
    kis_mock_row,
    live_row,
    paper_pending_row,
    projected,
    proposal_row,
    rung_row,
    toss_row,
)

pytestmark = pytest.mark.unit

COMMIT = "a" * 40


def _publish(tmp_path: Path, **tables: list[dict[str, Any]]) -> tuple[Path, dict]:
    out = tmp_path / "export"
    hasher = RefHasher(SALT)
    manifest = publish(
        projected(**tables),
        out_dir=out,
        window=WINDOW,
        salt=SALT,
        salt_fingerprint=hasher.fingerprint(),
        code_commit=COMMIT,
        code_dirty=False,
    )
    return out, manifest


def _csv_rows(out: Path, table: str) -> list[dict[str, str]]:
    with (out / f"{table}.csv").open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def _only(out: Path, table: str) -> dict[str, str]:
    [row] = _csv_rows(out, table)
    return row


def test_every_table_has_exactly_its_allowlisted_columns_in_both_formats(
    tmp_path: Path,
) -> None:
    out, _ = _publish(tmp_path)
    for spec in TABLES:
        with (out / f"{spec.name}.csv").open(encoding="utf-8", newline="") as handle:
            assert next(csv.reader(handle)) == list(spec.column_names)
        table = pq.read_table(out / f"{spec.name}.parquet")
        assert table.column_names == list(spec.column_names)
        assert table.num_rows == 1


def test_files_are_exactly_two_per_table_plus_the_manifest(tmp_path: Path) -> None:
    out, _ = _publish(tmp_path)
    expected = {f"{t.name}.{s}" for t in TABLES for s in ("csv", "parquet")}
    assert {path.name for path in out.iterdir()} == expected | {MANIFEST_NAME}


def test_order_fill_and_rung_rows_join_on_the_same_order_hash(tmp_path: Path) -> None:
    out, _ = _publish(tmp_path)
    order = _only(out, "kis_live_order_ledger")
    fill = _only(out, "execution_ledger")
    rung = _only(out, "order_proposal_rungs")
    # The ledger keeps "0012345678" and the fill "12345678": one broker order.
    assert order["order_ref"] == fill["order_ref"] == rung["order_ref"]
    assert order["order_ref"].startswith("ord_")
    assert (
        order["correlation_ref"] == fill["correlation_ref"] == rung["correlation_ref"]
    )
    assert order["trading_day"] == fill["trading_day"] == "2026-06-10"


def test_proposals_and_rungs_join_on_the_proposal_hash(tmp_path: Path) -> None:
    out, _ = _publish(tmp_path)
    proposal = _only(out, "order_proposals")
    rung = _only(out, "order_proposal_rungs")
    assert proposal["proposal_ref"] == rung["proposal_ref"]
    assert proposal["proposal_ref"] == proposal["root_proposal_ref"]
    assert proposal["account_alias"] == rung["account_alias"] == "kis_live"


def test_paper_trade_joins_the_pending_order_it_filled(tmp_path: Path) -> None:
    out, _ = _publish(tmp_path)
    pending = _only(out, "paper_pending_orders")
    trade = _only(out, "paper_trades")
    assert pending["order_ref"] == trade["order_ref"]
    assert pending["account_ref"] == trade["account_ref"]
    assert pending["account_alias"] == trade["account_alias"] == "db_paper"


def test_hashes_are_stable_for_one_salt_and_change_with_another(tmp_path: Path) -> None:
    one = projected().tables["kis_live_order_ledger"][0]
    two = projected().tables["kis_live_order_ledger"][0]
    other = projected(salt=SALT + b"-other").tables["kis_live_order_ledger"][0]
    assert one["order_ref"] == two["order_ref"]
    assert one["order_ref"] != other["order_ref"]


def test_a_short_salt_is_refused_without_repeating_it() -> None:
    with pytest.raises(SaltError) as raised:
        RefHasher(b"short-salt-value")
    assert "short-salt-value" not in str(raised.value)


def test_no_identifier_survives_in_any_file_or_the_salt(tmp_path: Path) -> None:
    out, _ = _publish(tmp_path)
    needles = [*RAW_IDENTIFIERS, SALT.decode()]
    for path in out.iterdir():
        if path.suffix == ".parquet":
            text = json.dumps(pq.read_table(path).to_pylist(), default=str)
            metadata = pq.read_metadata(path).metadata or {}
            text += str(metadata)
        else:
            text = path.read_text("utf-8")
        for needle in needles:
            assert needle not in text, f"{needle} found in {path.name}"


def test_account_alias_is_the_only_account_field_on_a_live_order(
    tmp_path: Path,
) -> None:
    out, _ = _publish(tmp_path)
    assert _only(out, "kis_live_order_ledger")["account_alias"] == "kis_live"
    assert _only(out, "toss_live_order_ledger")["account_alias"] == "toss_live"
    assert _only(out, "live_order_ledger")["account_alias"] == "upbit_live"
    assert _only(out, "kis_mock_order_ledger")["account_alias"] == "kis_mock"
    assert _only(out, "alpaca_paper_order_ledger")["account_alias"] == "alpaca_paper"
    for spec in TABLES:
        for name in spec.column_names:
            assert "account_no" not in name and "account_number" not in name
            assert name not in ("order_no", "broker_order_id", "client_order_id")


def test_timestamps_are_written_in_utc_and_market_local_time(tmp_path: Path) -> None:
    out, _ = _publish(tmp_path)
    kr = _only(out, "kis_live_order_ledger")
    assert kr["created_at_utc"] == "2026-06-10T01:00:00Z"
    assert kr["created_at_local"] == "2026-06-10T10:00:00+09:00"
    assert kr["local_tz"] == "Asia/Seoul"
    us = _only(out, "toss_live_order_ledger")
    assert us["created_at_local"] == "2026-06-09T21:00:00-04:00"
    assert us["expired_at_utc"] == "2026-06-10T12:00:00Z"


def test_us_symbol_is_written_in_db_form(tmp_path: Path) -> None:
    out, _ = _publish(tmp_path)
    assert _only(out, "toss_live_order_ledger")["symbol"] == "BRK.B"


def test_exact_decimals_survive_both_formats(tmp_path: Path) -> None:
    out, _ = _publish(tmp_path)
    row = _only(out, "live_order_ledger")
    assert row["limit_price"] == "150000000.00000000"
    table = pq.read_table(out / "live_order_ledger.parquet").to_pylist()[0]
    assert format(table["limit_price"], "f") == "150000000.000000000000"


@pytest.mark.parametrize(
    ("table", "row", "expected"),
    [
        ("kis_live_order_ledger", kis_live_row(status="filled"), "filled"),
        ("kis_live_order_ledger", kis_live_row(status="partial"), "partial"),
        (
            "kis_live_order_ledger",
            kis_live_row(status="expired", filled_qty=None),
            "expired",
        ),
        (
            "kis_live_order_ledger",
            kis_live_row(status="cancelled", filled_qty=None),
            "cancelled",
        ),
        (
            "kis_live_order_ledger",
            kis_live_row(status="rejected", filled_qty=None),
            "rejected",
        ),
        (
            "kis_live_order_ledger",
            kis_live_row(status="accepted", filled_qty=None),
            "open",
        ),
        (
            "kis_live_order_ledger",
            kis_live_row(status="pending", filled_qty=None),
            "open",
        ),
        (
            "kis_live_order_ledger",
            kis_live_row(status="anomaly", filled_qty=None),
            "anomaly",
        ),
        (
            "kis_live_order_ledger",
            kis_live_row(status="strange", filled_qty=None),
            "unknown",
        ),
        ("live_order_ledger", live_row(), "partial"),
        ("live_order_ledger", live_row(filled_qty=None), "cancelled"),
        ("toss_live_order_ledger", toss_row(status="expired"), "expired"),
        ("toss_live_order_ledger", toss_row(status="replaced"), "replaced"),
        ("toss_live_order_ledger", toss_row(status="cancel_rejected"), "rejected"),
        ("kis_mock_order_ledger", kis_mock_row(lifecycle_state="expired"), "expired"),
        ("kis_mock_order_ledger", kis_mock_row(lifecycle_state="fill"), "filled"),
        ("kis_mock_order_ledger", kis_mock_row(lifecycle_state="stale"), "open"),
        ("kis_mock_order_ledger", kis_mock_row(status="rejected"), "rejected"),
        ("alpaca_paper_order_ledger", alpaca_row(), "open"),
        ("alpaca_paper_order_ledger", alpaca_row(order_status="filled"), "filled"),
        ("alpaca_paper_order_ledger", alpaca_row(order_status="canceled"), "cancelled"),
        (
            "alpaca_paper_order_ledger",
            alpaca_row(record_kind="preview", order_status=None),
            "not_submitted",
        ),
        (
            "alpaca_paper_order_ledger",
            alpaca_row(
                order_status=None,
                lifecycle_state="final_reconciled",
                filled_qty=Decimal("1"),
            ),
            "filled",
        ),
        (
            "alpaca_paper_order_ledger",
            alpaca_row(order_status=None, lifecycle_state="final_reconciled"),
            "unknown",
        ),
        (
            "alpaca_paper_order_ledger",
            alpaca_row(order_status="new", lifecycle_state="anomaly"),
            "anomaly",
        ),
        (
            "alpaca_paper_order_ledger",
            alpaca_row(order_status="filled", record_kind="reconcile"),
            "filled",
        ),
        (
            "toss_live_order_ledger",
            toss_row(status="replaced", filled_qty=Decimal("1")),
            "partial",
        ),
        (
            "kis_mock_order_ledger",
            kis_mock_row(lifecycle_state="fill", attributed_fill_qty="1"),
            "partial",
        ),
        (
            "kis_mock_order_ledger",
            kis_mock_row(lifecycle_state="fill", attributed_fill_qty="2"),
            "filled",
        ),
        (
            "kis_mock_order_ledger",
            kis_mock_row(lifecycle_state="fill", attributed_fill_qty="0"),
            "anomaly",
        ),
        (
            "kis_mock_order_ledger",
            kis_mock_row(lifecycle_state="reconciled", attributed_fill_qty="1"),
            "partial",
        ),
        ("paper_pending_orders", paper_pending_row(status="pending"), "open"),
        ("paper_pending_orders", paper_pending_row(status="cancelled"), "cancelled"),
    ],
)
def test_terminal_outcome_mapping(
    table: str, row: dict[str, Any], expected: str
) -> None:
    [output] = projected(**{table: [row]}).tables[table]
    assert output["terminal_outcome"] == expected


def test_an_accepted_order_with_no_fill_row_stays_open_with_no_fill_time(
    tmp_path: Path,
) -> None:
    out, _ = _publish(
        tmp_path,
        kis_live_order_ledger=[kis_live_row(status="accepted", filled_qty=None)],
        execution_ledger=[],
    )
    row = _only(out, "kis_live_order_ledger")
    assert row["terminal_outcome"] == "open"
    assert row["filled_at_utc"] == ""
    assert row["accepted_at_utc"] == "2026-06-10T01:00:00Z"


def test_a_rejected_order_was_never_accepted(tmp_path: Path) -> None:
    out, _ = _publish(
        tmp_path,
        kis_live_order_ledger=[kis_live_row(status="rejected", filled_qty=None)],
    )
    assert _only(out, "kis_live_order_ledger")["accepted_at_utc"] == ""


def test_order_filled_time_comes_from_the_fill_rows_it_was_given(
    tmp_path: Path,
) -> None:
    out, _ = _publish(tmp_path)
    assert (
        _only(out, "kis_live_order_ledger")["filled_at_utc"] == "2026-06-10T01:30:00Z"
    )


def test_a_reused_kis_order_number_on_another_day_does_not_claim_the_fill(
    tmp_path: Path,
) -> None:
    out, _ = _publish(
        tmp_path,
        execution_ledger=[fill_row(filled_at=CREATED + timedelta(days=3))],
    )
    order = _only(out, "kis_live_order_ledger")
    assert order["filled_at_utc"] == ""
    assert order["trading_day"] != _only(out, "execution_ledger")["trading_day"]


def test_a_cross_source_duplicate_is_flagged_not_silently_double_counted(
    tmp_path: Path,
) -> None:
    out, _ = _publish(
        tmp_path,
        execution_ledger=[
            fill_row(id=20, source="reconciler"),
            fill_row(id=21, source="websocket"),
        ],
    )
    rows = {row["fill_source"]: row for row in _csv_rows(out, "execution_ledger")}
    assert rows["reconciler"]["superseded_duplicate"] == "false"
    assert rows["websocket"]["superseded_duplicate"] == "true"
    assert rows["reconciler"]["order_ref"] == rows["websocket"]["order_ref"]


def test_a_kis_mock_partial_reports_the_attributed_quantity() -> None:
    [row] = projected(
        kis_mock_order_ledger=[
            kis_mock_row(lifecycle_state="fill", attributed_fill_qty="1")
        ]
    ).tables["kis_mock_order_ledger"]
    assert row["filled_qty"] == "1"
    assert row["terminal_outcome"] == "partial"


def test_a_legacy_kis_mock_fill_without_attribution_counts_as_full() -> None:
    [row] = projected(
        kis_mock_order_ledger=[kis_mock_row(lifecycle_state="fill")]
    ).tables["kis_mock_order_ledger"]
    assert row["filled_qty"] == "2"
    assert row["terminal_outcome"] == "filled"


def test_a_reconciler_row_outside_the_window_does_not_hide_the_exported_fill() -> None:
    [row] = projected(
        execution_ledger=[
            fill_row(
                id=20,
                source="reconciler",
                filled_at=WINDOW.since - timedelta(minutes=1),
            ),
            fill_row(
                id=21,
                source="websocket",
                filled_at=WINDOW.since + timedelta(minutes=1),
            ),
        ]
    ).tables["execution_ledger"]
    assert row["fill_source"] == "websocket"
    assert row["superseded_duplicate"] == "false"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("expired_valid_until_night_sweep", "expired_valid_until_night_sweep"),
        (
            "expired_inference:kis_regular_day_order_no_broker_original",
            "expired_inference:kis_regular_day_order_no_broker_original",
        ),
        ("submit_exception:account 1234567 name 문광현 rejected", "submit_exception"),
        ("cancel_unconfirmed:12345", "cancel_unconfirmed"),
        ("submit_exception:alice", "submit_exception"),
        ("submit_exception:alice:bob", "submit_exception"),
        ("expired_inference:alice", "expired_inference"),
        ("submit_exception:ConnectError(host down)", "submit_exception"),
    ],
)
def test_void_reason_keeps_the_code_and_drops_the_detail(
    raw: str, expected: str
) -> None:
    projection = projected(
        order_proposals=[proposal_row(void_reason=raw)],
        order_proposal_rungs=[rung_row(void_reason=raw)],
    )
    assert projection.tables["order_proposals"][0]["void_reason"] == expected
    assert projection.tables["order_proposal_rungs"][0]["void_reason"] == expected


@pytest.mark.parametrize(
    ("stored", "market", "local_tz"),
    [
        ("equity_kr", "kr", "Asia/Seoul"),
        ("equity_us", "us", "America/New_York"),
        ("crypto", "crypto", "Asia/Seoul"),
        ("forex", "forex", "UTC"),
        ("index", "index", "UTC"),
    ],
)
def test_proposal_market_vocabulary_maps_to_a_real_market(
    stored: str, market: str, local_tz: str
) -> None:
    # The column stores the instrument form, not kr/us. See the CHECK below.
    assert f"'{stored}'" in order_proposals_model._MARKETS
    projection = projected(
        order_proposals=[proposal_row(market=stored)],
        order_proposal_rungs=[rung_row(market=stored)],
    )
    for table in ("order_proposals", "order_proposal_rungs"):
        [row] = projection.tables[table]
        assert row["market"] == market
        assert row["local_tz"] == local_tz


def test_every_stored_proposal_market_is_covered_by_the_test_above() -> None:
    assert order_proposals_model._MARKETS == (
        "'equity_kr','equity_us','crypto','forex','index'"
    )


def test_the_kept_void_reason_marker_is_the_ledger_marker() -> None:
    assert EXPIRED_INFERENCE_MARKER == EXPIRED_INFERENCE_VOID_REASON


@pytest.mark.parametrize("raw", ["Submit_Exception:alice", "http2:alice", ":alice"])
def test_a_colon_value_without_a_code_is_hashed_even_when_it_looks_safe(
    raw: str,
) -> None:
    [row] = projected(order_proposals=[proposal_row(void_reason=raw)]).tables[
        "order_proposals"
    ]
    assert row["void_reason"].startswith("oth_")
    assert "alice" not in row["void_reason"]


def test_an_alphabetic_void_reason_suffix_never_reaches_a_file(tmp_path: Path) -> None:
    raw = "submit_exception:alice"
    out, manifest = _publish(
        tmp_path,
        order_proposals=[proposal_row(void_reason=raw)],
        order_proposal_rungs=[rung_row(void_reason=raw)],
    )
    for table in ("order_proposals", "order_proposal_rungs"):
        assert "alice" not in (out / f"{table}.csv").read_text("utf-8")
        cells = json.dumps(
            pq.read_table(out / f"{table}.parquet").to_pylist(), default=str
        )
        assert "alice" not in cells
        assert manifest["tables"][table]["tags_hashed"] == 1


def test_a_void_reason_with_no_code_is_hashed_whole() -> None:
    raw = "계좌 1234567 거부"
    [row] = projected(order_proposals=[proposal_row(void_reason=raw)]).tables[
        "order_proposals"
    ]
    assert row["void_reason"].startswith("oth_")


@pytest.mark.parametrize(
    "strategy",
    [
        "strategy_20261001",
        "반도체눌림목",
        "account 1234567 name 문광현",
        "87654321-01",
        "tossp6-a1b2c3d4e5f6a7b8",
        "0f3c7e8a-1b2d-4c5e-8f90-a1b2c3d4e5f6",
    ],
)
def test_a_label_that_could_identify_is_hashed_and_does_not_block_the_export(
    tmp_path: Path, strategy: str
) -> None:
    out, manifest = _publish(
        tmp_path, kis_live_order_ledger=[kis_live_row(strategy=strategy)]
    )
    assert _only(out, "kis_live_order_ledger")["strategy_tag"].startswith("oth_")
    assert strategy not in (out / "kis_live_order_ledger.csv").read_text("utf-8")
    assert manifest["tables"]["kis_live_order_ledger"]["tags_hashed"] == 1


def test_a_correlation_id_equal_to_a_strategy_label_does_not_block_the_export(
    tmp_path: Path,
) -> None:
    _publish(
        tmp_path,
        kis_live_order_ledger=[
            kis_live_row(strategy="dip-v2", correlation_id="dip-v2")
        ],
    )


def test_a_paper_order_symbol_with_a_dash_is_crypto_only_for_a_crypto_quote() -> None:
    [crypto, us] = projected(
        paper_pending_orders=[
            paper_pending_row(id=1, symbol="KRW-ETH"),
            paper_pending_row(id=2, symbol="BRK-B"),
        ]
    ).tables["paper_pending_orders"]
    assert crypto["market"] == "crypto"
    assert us["market"] == "us"
    assert us["symbol"] == "BRK.B"


def test_a_fill_outside_the_window_is_read_but_not_exported(tmp_path: Path) -> None:
    out, manifest = _publish(
        tmp_path,
        execution_ledger=[fill_row(filled_at=WINDOW.until + timedelta(days=1))],
    )
    assert _csv_rows(out, "execution_ledger") == []
    assert manifest["tables"]["execution_ledger"]["row_count"] == 0


def test_manifest_records_window_counts_columns_hashes_and_commit(
    tmp_path: Path,
) -> None:
    out, manifest = _publish(
        tmp_path,
        execution_ledger=[fill_row(id=20), fill_row(id=21, source="websocket")],
    )
    on_disk = json.loads((out / MANIFEST_NAME).read_text("utf-8"))
    assert on_disk == manifest
    assert on_disk["schema_version"] == "dots-history-export/v1"
    assert on_disk["code_commit"] == COMMIT
    assert on_disk["code_dirty"] is False
    assert on_disk["window"]["since_utc"] == "2026-06-01T00:00:00+00:00"
    assert on_disk["window"]["until_utc"] == "2026-07-01T00:00:00+00:00"
    assert on_disk["leak_check"]["status"] == "passed"
    assert on_disk["tables"]["execution_ledger"]["row_count"] == 2
    assert on_disk["tables"]["kis_live_order_ledger"]["row_count"] == 1
    assert "kiwoom_mock" in on_disk["sources_without_ledger"]
    for spec in TABLES:
        entry = on_disk["tables"][spec.name]
        assert entry["columns"] == list(TABLE_BY_NAME[spec.name].column_names)
        for suffix in ("csv", "parquet"):
            recorded = entry["files"][suffix]
            data = (out / f"{spec.name}.{suffix}").read_bytes()
            assert recorded["sha256"] == hashlib.sha256(data).hexdigest()
            assert recorded["bytes"] == len(data)
    text = (out / MANIFEST_NAME).read_text("utf-8")
    assert str(tmp_path) not in text
    assert SALT.decode() not in text
    assert ACCOUNT_NUMBER not in text
    assert KIS_ORDER_NO not in text
    assert TOSS_ORDER_ID not in text


def test_published_files_are_private_to_the_owner(tmp_path: Path) -> None:
    out, _ = _publish(tmp_path)
    assert out.stat().st_mode & 0o077 == 0
    for path in out.iterdir():
        assert path.stat().st_mode & 0o077 == 0
