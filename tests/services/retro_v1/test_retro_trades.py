import json
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from app.models.review import TradeRetrospective
from app.models.trade_journal import TradeJournal
from app.models.trading import InstrumentType
from app.services.retro_v1.retro_trades import filter_prose, read_retro_trades
from tests.services.retro_v1.support import FakeSession, validate

pytestmark = pytest.mark.unit
NOW = datetime(2026, 1, 5, tzinfo=UTC)


def journal(id_, kind, **values):
    return TradeJournal(
        id=id_,
        symbol="SAMPLE",
        instrument_type=kind,
        side="buy",
        status="closed",
        account_type="live",
        created_at=NOW,
        exit_date=NOW,
        entry_price=Decimal("10"),
        exit_price=Decimal("12"),
        quantity=Decimal("2"),
        thesis="secret-thesis",
        **values,
    )


def review(id_, journal_id, **values):
    return TradeRetrospective(
        id=id_,
        journal_id=journal_id,
        symbol="SAMPLE",
        instrument_type=InstrumentType.equity_kr,
        account_mode="kis_live",
        outcome="filled",
        side="sell",
        fill_evidence_available=True,
        created_at=NOW,
        evidence_snapshot={"secret_json_key": "secret"},
        **values,
    )


def session(journals, reviews):
    return FakeSession(
        {
            "FROM review.trade_journals": journals,
            "FROM review.trade_retrospectives": reviews,
        }
    )


@pytest.mark.asyncio
async def test_rows_summary_redaction_and_pagination():
    journals = [
        journal(
            101,
            InstrumentType.equity_kr,
            pnl_pct=Decimal("2.5"),
            total_pnl_krw=Decimal("100"),
        ),
        journal(202, InstrumentType.equity_us, security_pnl_usd=Decimal("4.25")),
    ]
    reviews = [
        review(
            301,
            101,
            realized_pnl=Decimal("100"),
            realized_pnl_currency="KRW",
            lesson="12345678",
        ),
        review(302, 101, lesson="x" * 1500),
        review(303, 202, lesson="normal"),
    ]
    result = await read_retro_trades(
        session(journals, reviews), from_="2026-01-01", to="2026-01-31", limit=1
    )
    validate(result, "RetroTradesResponse")
    assert result["summary"] == {
        "trade_count": 2,
        "return_rate_count": 1,
        "realized_by_currency": {"KRW": "100", "USD": "4.25"},
        "pnl_coverage": {
            "recorded": 1,
            "not_applicable": 0,
            "unknown": 0,
            "missing": 2,
        },
    }
    assert result["data"][0]["reviews"][0]["lesson"] is None
    assert {"field": "data.0.reviews.0.lesson", "reason": "redacted"} in result[
        "unknowns"
    ]
    assert result["data"][0]["reviews"][1]["lesson"] == "x" * 1000
    assert result["data"][0]["entry_price"] == "10"

    def values(obj):
        if isinstance(obj, dict):
            for value in obj.values():
                yield from values(value)
        elif isinstance(obj, list):
            for value in obj:
                yield from values(value)
        else:
            yield obj

    assert not {101, 202, 301, 302, 303} & set(values(result))
    serialized = json.dumps(result)
    for forbidden in (
        "secret_json_key",
        "secret-thesis",
        "correlation_id",
        "account",
        '"id"',
    ):
        assert forbidden not in serialized
    second = await read_retro_trades(
        session(journals, reviews),
        from_="2026-01-01",
        to="2026-01-31",
        limit=1,
        cursor=result["next_cursor"],
    )
    validate(second, "RetroTradesResponse")
    assert second["data"][0]["reviews"][0]["lesson"] == "normal"
    assert second["summary"] == result["summary"]


@pytest.mark.asyncio
async def test_null_amount_is_not_zero():
    result = await read_retro_trades(
        session([journal(101, InstrumentType.equity_kr)], []),
        from_="2026-01-01",
        to="2026-01-31",
    )
    validate(result, "RetroTradesResponse")
    assert result["summary"]["realized_by_currency"] is None
    assert {
        "field": "summary.realized_by_currency",
        "reason": "missing_data",
    } in result["unknowns"]


def test_filter_checks_entire_text_before_truncation():
    assert filter_prose(" normal ") == ("normal", False)
    assert filter_prose("x" * 1500) == ("x" * 1000, False)
    assert filter_prose("x" * 1500 + "12345678") == (None, True)
    assert filter_prose("account-number: 12-34-56") == (None, True)


@pytest.mark.asyncio
async def test_query_filters_and_failed_reviews_are_unknown():
    from tests.services.retro_v1.support import operational_error

    source = session([journal(101, InstrumentType.equity_kr)], [])
    source.routes["FROM review.trade_retrospectives"] = operational_error()
    result = await read_retro_trades(
        source, from_="2026-01-01", to="2026-01-31", market="kr", strategy="trend"
    )
    validate(result, "RetroTradesResponse")
    sql = "\n".join(source.statements)
    assert "trade_journals.account_type" in sql and "trade_journals.status" in sql
    assert "trade_journals.exit_date" in sql and "trade_journals.instrument_type" in sql
    assert (
        "trade_journals.strategy" in sql and "trade_retrospectives.account_mode" in sql
    )
    assert result["summary"]["pnl_coverage"] is None
    assert {"field": "summary.pnl_coverage", "reason": "incomplete_evidence"} in result[
        "unknowns"
    ]


@pytest.mark.asyncio
async def test_unsupported_instruments_keep_rows_and_counts():
    rows = [
        journal(101, InstrumentType.equity_kr),
        journal(202, InstrumentType.forex),
        journal(303, InstrumentType.index),
    ]
    result = await read_retro_trades(
        session(rows, []), from_="2026-01-01", to="2026-01-31"
    )
    validate(result, "RetroTradesResponse")
    assert (
        len(result["data"]) == result["total"] == result["summary"]["trade_count"] == 3
    )
    for i in (1, 2):
        assert result["data"][i]["market"] is None
        assert result["data"][i]["currency"] is None
        assert {"field": f"data.{i}.market", "reason": "unsupported"} in result[
            "unknowns"
        ]
        assert {"field": f"data.{i}.currency", "reason": "unsupported"} in result[
            "unknowns"
        ]


def test_filter_tokens_without_false_substrings():
    assert filter_prose("an exceptional move at the border no one expected") == (
        "an exceptional move at the border no one expected",
        False,
    )
    for text in (
        "an exception to the rule",
        "raw_payload",
        "account-number",
        "order_no",
    ):
        assert filter_prose(text) == (None, True)
