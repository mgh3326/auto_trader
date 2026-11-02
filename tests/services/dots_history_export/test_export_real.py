"""ROB-1465: the real-value mode keeps the data and still refuses secrets."""

from __future__ import annotations

import asyncio
import csv
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from app.services.dots_history_export.leak_check import (
    REAL_LEAK_RULE_NAMES,
    LeakCheckFailed,
    Violation,
)
from app.services.dots_history_export.projection import (
    Projection,
    RefHasher,
    fill_lot_links,
    fill_sequences,
)
from app.services.dots_history_export.publish import MANIFEST_NAME, publish
from app.services.dots_history_export.spec import Mode, tables_for
from scripts import export_dots_history as cli
from tests.services.dots_history_export._fixtures import (
    ACCOUNT_NUMBER,
    CREATED,
    FILLED,
    KIS_ORDER_NO,
    SALT,
    UPBIT_ORDER_ID,
    WINDOW,
    fill_row,
    kis_live_row,
    paper_pending_row,
    proposal_row,
    rung_row,
    sources,
    toss_row,
)

pytestmark = pytest.mark.unit

COMMIT = "d" * 40
REAL_TABLES = tables_for(Mode.REAL)
REAL_BY_NAME = {table.name: table for table in REAL_TABLES}


def projected_real(**tables: list[dict[str, Any]]) -> Projection:
    from app.services.dots_history_export.projection import project

    return project(sources(**tables), hasher=None, window=WINDOW, mode=Mode.REAL)


def _publish(projection: Projection, out: Path, **kwargs) -> dict:
    return publish(
        projection,
        out_dir=out,
        window=WINDOW,
        salt=None,
        salt_fingerprint="none",
        code_commit=COMMIT,
        code_dirty=False,
        mode=Mode.REAL,
        **kwargs,
    )


def _only(out: Path, table: str) -> dict[str, str]:
    with (out / f"{table}.csv").open(encoding="utf-8", newline="") as handle:
        [row] = list(csv.DictReader(handle))
    return row


def test_real_mode_keeps_symbols_prices_and_plain_labels() -> None:
    [order] = projected_real(
        kis_live_order_ledger=[kis_live_row(strategy="반도체 눌림목 v2")]
    ).tables["kis_live_order_ledger"]
    assert order["symbol"] == "005930"
    assert order["limit_price"] == "70000.0000"
    assert order["qty"] == "10.00000000"
    assert order["strategy_tag"] == "반도체 눌림목 v2"
    assert order["status"] == "filled"
    assert order["venue"] in ("", None)


def test_real_mode_keeps_broker_order_ids_and_joins_them() -> None:
    projection = projected_real()
    order = projection.tables["kis_live_order_ledger"][0]
    fill = projection.tables["execution_ledger"][0]
    # The ledger keeps "0012345678", the fill "12345678": one broker order.
    assert order["order_ref"] == fill["order_ref"] == "12345678"
    assert projection.tables["order_proposal_rungs"][0]["order_ref"] == "12345678"
    assert projection.tables["order_proposals"][0]["proposal_ref"].startswith(
        "11111111-2222"
    )


def test_real_mode_masks_the_account_it_read() -> None:
    [pending] = projected_real(
        paper_pending_orders=[paper_pending_row(account_id=87654321)]
    ).tables["paper_pending_orders"]
    assert pending["account_ref"] == "****4321"
    projection = projected_real()
    assert ACCOUNT_NUMBER not in json.dumps(projection.tables, default=str)


def test_real_mode_adds_the_first_fill_time() -> None:
    [order] = projected_real(
        execution_ledger=[
            fill_row(id=30, filled_at=FILLED),
            fill_row(id=31, filled_at=FILLED + timedelta(minutes=5)),
        ]
    ).tables["kis_live_order_ledger"]
    assert order["first_fill_at_utc"] == "2026-06-10T01:30:00Z"
    assert order["filled_at_utc"] == "2026-06-10T01:35:00Z"


@pytest.mark.parametrize(
    ("value", "rule"),
    [
        (ACCOUNT_NUMBER, "account_number"),
        ("api_key=abcdef123456", "credential_assignment"),
        ("Bearer plain-opaque-token-value", "bearer"),
        ("ghp_abcdefghijklmnop1234", "token_prefix"),
        ("eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0In0.c2ln", "jwt"),
    ],
)
def test_real_mode_refuses_a_credential_or_full_account_number(
    tmp_path: Path, value: str, rule: str
) -> None:
    projection = projected_real(kis_live_order_ledger=[kis_live_row(strategy=value)])
    out = tmp_path / "export"
    with pytest.raises(LeakCheckFailed) as raised:
        _publish(projection, out)
    assert Violation("kis_live_order_ledger", "strategy_tag", 1, rule) in (
        raised.value.violations
    )
    assert list(tmp_path.iterdir()) == []


def test_real_mode_refuses_an_account_number_read_from_another_row(
    tmp_path: Path,
) -> None:
    projection = projected_real(
        order_proposals=[proposal_row(strategy="desk acct " + ACCOUNT_NUMBER)]
    )
    with pytest.raises(LeakCheckFailed) as raised:
        _publish(projection, tmp_path / "export")
    assert {item.rule for item in raised.value.violations} == {"account_number"}


def test_real_mode_refuses_an_operator_supplied_deny_value(tmp_path: Path) -> None:
    projection = projected_real(
        kis_live_order_ledger=[kis_live_row(strategy="desk account 88776655")]
    )
    with pytest.raises(LeakCheckFailed) as raised:
        _publish(projection, tmp_path / "export", extra_forbidden={"88776655"})
    assert {item.rule for item in raised.value.violations} == {"forbidden_value"}


def test_real_mode_writes_every_table_and_records_the_mode(tmp_path: Path) -> None:
    out = tmp_path / "export"
    manifest = _publish(projected_real(), out)
    on_disk = json.loads((out / MANIFEST_NAME).read_text("utf-8"))
    assert on_disk == manifest
    assert manifest["schema_version"] == "dots-history-export/real-v1"
    assert manifest["mode"] == "real"
    assert manifest["salt_fingerprint"] == "none"
    assert set(manifest["leak_check"]["rules"]) == set(REAL_LEAK_RULE_NAMES)
    assert set(manifest["tables"]) == {table.name for table in REAL_TABLES}
    assert {
        "first_fill_at_utc",
        "first_fill_at_local",
    } <= set(manifest["tables"]["kis_live_order_ledger"]["columns"])
    assert RELEVANT_LOT_COLUMNS <= set(
        manifest["tables"]["execution_ledger"]["columns"]
    )


RELEVANT_LOT_COLUMNS = {
    "lot_ref",
    "lot_link_kind",
    "lot_opened_at_utc",
    "lot_cost_basis",
    "lot_basis_kind",
    "linked_fill_ref",
}


def test_real_fill_carries_lot_links_for_an_add_buy() -> None:
    fills = [
        fill_row(
            id=40,
            source="websocket",
            filled_qty=Decimal("10"),
            filled_price=Decimal("100"),
            filled_at=CREATED,
        ),
        fill_row(
            id=41,
            source="websocket",
            filled_qty=Decimal("5"),
            filled_price=Decimal("80"),
            filled_at=CREATED + timedelta(hours=1),
        ),
    ]
    sequences = fill_sequences(fills)
    links = fill_lot_links(fills, sequences)
    assert links[40]["lot_link_kind"] == "opens_lot"
    assert links[41]["lot_link_kind"] == "adds_to_lot"
    assert links[41]["lot_cost_basis"] == "100.000000000000"
    assert links[41]["lot_opened_at_utc"] == "2026-06-10T01:00:00Z"
    assert links[41]["linked_fill_ref"] == "execution_ledger:40"


def test_real_seed_fill_sets_the_period_start_cost_basis() -> None:
    seed = fill_row(
        id=50,
        source="manual_import",
        broker_order_id="SEED-20260929-005930",
        filled_qty=Decimal("100"),
        filled_price=Decimal("70000"),
        filled_at=CREATED,
    )
    sequences = fill_sequences([seed])
    links = fill_lot_links([seed], sequences)
    assert links[50]["lot_basis_kind"] == "period_start_seed"
    assert links[50]["lot_cost_basis"] == "70000"
    assert links[50]["lot_ref"] == "lot:kis_live:005930:50"


def test_real_partial_sell_links_to_the_lot_it_reduced() -> None:
    buy = fill_row(
        id=60,
        source="websocket",
        side="buy",
        filled_qty=Decimal("10"),
        filled_price=Decimal("100"),
        filled_at=CREATED,
    )
    sell = fill_row(
        id=61,
        source="websocket",
        side="sell",
        filled_qty=Decimal("4"),
        filled_price=Decimal("120"),
        filled_at=CREATED + timedelta(hours=2),
    )
    sequences = fill_sequences([buy, sell])
    links = fill_lot_links([buy, sell], sequences)
    assert links[61]["lot_link_kind"] == "partial_sell"
    assert links[61]["lot_ref"] == links[60]["lot_ref"]
    assert links[61]["lot_cost_basis"] == "100.000000000000"


def test_real_sell_with_no_open_lot_is_unlinked_not_guessed() -> None:
    sell = fill_row(
        id=70,
        side="sell",
        filled_qty=Decimal("4"),
        filled_price=Decimal("120"),
        filled_at=CREATED,
    )
    sequences = fill_sequences([sell])
    links = fill_lot_links([sell], sequences)
    assert links[70]["lot_link_kind"] == "unlinked"
    assert links[70]["lot_basis_kind"] == "unknown"


CRYPTO_SELL_RUNG = rung_row(
    id=80,
    proposal_id="22222222-3333-4444-8555-666666666666",
    account_mode="upbit",
    market="crypto",
    symbol="KRW-BTC",
    side="sell",
    quantity=Decimal("0.01"),
    limit_price=Decimal("150000000"),
    broker_order_id=UPBIT_ORDER_ID,
    created_at=CREATED,
)

CANDLES = [
    {
        "venue": "upbit",
        "venue_symbol": "KRW-BTC",
        "time": datetime(2026, 6, 8, tzinfo=UTC),
        "close": Decimal("145000000"),
    }
]


def test_real_crypto_sell_rung_carries_the_reference_close_at_creation() -> None:
    [rung] = projected_real(
        order_proposal_rungs=[CRYPTO_SELL_RUNG],
        crypto_daily_candles=CANDLES,
    ).tables["order_proposal_rungs"]
    assert rung["reference_price"] == "145000000.000000000000"
    assert rung["reference_price_asof_utc"] == "2026-06-09T00:00:00Z"
    assert rung["reference_price_source"] == "crypto_candles_1d_close"
    assert rung["distance_above_reference_pct"] == "3.4483"
    assert rung["created_at_utc"] == "2026-06-10T01:00:00Z"


def test_a_candle_that_had_not_closed_is_not_a_reference() -> None:
    [rung] = projected_real(
        order_proposal_rungs=[CRYPTO_SELL_RUNG],
        crypto_daily_candles=[
            {
                "venue": "upbit",
                "venue_symbol": "KRW-BTC",
                "time": datetime(2026, 6, 9, 12, tzinfo=UTC),
                "close": Decimal("145000000"),
            }
        ],
    ).tables["order_proposal_rungs"]
    assert rung["reference_price"] is None
    assert rung["reference_price_source"] == "unavailable:no_candle"


def test_a_symbol_without_a_candle_source_says_so() -> None:
    [rung] = projected_real(order_proposal_rungs=[CRYPTO_SELL_RUNG]).tables[
        "order_proposal_rungs"
    ]
    assert rung["reference_price"] is None
    assert rung["reference_price_source"] == "unavailable:no_instrument"


def test_a_kr_rung_gets_no_reference_price_columns() -> None:
    [rung] = projected_real().tables["order_proposal_rungs"]
    assert rung["reference_price"] is None
    assert rung["reference_price_source"] is None


def test_the_toss_us_row_keeps_its_real_symbol_and_plain_strategy() -> None:
    [row] = projected_real(
        toss_live_order_ledger=[toss_row(strategy="us-open near market")]
    ).tables["toss_live_order_ledger"]
    assert row["symbol"] == "BRK.B"
    assert row["strategy_tag"] == "us-open near market"
    assert row["order_ref"] == "TOSSORDER7788"


def test_the_cli_accepts_real_values_without_a_salt(tmp_path: Path) -> None:
    args = cli._parser().parse_args(
        [
            "--database-url-env",
            "D",
            "--real-values",
            "--out",
            str(tmp_path / "export"),
        ]
    )
    assert args.real_values is True
    assert cli.load_salt(args, repo_root=tmp_path) is None


def test_the_cli_still_requires_a_salt_without_real_values(tmp_path: Path) -> None:
    args = cli._parser().parse_args(
        ["--database-url-env", "D", "--out", str(tmp_path / "export")]
    )
    with pytest.raises(ValueError, match="salt"):
        asyncio.run(cli.run(args, session_factory=object, repo_root=tmp_path))


def test_the_deidentified_default_is_unchanged(tmp_path: Path) -> None:
    projection = projected_real()
    assert projection.mode is Mode.REAL
    # The default projector still hashes a label it cannot judge.
    from app.services.dots_history_export.projection import project

    default = project(
        sources(kis_live_order_ledger=[kis_live_row(strategy="반도체눌림목")]),
        hasher=RefHasher(SALT),
        window=WINDOW,
    )
    assert default.tables["kis_live_order_ledger"][0]["strategy_tag"].startswith("oth_")
    assert KIS_ORDER_NO not in json.dumps(default.tables, default=str)
