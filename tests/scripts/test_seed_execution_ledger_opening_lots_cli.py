# tests/scripts/test_seed_execution_ledger_opening_lots_cli.py
from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import scripts.seed_execution_ledger_opening_lots as cli
from app.services.execution_ledger.opening_lots import OpeningLotCandidate

CUTOVER = datetime(2099, 10, 10, tzinfo=UTC)
TEST_SYMBOLS = ["985001", "985002", "985003"]


def _candidate(**overrides) -> OpeningLotCandidate:  # noqa: ANN003
    data = {
        "broker": "kis",
        "account_mode": "live",
        "venue": "krx",
        "instrument_type": "equity_kr",
        "symbol": "005930",
        "raw_symbol": "005930",
        "currency": "KRW",
        "current_qty": Decimal("10"),
        "avg_price": Decimal("70000"),
    }
    data.update(overrides)
    return OpeningLotCandidate(
        **data,
    )


def _fake_session() -> AsyncMock:
    session = AsyncMock()
    session.rollback = AsyncMock()
    session.commit = AsyncMock()
    session.__aenter__.return_value = session
    session.__aexit__.return_value = None
    return session


def _patch_candidates(monkeypatch, candidates: list[OpeningLotCandidate]) -> None:
    monkeypatch.setattr(
        cli, "load_opening_lot_candidates", AsyncMock(return_value=candidates)
    )


def _kr_candidates() -> list[OpeningLotCandidate]:
    return [_candidate(symbol=symbol, raw_symbol=symbol) for symbol in TEST_SYMBOLS]


@pytest.mark.asyncio
async def test_seed_cli_dry_run_rolls_back(monkeypatch):
    session = AsyncMock()
    session.rollback = AsyncMock()
    session.commit = AsyncMock()
    session.__aenter__.return_value = session
    session.__aexit__.return_value = None
    monkeypatch.setattr(cli, "AsyncSessionLocal", lambda: session)
    monkeypatch.setattr(
        cli, "load_opening_lot_candidates", AsyncMock(return_value=[_candidate()])
    )
    monkeypatch.setattr(
        "app.services.execution_ledger.repository.ExecutionLedgerRepository.net_quantity_by_match_key_since",
        AsyncMock(return_value={}),
    )
    monkeypatch.setattr(
        "app.services.execution_ledger.repository.ExecutionLedgerRepository.classify_fill",
        AsyncMock(return_value="inserted"),
    )

    rc = await cli._run(
        brokers=["kis"],
        cutover=datetime(2026, 5, 10, tzinfo=UTC),
        dry_run=True,
    )

    assert rc == 0
    session.rollback.assert_awaited_once()
    session.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_seed_cli_dry_run_reports_classified_counts_and_samples(
    monkeypatch, capsys
):
    session = AsyncMock()
    session.rollback = AsyncMock()
    session.commit = AsyncMock()
    session.__aenter__.return_value = session
    session.__aexit__.return_value = None
    monkeypatch.setattr(cli, "AsyncSessionLocal", lambda: session)
    monkeypatch.setattr(
        cli,
        "load_opening_lot_candidates",
        AsyncMock(
            return_value=[
                _candidate(symbol="005930", raw_symbol="005930"),
                _candidate(symbol="000660", raw_symbol="000660"),
                _candidate(symbol="035420", raw_symbol="035420"),
            ]
        ),
    )
    monkeypatch.setattr(
        cli.ExecutionLedgerRepository,
        "net_quantity_by_match_key_since",
        AsyncMock(return_value={}),
    )
    monkeypatch.setattr(
        cli.ExecutionLedgerRepository,
        "classify_fill",
        AsyncMock(side_effect=["inserted", "updated", "unchanged"]),
    )

    rc = await cli._run(
        brokers=["kis"],
        cutover=datetime(2026, 5, 10, tzinfo=UTC),
        dry_run=True,
    )

    payload = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert payload["would_insert"] == 1
    assert payload["would_update"] == 1
    assert payload["unchanged"] == 1
    assert payload["sample_seed_rows"][0]["broker_order_id"].startswith(
        "SEED-20260510-kis-krx-005930"
    )


@pytest.mark.asyncio
async def test_seed_cli_commit_requires_gate(monkeypatch):
    monkeypatch.setattr(
        cli,
        "settings",
        SimpleNamespace(EXECUTION_LEDGER_COMMIT_ENABLED=False),
    )

    with pytest.raises(RuntimeError, match="EXECUTION_LEDGER_COMMIT_ENABLED"):
        await cli._run(
            brokers=["kis"],
            cutover=datetime(2026, 5, 10, tzinfo=UTC),
            dry_run=False,
        )


@pytest.mark.asyncio
async def test_seed_cli_dry_run_json_is_byte_identical_without_symbol(
    monkeypatch, capsys
):
    """Golden: no --symbol -> the dry-run JSON must be byte-identical to the
    pre-filter shape (same keys, same ordering, no symbol_filter field)."""
    session = _fake_session()
    monkeypatch.setattr(cli, "AsyncSessionLocal", lambda: session)
    _patch_candidates(
        monkeypatch,
        [
            _candidate(symbol="985001", raw_symbol="985001"),
            _candidate(symbol="985002", raw_symbol="985002"),
        ],
    )
    monkeypatch.setattr(
        cli.ExecutionLedgerRepository,
        "net_quantity_by_match_key_since",
        AsyncMock(
            return_value={
                ("kis", "live", "krx", "equity_kr", "985002", "KRW"): Decimal("10")
            }
        ),
    )
    monkeypatch.setattr(
        cli.ExecutionLedgerRepository,
        "classify_fill",
        AsyncMock(return_value="inserted"),
    )

    rc = await cli._run(brokers=["kis"], cutover=CUTOVER, dry_run=True)

    expected = {
        "dry_run": True,
        "would_seed": 1,
        "would_insert": 1,
        "would_update": 0,
        "unchanged": 0,
        "committed": 0,
        "committed_insert": 0,
        "committed_update": 0,
        "sample_seed_rows": [
            {
                "status": "inserted",
                "broker": "kis",
                "account_mode": "live",
                "venue": "krx",
                "instrument_type": "equity_kr",
                "symbol": "985001",
                "raw_symbol": "985001",
                "currency": "KRW",
                "side": "buy",
                "filled_qty": "10",
                "filled_price": "70000",
                "broker_order_id": "SEED-20991010-kis-krx-985001",
            }
        ],
        "skipped": [
            {
                "key": ["kis", "live", "krx", "equity_kr", "985002", "KRW"],
                "reason": "covered_by_ledger_net",
                "current_qty": "10",
                "ledger_net_qty": "10",
            }
        ],
    }
    assert (
        capsys.readouterr().out
        == json.dumps(expected, ensure_ascii=False, sort_keys=True, default=str) + "\n"
    )
    assert rc == 0


@pytest.mark.asyncio
async def test_seed_cli_symbol_filter_restricts_dry_run_plan(monkeypatch, capsys):
    session = _fake_session()
    monkeypatch.setattr(cli, "AsyncSessionLocal", lambda: session)
    _patch_candidates(monkeypatch, _kr_candidates())
    monkeypatch.setattr(
        cli.ExecutionLedgerRepository,
        "net_quantity_by_match_key_since",
        AsyncMock(return_value={}),
    )
    monkeypatch.setattr(
        cli.ExecutionLedgerRepository,
        "classify_fill",
        AsyncMock(return_value="inserted"),
    )

    rc = await cli._run(
        brokers=["kis"],
        cutover=CUTOVER,
        dry_run=True,
        symbols=["985001"],
    )

    payload = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert payload["would_seed"] == 1
    assert payload["would_insert"] == 1
    assert {row["symbol"] for row in payload["sample_seed_rows"]} == {"985001"}
    assert payload["skipped"] == []
    assert payload["symbol_filter"] == {
        "requested": ["985001"],
        "matched": ["985001"],
        "unmatched": [],
    }


@pytest.mark.asyncio
async def test_seed_cli_symbol_filter_accepts_repeats_and_comma_lists(
    monkeypatch, capsys
):
    session = _fake_session()
    monkeypatch.setattr(cli, "AsyncSessionLocal", lambda: session)
    _patch_candidates(monkeypatch, _kr_candidates())
    monkeypatch.setattr(
        cli.ExecutionLedgerRepository,
        "net_quantity_by_match_key_since",
        AsyncMock(return_value={}),
    )
    monkeypatch.setattr(
        cli.ExecutionLedgerRepository,
        "classify_fill",
        AsyncMock(return_value="inserted"),
    )

    rc = await cli._run(
        brokers=["kis"],
        cutover=CUTOVER,
        dry_run=True,
        symbols=["985001,985002", "985003", "985001"],
    )

    payload = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert payload["would_seed"] == 3
    assert payload["symbol_filter"]["requested"] == [
        "985001",
        "985002",
        "985003",
    ]


def test_seed_cli_parse_args_collects_symbols() -> None:
    args = cli.parse_args(
        [
            "--cutover",
            "2099-10-10",
            "--symbol",
            "005930,196170",
            "--symbol",
            "000660",
        ]
    )
    assert args.symbol == ["005930,196170", "000660"]


@pytest.mark.asyncio
async def test_seed_cli_symbol_filter_requires_exact_equality(monkeypatch, capsys):
    session = _fake_session()
    monkeypatch.setattr(cli, "AsyncSessionLocal", lambda: session)
    _patch_candidates(monkeypatch, [_candidate(symbol="985001", raw_symbol="985001")])
    monkeypatch.setattr(
        cli.ExecutionLedgerRepository,
        "net_quantity_by_match_key_since",
        AsyncMock(return_value={}),
    )
    monkeypatch.setattr(
        cli.ExecutionLedgerRepository,
        "classify_fill",
        AsyncMock(return_value="inserted"),
    )

    rc = await cli._run(
        brokers=["kis"],
        cutover=CUTOVER,
        dry_run=True,
        symbols=["98500", "9850", "85001"],
    )

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert rc == 1
    assert payload["would_seed"] == 0
    assert payload["committed"] == 0
    assert payload["symbol_filter"]["matched"] == []
    assert payload["symbol_filter"]["unmatched"] == [
        {"symbol": "98500", "reason": "no_matching_candidate"},
        {"symbol": "9850", "reason": "no_matching_candidate"},
        {"symbol": "85001", "reason": "no_matching_candidate"},
    ]
    assert "no --symbol value matched" in captured.err


@pytest.mark.asyncio
async def test_seed_cli_symbol_filter_unknown_symbol_is_reported_not_dropped(
    monkeypatch, capsys
):
    session = _fake_session()
    monkeypatch.setattr(cli, "AsyncSessionLocal", lambda: session)
    _patch_candidates(monkeypatch, _kr_candidates())
    monkeypatch.setattr(
        cli.ExecutionLedgerRepository,
        "net_quantity_by_match_key_since",
        AsyncMock(return_value={}),
    )
    monkeypatch.setattr(
        cli.ExecutionLedgerRepository,
        "classify_fill",
        AsyncMock(return_value="inserted"),
    )

    rc = await cli._run(
        brokers=["kis"],
        cutover=CUTOVER,
        dry_run=True,
        symbols=["985001", "999999"],
    )

    payload = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert payload["would_seed"] == 1
    assert payload["symbol_filter"]["unmatched"] == [
        {"symbol": "999999", "reason": "no_matching_candidate"}
    ]
    assert {
        "symbol": "999999",
        "reason": "no_matching_candidate",
    } in payload["skipped"]


@pytest.mark.asyncio
async def test_seed_cli_symbol_filter_normalizes_us_and_crypto_inputs(
    monkeypatch, capsys
):
    session = _fake_session()
    monkeypatch.setattr(cli, "AsyncSessionLocal", lambda: session)
    _patch_candidates(
        monkeypatch,
        [
            _candidate(
                symbol="BRK/B",
                raw_symbol="BRK/B",
                venue="NASD",
                instrument_type="equity_us",
                currency="USD",
            ),
            _candidate(
                broker="upbit",
                venue="upbit_krw",
                instrument_type="crypto",
                symbol="BTC",
                raw_symbol="KRW-BTC",
            ),
            _candidate(symbol="985001", raw_symbol="985001"),
        ],
    )
    monkeypatch.setattr(
        cli.ExecutionLedgerRepository,
        "net_quantity_by_match_key_since",
        AsyncMock(return_value={}),
    )
    monkeypatch.setattr(
        cli.ExecutionLedgerRepository,
        "classify_fill",
        AsyncMock(return_value="inserted"),
    )

    rc = await cli._run(
        brokers=["kis", "upbit"],
        cutover=CUTOVER,
        dry_run=True,
        symbols=["brk.b", "KRW-BTC"],
    )

    payload = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert payload["would_seed"] == 2
    assert payload["symbol_filter"]["matched"] == ["BRK.B", "KRW.BTC"]
    assert {row["symbol"] for row in payload["sample_seed_rows"]} == {
        "BRK/B",
        "BTC",
    }


async def _seeded_rows(db_session) -> list:  # noqa: ANN001, ANN202
    from sqlalchemy import select

    from app.models.execution_ledger import ExecutionLedger

    result = await db_session.execute(
        select(ExecutionLedger)
        .where(
            ExecutionLedger.broker_order_id.like("SEED-20991010-%"),
            ExecutionLedger.symbol.in_(TEST_SYMBOLS + ["999999"]),
        )
        .order_by(ExecutionLedger.symbol)
    )
    return list(result.scalars())


async def _cleanup_seed_rows(db_session) -> None:  # noqa: ANN001
    from sqlalchemy import delete

    from app.models.execution_ledger import ExecutionLedger

    await db_session.execute(
        delete(ExecutionLedger).where(
            ExecutionLedger.broker_order_id.like("SEED-20991010-%")
        )
    )
    await db_session.commit()


def _enable_commit_gate(monkeypatch) -> None:
    monkeypatch.setattr(
        cli,
        "settings",
        SimpleNamespace(EXECUTION_LEDGER_COMMIT_ENABLED=True),
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_seed_cli_commit_writes_only_filtered_symbols(
    db_session, monkeypatch, capsys
):
    """Commit path must honor the filter: only the requested symbol is
    persisted (assertion-RED vs a mutant that filters dry-run only)."""
    _enable_commit_gate(monkeypatch)
    _patch_candidates(monkeypatch, _kr_candidates())

    try:
        rc = await cli._run(
            brokers=["kis"],
            cutover=CUTOVER,
            dry_run=False,
            symbols=["985001"],
        )

        payload = json.loads(capsys.readouterr().out)
        rows = await _seeded_rows(db_session)
        assert rc == 0
        assert payload["committed"] == 1
        assert payload["committed_insert"] == 1
        assert [row.symbol for row in rows] == ["985001"]
        assert rows[0].broker_order_id == "SEED-20991010-kis-krx-985001"
        assert rows[0].source == "manual_import"
        assert rows[0].filled_qty == Decimal("10")
    finally:
        await _cleanup_seed_rows(db_session)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_seed_cli_commit_filter_persists_across_symbols(
    db_session, monkeypatch, capsys
):
    """A two-symbol filter commits exactly those two, none other."""
    _enable_commit_gate(monkeypatch)
    _patch_candidates(monkeypatch, _kr_candidates())

    try:
        rc = await cli._run(
            brokers=["kis"],
            cutover=CUTOVER,
            dry_run=False,
            symbols=["985002,985003"],
        )

        payload = json.loads(capsys.readouterr().out)
        rows = await _seeded_rows(db_session)
        assert rc == 0
        assert payload["committed"] == 2
        assert sorted(row.symbol for row in rows) == ["985002", "985003"]
    finally:
        await _cleanup_seed_rows(db_session)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_seed_cli_dry_run_with_filter_writes_nothing(
    db_session, monkeypatch, capsys
):
    _patch_candidates(monkeypatch, _kr_candidates())

    try:
        rc = await cli._run(
            brokers=["kis"],
            cutover=CUTOVER,
            dry_run=True,
            symbols=["985001"],
        )

        assert rc == 0
        capsys.readouterr()
        assert await _seeded_rows(db_session) == []
    finally:
        await _cleanup_seed_rows(db_session)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_seed_cli_commit_all_unmatched_writes_nothing_and_fails(
    db_session, monkeypatch, capsys
):
    """If no requested symbol matches, commit mode must write nothing and
    exit non-zero."""
    _enable_commit_gate(monkeypatch)
    _patch_candidates(monkeypatch, _kr_candidates())

    try:
        rc = await cli._run(
            brokers=["kis"],
            cutover=CUTOVER,
            dry_run=False,
            symbols=["999999"],
        )

        captured = capsys.readouterr()
        payload = json.loads(captured.out)
        assert rc == 1
        assert payload["committed"] == 0
        assert payload["symbol_filter"]["unmatched"] == [
            {"symbol": "999999", "reason": "no_matching_candidate"}
        ]
        assert "no --symbol value matched" in captured.err
        assert await _seeded_rows(db_session) == []
    finally:
        await _cleanup_seed_rows(db_session)


@pytest.mark.asyncio
async def test_seed_cli_commit_gate_unchanged_with_symbol(monkeypatch):
    """--commit still requires EXECUTION_LEDGER_COMMIT_ENABLED even when a
    symbol filter is present."""
    monkeypatch.setattr(
        cli,
        "settings",
        SimpleNamespace(EXECUTION_LEDGER_COMMIT_ENABLED=False),
    )
    _patch_candidates(monkeypatch, _kr_candidates())

    with pytest.raises(RuntimeError, match="EXECUTION_LEDGER_COMMIT_ENABLED"):
        await cli._run(
            brokers=["kis"],
            cutover=CUTOVER,
            dry_run=False,
            symbols=["985001"],
        )
