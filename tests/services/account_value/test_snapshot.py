from datetime import UTC, date, datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.services.account_value.snapshot import Holding, Observation, capture


class Reader:
    async def read(self):
        at = datetime(2026, 1, 1, tzinfo=UTC)
        return [
            Observation(
                {
                    "kr": "kis:kis_live:",
                    "us": "toss:toss_live:",
                    "crypto": "upbit:upbit:",
                }[market]
                + "a" * 64,
                market,
                date(2026, 1, 1),
                at,
                (
                    Holding(
                        "X",
                        Decimal(1),
                        currency,
                        None if market == "crypto" else Decimal(5),
                        None if market == "crypto" else "broker",
                        None if market == "crypto" else at,
                    ),
                ),
                {currency: Decimal(10)},
            )
            for market, currency in [("kr", "KRW"), ("us", "USD"), ("crypto", "KRW")]
        ]


class Session:
    def __init__(self):
        self.calls = 0
        self.commits = 0
        self.stored = {}

    async def execute(self, statement):
        self.calls += 1
        self.statement = statement
        if statement.is_insert:
            params = statement.compile().params
            inserted = []
            for i in range(len([key for key in params if key.startswith("account_m")])):
                row = {
                    name: params[f"{name}_m{i}"]
                    for name in (
                        "account",
                        "market",
                        "day",
                        "observed_at",
                        "holdings",
                        "cash",
                        "reasons",
                        "fx",
                    )
                }
                key = (row["account"], row["market"], row["day"])
                if key not in self.stored:
                    self.stored[key] = SimpleNamespace(**row)
                    inserted.append(key)
            return SimpleNamespace(all=lambda: inserted)
        return SimpleNamespace(
            scalars=lambda: SimpleNamespace(all=lambda: list(self.stored.values()))
        )

    async def commit(self):
        self.commits += 1


@pytest.mark.asyncio
async def test_preview_and_commit_keep_unknown_prices():
    session = Session()
    preview = await capture(Reader(), session)
    assert session.calls == session.commits == 0
    assert preview[2]["holdings"][0]["mark"] is None
    assert preview[2]["reasons"] == ["mark_unknown:X"]
    rows = await capture(Reader(), session, commit=True)
    assert all(row["write_status"] == "inserted" for row in rows)
    assert session.calls == 2 and session.commits == 1
    repeated = await capture(Reader(), session, commit=True)
    assert all(row["write_status"] == "already_present" for row in repeated)


@pytest.mark.parametrize(
    "path",
    ["app/services/account_value/snapshot.py", "scripts/account_value_snapshot.py"],
)
def test_snapshot_writer_and_cli_import_no_broker_or_order_modules(path):
    import ast
    from pathlib import Path

    source = Path(path).read_text()
    tree = ast.parse(source)
    imports = [
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    ]
    imports.extend(
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    )
    assert not [
        name
        for name in imports
        if name.startswith(
            (
                "app.services.brokers",
                "app.services.order_proposals",
                "app.mcp_server.tooling.orders",
            )
        )
    ]


@pytest.mark.asyncio
async def test_preview_labels_each_observation_without_writing():
    session = Session()
    rows = await capture(Reader(), session)
    assert [row.get("write_status") for row in rows] == [
        "preview",
        "preview",
        "preview",
    ]
    assert session.calls == 0


@pytest.mark.asyncio
async def test_repeat_reports_the_observation_actually_stored():
    from dataclasses import replace

    session = Session()
    await capture(Reader(), session, commit=True)

    class ChangedReader:
        async def read(self):
            return [
                replace(item, cash={currency: Decimal(99) for currency in item.cash})
                for item in await Reader().read()
            ]

    repeated = await capture(ChangedReader(), session, commit=True)
    assert all(row["write_status"] == "already_present" for row in repeated)
    assert all(list(row["cash"].values()) == ["10"] for row in repeated)


@pytest.mark.asyncio
async def test_partial_prior_capture_reports_inserted_and_existing():
    class FirstOnly:
        async def read(self):
            return (await Reader().read())[:1]

    session = Session()
    await capture(FirstOnly(), session, commit=True)
    rows = await capture(Reader(), session, commit=True)
    assert [row["write_status"] for row in rows] == [
        "already_present",
        "inserted",
        "inserted",
    ]
