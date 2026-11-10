from datetime import UTC, date, datetime
from decimal import Decimal

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

    async def execute(self, statement):
        self.calls += 1
        self.statement = statement

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
    assert rows == preview
    assert session.calls == session.commits == 1
    assert "ON CONFLICT" in str(session.statement)


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
