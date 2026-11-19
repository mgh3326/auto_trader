"""3795 T4d: the venue best ask for mandate plan proposals (§4.1 cond. 6, V6).

Fixed fixtures only: a fake book reader and an injected clock. No broker, no
database, no network.
"""

from __future__ import annotations

import ast
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.services.mandate import proposal_gate
from app.services.order_proposals import mandate_quote
from app.services.order_proposals.mandate_quote import (
    JsonlSink,
    QuoteReadRecord,
    VenueBook,
    parse_kis_book,
    read_plan_quote,
)

pytestmark = pytest.mark.unit

T0 = datetime(2026, 11, 20, 1, 0, 0, tzinfo=UTC)
ROOT = Path(__file__).resolve().parents[3]


def _output1(**overrides):
    base = {
        "aspr_acpt_hour": "100000",
        "askp1": "70100",
        "askp_rsqn1": "10",
        "askp2": "70200",
        "askp_rsqn2": "20",
        "bidp1": "70000",
        "bidp_rsqn1": "30",
    }
    base.update(overrides)
    return base


def _context(account_mode="kiwoom_mock", market="equity_kr", premium="30"):
    return SimpleNamespace(
        sleeve=SimpleNamespace(account_mode=account_mode, market=market),
        plan=SimpleNamespace(symbol="005930"),
        terms=SimpleNamespace(entry_max_premium_bps=Decimal(premium)),
    )


class _Clock:
    def __init__(self, *ticks: datetime) -> None:
        self._ticks = list(ticks)

    def __call__(self) -> datetime:
        return self._ticks.pop(0) if len(self._ticks) > 1 else self._ticks[0]


# --- parse_kis_book: what counts as a best ask ------------------------------


def test_best_ask_is_the_lowest_positive_level_not_the_first_listed():
    book = parse_kis_book(_output1(askp1="70300", askp2="70100"), "70050")
    assert book.outcome == "ok"
    assert book.best_ask == Decimal("70100")
    assert book.best_bid == Decimal("70000")
    assert book.current_price == Decimal("70050")
    assert book.broker_book_time == "100000"


@pytest.mark.parametrize(
    ("overrides", "outcome"),
    [
        ({"askp1": "0", "askp2": "0"}, "no_ask"),
        ({"askp1": "", "askp2": None}, "no_ask"),
        ({"askp1": "-70100", "askp2": "0"}, "invalid_ask"),
        ({"askp1": "NaN", "askp2": "0"}, "invalid_ask"),
        ({"askp1": "Infinity", "askp2": "0"}, "invalid_ask"),
        ({"askp1": "abc", "askp2": "0"}, "invalid_ask"),
    ],
    ids=["zero", "blank", "negative", "nan", "inf", "garbage"],
)
def test_v6_an_unusable_ask_is_no_ask_and_current_price_never_fills_in(
    overrides, outcome
):
    book = parse_kis_book(_output1(**overrides), "70100")
    assert book.outcome == outcome
    assert book.best_ask is None
    # The current price is evidence for the price input only, never for the ask.
    assert book.current_price == Decimal("70100")


def test_an_ask_level_with_no_resting_quantity_is_not_an_ask():
    book = parse_kis_book(_output1(askp_rsqn1="0"), "70100")
    assert book.best_ask == Decimal("70200")


@pytest.mark.parametrize("bid", ["70100", "70500"], ids=["locked", "crossed"])
def test_a_locked_or_crossed_book_has_no_ask(bid):
    book = parse_kis_book(_output1(bidp1=bid), "70100")
    assert (book.outcome, book.best_ask) == ("crossed_book", None)


def test_no_current_price_is_its_own_outcome_and_still_no_quote():
    book = parse_kis_book(_output1(), None)
    assert (book.outcome, book.current_price) == ("no_price", None)


# --- read_plan_quote: one reader per venue, no substitute --------------------


async def _book_reader(book: VenueBook | Exception, calls: list | None = None):
    async def read(symbol: str, market_code: str) -> VenueBook:
        if calls is not None:
            calls.append((symbol, market_code))
        if isinstance(book, Exception):
            raise book
        return book

    return read


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("account_mode", "market"),
    [
        ("toss_live", "equity_kr"),
        ("kis_live", "equity_kr"),
        ("kiwoom_mock_us", "equity_us"),
        ("kiwoom_mock", "equity_us"),
        ("upbit", "crypto"),
    ],
)
async def test_v6_a_venue_without_a_reader_has_no_ask_and_reads_nothing(
    account_mode, market
):
    calls: list = []
    records: list[QuoteReadRecord] = []
    quote = await read_plan_quote(
        _context(account_mode, market),
        sink=records.append,
        book_reader=await _book_reader(parse_kis_book(_output1(), "70100"), calls),
        clock=_Clock(T0),
    )
    assert quote is None
    assert calls == []  # no KIS read stands in for another venue's book
    assert [r.outcome for r in records] == ["unsupported_venue"]
    assert (records[0].venue, records[0].source) == (None, None)


@pytest.mark.asyncio
async def test_a_kiwoom_mock_plan_reads_the_krx_book_and_returns_a_dated_quote():
    calls: list = []
    records: list[QuoteReadRecord] = []
    started, finished = T0, T0 + timedelta(milliseconds=420)
    quote = await read_plan_quote(
        _context(premium="30"),
        sink=records.append,
        book_reader=await _book_reader(
            parse_kis_book(_output1(askp1="19990", bidp1="19980"), "19980"), calls
        ),
        clock=_Clock(started, finished),
    )
    assert calls == [("005930", "J")]
    assert quote is not None
    assert quote.best_ask == Decimal("19990")
    assert quote.current_price == Decimal("19980")
    # observed at the START of the read (the earliest the book could have been)
    assert quote.ask_observed_at == started
    assert quote.read_at == finished
    # ceiling 19990 * 1.003 = 20049.97 -> the 50 won tick above 20,000
    assert quote.tick_size == Decimal(50)
    [record] = records
    assert (record.outcome, record.venue, record.source) == ("ok", "krx", "kis")
    assert record.read_ms == 420
    assert record.best_ask == "19990"
    assert record.best_bid == "19980"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "book",
    [
        parse_kis_book(_output1(askp1="0", askp2="0"), "70100"),
        parse_kis_book(_output1(askp1="-1", askp2="0"), "70100"),
        parse_kis_book(_output1(bidp1="70500"), "70100"),
        parse_kis_book(_output1(), None),
    ],
    ids=["no_ask", "invalid_ask", "crossed", "no_price"],
)
async def test_an_unusable_book_yields_no_quote_but_is_recorded(book):
    records: list[QuoteReadRecord] = []
    quote = await read_plan_quote(
        _context(),
        sink=records.append,
        book_reader=await _book_reader(book),
        clock=_Clock(T0),
    )
    assert quote is None
    assert [r.outcome for r in records] == [book.outcome]


@pytest.mark.asyncio
async def test_a_failed_read_has_no_ask_and_records_only_the_error_class():
    records: list[QuoteReadRecord] = []
    quote = await read_plan_quote(
        _context(),
        sink=records.append,
        book_reader=await _book_reader(RuntimeError("secret-token-123 in message")),
        clock=_Clock(T0),
    )
    assert quote is None
    [record] = records
    assert (record.outcome, record.error_class) == ("read_failed", "RuntimeError")
    assert "secret-token-123" not in record.to_json()


@pytest.mark.asyncio
async def test_a_failing_sink_never_changes_the_answer():
    def broken(_record: QuoteReadRecord) -> None:
        raise OSError("disk full")

    quote = await read_plan_quote(
        _context(),
        sink=broken,
        book_reader=await _book_reader(parse_kis_book(_output1(), "70100")),
        clock=_Clock(T0),
    )
    assert quote is not None and quote.best_ask == Decimal("70100")


@pytest.mark.asyncio
async def test_the_gate_wrapper_treats_a_missing_or_raising_reader_as_no_quote():
    assert await proposal_gate.load_proposal_quote(_context()) is None

    async def boom(_context):
        raise RuntimeError("down")

    assert await proposal_gate.load_proposal_quote(_context(), boom) is None


# --- the 5 second bound at the use site (§4.1 cond. 6, V6) ------------------


def _band(ask, observed_at, now, tick="100", limit="70100"):
    return proposal_gate.check_near_ask(
        limit_price=Decimal(limit),
        best_ask=ask,
        observed_at=observed_at,
        tick_size=Decimal(tick) if tick is not None else None,
        premium_bps=Decimal(30),
        now=now,
    )


@pytest.mark.parametrize(
    ("age_ms", "usable"),
    [(0, True), (4_999, True), (5_000, True), (5_001, False), (60_000, False)],
)
def test_v6_stale_ask_is_unavailable_at_the_five_second_bound(age_ms, usable):
    refusal = _band(Decimal(70100), T0, T0 + timedelta(milliseconds=age_ms))
    assert (refusal is None) is usable
    if not usable:
        assert refusal == ("mandate_ask_unavailable", {})


@pytest.mark.parametrize(
    "ask",
    [None, Decimal(0), Decimal(-1), Decimal("NaN"), Decimal("Infinity")],
    ids=["none", "zero", "negative", "nan", "inf"],
)
def test_v6_a_missing_or_invalid_ask_is_unavailable(ask):
    assert _band(ask, T0, T0) == ("mandate_ask_unavailable", {})


def test_v6_an_ask_observed_in_the_future_or_without_a_zone_is_unavailable():
    assert _band(Decimal(70100), T0 + timedelta(seconds=1), T0) == (
        "mandate_ask_unavailable",
        {},
    )
    assert _band(Decimal(70100), T0.replace(tzinfo=None), T0) == (
        "mandate_ask_unavailable",
        {},
    )
    assert _band(Decimal(70100), None, T0) == ("mandate_ask_unavailable", {})
    assert _band(Decimal(70100), T0, T0, tick=None) == ("mandate_ask_unavailable", {})


def test_the_gate_clock_is_never_earlier_than_the_read():
    # ``now`` sampled before a 700 ms read must not make the fresh ask a
    # future observation; a genuinely old ask is still stale.
    read_at = T0 + timedelta(milliseconds=700)
    assert proposal_gate.ask_check_time(T0, read_at) == read_at
    assert proposal_gate.ask_check_time(T0 + timedelta(seconds=9), read_at) == (
        T0 + timedelta(seconds=9)
    )
    assert proposal_gate.ask_check_time(T0, None) == T0
    assert proposal_gate.ask_check_time(T0, read_at.replace(tzinfo=None)) == T0


def test_the_band_is_the_ask_not_the_current_price():
    # limit above ask*(1+30bp) floored to the tick is refused even though it is
    # not above any other price that is lying around.
    refusal = _band(Decimal(70100), T0, T0, limit="70400")
    assert refusal is not None and refusal[0] == "mandate_limit_above_ask_band"
    assert _band(Decimal(70100), T0, T0, limit="70300") is None


# --- E1: the read record ------------------------------------------------------


def test_jsonl_sink_writes_one_closed_line_per_read_and_survives_a_bad_path(tmp_path):
    record = QuoteReadRecord(
        account_mode="kiwoom_mock",
        market="equity_kr",
        symbol="005930",
        venue="krx",
        source="kis",
        outcome="ok",
        observed_at=T0,
        read_completed_at=T0 + timedelta(milliseconds=250),
        read_ms=250,
        best_ask="70100",
        best_bid="70000",
        broker_book_time="100000",
    )
    path = tmp_path / "e1.jsonl"
    JsonlSink(path)(record)
    JsonlSink(path)(record)
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    assert len(rows) == 2
    assert set(rows[0]) == {
        "account_mode",
        "market",
        "symbol",
        "venue",
        "source",
        "outcome",
        "observed_at",
        "read_completed_at",
        "read_ms",
        "best_ask",
        "best_bid",
        "broker_book_time",
        "error_class",
    }
    assert rows[0]["outcome"] in mandate_quote.READ_OUTCOMES
    JsonlSink(tmp_path / "missing" / "e1.jsonl")(record)  # no raise


# --- the module only reads ----------------------------------------------------

FORBIDDEN_IMPORTS = (
    "app.services.order_proposals.dispatch",
    "app.services.order_proposals.revalidation",
    "app.services.order_proposals.broker_gateway",
    "app.services.order_proposals.auto_approve",
    "app.services.order_proposals.telegram_callback",
    "app.services.brokers.kis.domestic_orders",
    "app.services.brokers.kis.overseas_orders",
    "app.services.brokers.toss",
    "app.services.brokers.kiwoom",
    "app.services.execution_ledger",
    "app.services.execution_intent_gateway",
    "app.services.mechanical_loss_cut",
)
KIS_CALLS_ALLOWED = {"inquire_orderbook_snapshot", "inquire_price_raw_evidence"}


def test_the_quote_module_imports_no_order_or_ledger_path_and_calls_only_kis_reads():
    path = ROOT / "app/services/order_proposals/mandate_quote.py"
    tree = ast.parse(path.read_text())
    imported = [
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    ] + [
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    ]
    assert [n for n in imported if n.startswith(FORBIDDEN_IMPORTS)] == []
    kis_calls = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "kis"
    }
    assert kis_calls == KIS_CALLS_ALLOWED


# --- creation path: the real clock and a read that happens after ``now`` ------


def _creation_fixture(monkeypatch, *, now_arg: datetime | None):
    from unittest.mock import AsyncMock

    from app.services.mandate.fold import PlanState
    from app.services.order_proposals import service as service_module
    from app.services.order_proposals.service import OrderProposalsService

    session = AsyncMock()
    service = OrderProposalsService(session)
    service._repo = AsyncMock()
    monkeypatch.setattr(service, "_validate_sleeve_binding", AsyncMock())
    monkeypatch.setattr(service, "_validate_exit_binding", AsyncMock())
    monkeypatch.setattr(service_module, "record_proposal_decision", AsyncMock())
    monkeypatch.setattr(
        service_module.settings, "MANDATE_PLAN_PROPOSAL_ACCOUNT_MODES", "kiwoom_mock"
    )
    today = datetime.now(UTC).date()
    context = proposal_gate.PlanProposalContext(
        plan=SimpleNamespace(
            symbol="005930",
            account_ref="ref",
            scope_account_refs=["ref"],
            thesis="plan thesis",
            planned_amount=Decimal(1000),
            adopted_amount=Decimal(0),
        ),
        sleeve=SimpleNamespace(id=1, market="equity_kr", account_mode="kiwoom_mock"),
        state=PlanState(authority="approved", effective_session=today),
        terms=SimpleNamespace(account_ref="ref", entry_max_premium_bps=Decimal(0)),
        tranche=SimpleNamespace(tranche_index=1, amount=Decimal(1000)),
    )
    monkeypatch.setattr(
        proposal_gate, "lock_plan_proposal", AsyncMock(return_value=context)
    )
    monkeypatch.setattr(
        proposal_gate,
        "buy_freeze_state",
        AsyncMock(
            return_value=proposal_gate.FreezeVerdict(
                frozen=False,
                reason="fixture",
                evidence={},
                valid_until=datetime.now(UTC) + timedelta(minutes=5),
            )
        ),
    )
    monkeypatch.setattr(proposal_gate.store, "append_plan_event", AsyncMock())
    return service

    # (now_arg is applied by the caller)


async def _create(service, now):
    from app.services.order_proposals.service import RungInput

    return await service.create_proposal(
        symbol="005930",
        market="equity_kr",
        account_mode="kiwoom_mock",
        side="buy",
        order_type="limit",
        proposer="test",
        rungs=[RungInput(0, "buy", Decimal(1), Decimal(90), None)],
        mandate_plan_id=42,
        tranche_index=1,
        decision_price_text="100",
        now=now,
    )


@pytest.mark.asyncio
async def test_creation_with_the_real_clock_accepts_a_just_read_ask(monkeypatch):
    """The ask is read after ``now`` was taken; it must not look like a future
    observation (it did, before the freshness clock was tied to the read)."""
    service = _creation_fixture(monkeypatch, now_arg=None)

    async def book(_symbol: str, _code: str) -> VenueBook:
        return parse_kis_book(_output1(askp1="100", bidp1="99"), "100")

    monkeypatch.setattr(mandate_quote, "_read_kis_krx", book)
    result = await _create(service, None)
    assert result is service._repo.insert_group.return_value


@pytest.mark.asyncio
async def test_creation_refuses_when_the_ask_read_is_stale(monkeypatch):
    from app.services.order_proposals.errors import OrderProposalError

    service = _creation_fixture(monkeypatch, now_arg=T0)
    clock_ticks = iter([T0 - timedelta(seconds=10), T0 - timedelta(seconds=9)])

    async def book(_symbol: str, _code: str) -> VenueBook:
        return parse_kis_book(_output1(askp1="100", bidp1="99"), "100")

    async def stale_quote(context):
        return await read_plan_quote(
            context, book_reader=book, clock=lambda: next(clock_ticks)
        )

    monkeypatch.setattr(mandate_quote, "read_plan_quote", stale_quote)
    with pytest.raises(OrderProposalError, match="mandate_ask_unavailable"):
        await _create(service, T0)
    service._repo.insert_group.assert_not_called()


@pytest.mark.asyncio
async def test_creation_refuses_when_the_venue_has_no_ask_and_never_uses_price(
    monkeypatch,
):
    from app.services.order_proposals.errors import OrderProposalError

    service = _creation_fixture(monkeypatch, now_arg=None)

    async def book(_symbol: str, _code: str) -> VenueBook:
        return parse_kis_book(_output1(askp1="0", askp2="0"), "100")

    monkeypatch.setattr(mandate_quote, "_read_kis_krx", book)
    with pytest.raises(OrderProposalError, match="mandate_ask_unavailable"):
        await _create(service, None)
    service._repo.insert_group.assert_not_called()
