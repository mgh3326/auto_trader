"""hk 6508: write class — transient retry, immediate record, consecutive-failure breaker."""

from __future__ import annotations

import pytest
from sqlalchemy.exc import IntegrityError, OperationalError

import scripts.collect_fnguide_consensus as cli
from app.services.fnguide_consensus.collector import SinkAborted
from app.services.fnguide_consensus.writer import WriteOutcome
from tests.services.fnguide_consensus._helpers import parse_fixture

pytestmark = pytest.mark.unit


class _Begin:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


class FakeSession:
    def begin(self):
        return _Begin()


class FakeWriter:
    """Each script item is an exception to raise, or None for a successful write."""

    def __init__(self, script):
        self.script = list(script)
        self.calls = 0

    async def write_page(self, parsed):
        self.calls += 1
        item = self.script.pop(0)
        if item is not None:
            raise item
        return WriteOutcome(
            symbol=parsed.symbol,
            page=parsed.page,
            observed_date=parsed.observed_date.isoformat(),
            inserted=True,
            value_rows=parsed.value_count,
            report_rows=parsed.report_count,
        )


async def _no_sleep(_seconds: float) -> None:
    return None


def _transient() -> OperationalError:
    return OperationalError("INSERT", {}, Exception("connection lost"))


def _integrity() -> IntegrityError:
    return IntegrityError("INSERT", {}, Exception("check constraint violated"))


@pytest.mark.asyncio
async def test_transient_error_is_retried_then_written() -> None:
    written, errors = [], []
    writer = FakeWriter([_transient(), None])
    sink = cli.build_page_sink(FakeSession(), writer, written, errors, sleep=_no_sleep)

    await sink(parse_fixture("005930_consensus.html", symbol="005930"))

    assert errors == []
    assert len(written) == 1
    assert written[0]["attempts"] == 2
    assert writer.calls == 2


@pytest.mark.asyncio
async def test_integrity_error_is_recorded_at_once_and_not_retried() -> None:
    written, errors = [], []
    writer = FakeWriter([_integrity()])
    sink = cli.build_page_sink(FakeSession(), writer, written, errors, sleep=_no_sleep)

    await sink(parse_fixture("005930_consensus.html", symbol="005930"))

    assert written == []
    assert errors[0]["kind"] == "IntegrityError"
    assert errors[0]["attempts"] == 1
    assert writer.calls == 1


@pytest.mark.asyncio
async def test_transient_error_gives_up_after_the_retry_budget() -> None:
    written, errors = [], []
    writer = FakeWriter([_transient(), _transient(), _transient()])
    sink = cli.build_page_sink(
        FakeSession(), writer, written, errors, retries=2, sleep=_no_sleep
    )

    await sink(parse_fixture("005930_consensus.html", symbol="005930"))

    assert written == []
    assert errors[0]["attempts"] == 3
    assert writer.calls == 3


@pytest.mark.asyncio
async def test_consecutive_failures_abort_the_run() -> None:
    written, errors = [], []
    writer = FakeWriter([_integrity()] * 5)
    sink = cli.build_page_sink(
        FakeSession(), writer, written, errors, failure_limit=5, sleep=_no_sleep
    )
    parsed = parse_fixture("005930_consensus.html", symbol="005930")

    for _ in range(4):
        await sink(parsed)
    with pytest.raises(SinkAborted):
        await sink(parsed)
    assert len(errors) == 5


@pytest.mark.asyncio
async def test_a_success_resets_the_consecutive_failure_count() -> None:
    written, errors = [], []
    writer = FakeWriter([_integrity(), _integrity(), None, _integrity(), _integrity()])
    sink = cli.build_page_sink(
        FakeSession(), writer, written, errors, failure_limit=3, sleep=_no_sleep
    )
    parsed = parse_fixture("005930_consensus.html", symbol="005930")

    for _ in range(5):
        await sink(parsed)  # never three in a row, so no abort
    assert len(written) == 1
    assert len(errors) == 4


def test_exit_code_for_an_abort_is_one() -> None:
    from app.services.fnguide_consensus.collector import CollectResult

    collected = CollectResult()
    assert cli._exit_code(collected, [], aborted=True) == 1


class FakeUniverseSession:
    """Returns the symbols for the first statement and the excluded count for the second."""

    def __init__(self, symbols, excluded):
        self._results = [symbols, excluded]
        self.statements: list[str] = []

    async def execute(self, stmt):
        self.statements.append(
            str(stmt.compile(compile_kwargs={"literal_binds": True}))
        )
        value = self._results.pop(0)

        class _Result:
            def scalars(self_inner):
                class _S:
                    def all(_s):
                        return value

                return _S()

            def scalar_one(self_inner):
                return value

        return _Result()


@pytest.mark.asyncio
async def test_universe_keeps_six_digit_codes_and_counts_the_rest() -> None:
    from app.services.fnguide_consensus.universe import load_kr_stock_symbols

    session = FakeUniverseSession(["000660", "005930"], 3)
    loaded = await load_kr_stock_symbols(session)

    assert loaded.symbols == ["000660", "005930"]
    assert loaded.excluded_non_numeric == 3
    assert "^[0-9]{6}$" in session.statements[0]
    assert "NOT (kr_symbol_universe.symbol ~ '^[0-9]{6}$')" in session.statements[1]
