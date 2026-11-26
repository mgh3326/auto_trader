import json
import logging
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services.market_events.kis_ksd_dividends import (
    fetch_ksd_event_window,
    normalize_ksd_event_row,
)

pytestmark = pytest.mark.unit

FIXTURES = Path(__file__).parents[2] / "fixtures" / "kis_ksd_events_doc_examples.json"
DOCUMENTED_EXAMPLES = json.loads(FIXTURES.read_text())
EXPECTED_DATES = {
    "paidin_capin": date(2026, 11, 20),
    "bonus_issue": date(2026, 11, 20),
    "purreq": date(2026, 11, 20),
    "merger_split": date(2026, 11, 20),
    "rev_split": date(2026, 11, 20),
    "cap_dcrs": date(2026, 11, 20),
    "list_info": date(2026, 11, 20),
    "pub_offer": date(2026, 11, 20),
    "forfeit": date(2026, 11, 20),
    "mand_deposit": date(2026, 11, 20),
    "sharehld_meet": date(2026, 12, 1),
}
EXPECTED_DECIMALS = {
    "paidin_capin": {
        "tot_issue_stk_qty": Decimal("1000000"),
        "issue_stk_qty": Decimal("250000"),
        "fix_rate": Decimal("0.25"),
        "disc_rate": Decimal("0.10"),
        "fix_price": Decimal("72000"),
    },
    "bonus_issue": {
        "fix_rate": Decimal("0.5"),
        "odd_rec_price": Decimal("100"),
        "tot_issue_stk_qty": Decimal("1000000"),
        "issue_stk_qty": Decimal("500000"),
    },
    "purreq": {"buy_req_price": Decimal("73500")},
    "merger_split": {
        "merge_rate": Decimal("1.25"),
        "tot_issue_stk_qty": Decimal("1000000"),
        "issue_stk_qty": Decimal("250000"),
    },
    "rev_split": {
        "inter_bf_face_amt": Decimal("100"),
        "inter_af_face_amt": Decimal("500"),
    },
    "cap_dcrs": {"reduce_cap_rate": Decimal("0.2")},
    "list_info": {
        "issue_stk_qty": Decimal("250000"),
        "tot_issue_stk_qty": Decimal("1000000"),
        "issue_price": Decimal("72000"),
    },
    "pub_offer": {
        "fix_subscr_pri": Decimal("72000.00"),
        "face_value": Decimal("5000"),
        "pub_bf_cap": Decimal("1000000000"),
        "pub_af_cap": Decimal("1250000000"),
        "assign_stk_qty": Decimal("250000"),
    },
    "forfeit": {
        "subscr_price": Decimal("72000"),
        "subscr_stk_qty": Decimal("250000"),
    },
    "mand_deposit": {
        "stk_qty": Decimal("250000"),
        "tot_issue_qty_per_rate": Decimal("0.25"),
    },
    "sharehld_meet": {"vote_tot_qty": Decimal("1000000")},
}


@pytest.mark.parametrize("fixture", DOCUMENTED_EXAMPLES["events"])
def test_ksd_kind_normalizes_documented_row_exactly(fixture):
    received_at = datetime(2026, 11, 25, 3, 0, tzinfo=UTC)
    event, values = normalize_ksd_event_row(
        fixture["kind"], fixture["row"], received_at=received_at
    )

    assert fixture["sample"].endswith(
        f"ksdinfo_{fixture['kind']}/ksdinfo_{fixture['kind']}.py"
    )
    assert event["category"] == "corporate_event"
    assert event["symbol"] == "005930"
    assert event["event_date"] == EXPECTED_DATES[fixture["kind"]]
    assert event["source"] == "kis_ksd"
    assert event["market"] == "kr"
    assert event["first_received_at"] == received_at

    numeric_values = {
        value["metric_name"]: value["actual"] for value in values if "actual" in value
    }
    assert numeric_values == EXPECTED_DECIMALS[fixture["kind"]]
    assert all(isinstance(value, Decimal) for value in numeric_values.values())
    date_values = {
        value["metric_name"]: value["value_details"]["date_or_range"]
        for value in values
        if "value_details" in value
    }
    if fixture["kind"] == "purreq":
        assert date_values == {
            "opp_opi_rcpt_term": "2026-11-25",
            "buy_req_rcpt_term": "2026-11-30",
        }
    if fixture["kind"] == "pub_offer":
        assert date_values["subscr_dt"] == "2026/11/20 ~ 2026/11/21"


def test_ksd_decimal_rejects_float_input():
    with pytest.raises(ValueError, match="non-exact fix_price"):
        normalize_ksd_event_row(
            "paidin_capin",
            {"record_date": "20261120", "sht_cd": "005930", "fix_price": 1.5},
        )


def test_alphanumeric_short_code_and_distinct_listings_keep_distinct_ids():
    fixture = next(
        item for item in DOCUMENTED_EXAMPLES["events"] if item["kind"] == "list_info"
    )
    first_row = {**fixture["row"], "sht_cd": "00104K"}
    second_row = {
        **first_row,
        "stk_kind": "우선주",
        "issue_price": "73000",
        "issue_stk_qty": "300000",
    }

    first, _ = normalize_ksd_event_row("list_info", first_row)
    second, _ = normalize_ksd_event_row("list_info", second_row)

    assert first["symbol"] == "00104K"
    assert first["source_event_id"] != second["source_event_id"]


def test_ksd_identity_fallback_includes_other_stable_provider_fields():
    fixture = next(
        item for item in DOCUMENTED_EXAMPLES["events"] if item["kind"] == "list_info"
    )
    row = {
        key: value
        for key, value in fixture["row"].items()
        if key not in {"issue_type", "stk_kind", "issue_price", "issue_stk_qty"}
    }
    first, _ = normalize_ksd_event_row("list_info", row)
    second, _ = normalize_ksd_event_row(
        "list_info", {**row, "tot_issue_stk_qty": "2000000"}
    )

    assert first["source_event_id"] != second["source_event_id"]


def test_schedule_revision_updates_identity_without_splitting_event():
    fixture = next(
        item for item in DOCUMENTED_EXAMPLES["events"] if item["kind"] == "paidin_capin"
    )
    original, _ = normalize_ksd_event_row("paidin_capin", fixture["row"])
    revised, _ = normalize_ksd_event_row(
        "paidin_capin", {**fixture["row"], "list_date": "20261220"}
    )

    assert original["source_event_id"] == revised["source_event_id"]
    assert revised["raw_payload_json"]["list_date"] == "2026-12-20"


@pytest.mark.parametrize("bad_date", ["202611", "2026111"])
def test_ksd_event_rejects_truncated_compact_date(bad_date):
    with pytest.raises(ValueError, match="record_date"):
        normalize_ksd_event_row(
            "bonus_issue", {"record_date": bad_date, "sht_cd": "005930"}
        )


@pytest.mark.asyncio
async def test_100_row_non_dividend_response_splits_and_reports_truncated_day():
    calls: list[tuple[str, str]] = []

    async def fake_ksd_bonus_issue(start: str, end: str) -> list[dict[str, str]]:
        calls.append((start, end))
        if start == "20261101" and end == "20261102":
            return [{"record_date": "20261101"}] * 100
        if start == "20261101":
            return [{"record_date": start}]
        return [{"record_date": "20261102"}] * 100

    result = await fetch_ksd_event_window(
        fake_ksd_bonus_issue, date(2026, 11, 1), date(2026, 11, 2)
    )

    assert calls == [
        ("20261101", "20261102"),
        ("20261101", "20261101"),
        ("20261102", "20261102"),
    ]
    assert len(result.rows) == 101
    assert result.truncated_windows == ((date(2026, 11, 2), date(2026, 11, 2)),)


@pytest.mark.asyncio
async def test_non_dividend_ksd_client_rejects_mock_before_request():
    from app.services.brokers.kis.market_data import MarketDataClient

    client = MarketDataClient(SimpleNamespace(_is_mock_client=True))
    request = AsyncMock()
    client._request_with_token_retry = request

    with pytest.raises(RuntimeError, match="real accounts only"):
        await client.ksd_bonus_issue("20261101", "20261130")

    request.assert_not_awaited()


@pytest.mark.asyncio
async def test_dry_run_normalizes_without_constructing_repository(monkeypatch):
    from argparse import Namespace

    from scripts import ingest_kis_ksd_dividends as cli

    fixture = next(
        item for item in DOCUMENTED_EXAMPLES["events"] if item["kind"] == "bonus_issue"
    )

    class FakeKISClient:
        closed = False

        async def ksd_bonus_issue(self, _start: str, _end: str):
            return [fixture["row"]]

        async def close(self):
            self.closed = True

    client = FakeKISClient()
    monkeypatch.setattr(cli, "KISClient", lambda **_kwargs: client)
    repository = AsyncMock(side_effect=AssertionError("dry-run must not write"))
    monkeypatch.setattr(cli, "MarketEventsRepository", repository)

    result = await cli.run(
        Namespace(
            from_date=date(2026, 11, 20),
            to_date=date(2026, 11, 20),
            kinds=["bonus_issue"],
            commit=False,
        )
    )

    assert result == 0
    assert client.closed
    repository.assert_not_called()


@pytest.mark.asyncio
async def test_invalid_ksd_row_does_not_discard_valid_rows_and_returns_failure(
    monkeypatch, caplog
):
    from argparse import Namespace

    from scripts import ingest_kis_ksd_dividends as cli

    fixture = next(
        item for item in DOCUMENTED_EXAMPLES["events"] if item["kind"] == "bonus_issue"
    )

    class FakeKISClient:
        closed = False

        async def ksd_bonus_issue(self, _start: str, _end: str):
            return [fixture["row"], {**fixture["row"], "record_date": "202611"}]

        async def close(self):
            self.closed = True

    client = FakeKISClient()
    monkeypatch.setattr(cli, "KISClient", lambda **_kwargs: client)

    class FakeRepository:
        events = []

        async def upsert_event_with_values(self, event, values, *, replace_values):
            self.events.append((event, values, replace_values))

    class FakeSession:
        committed = False

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return None

        async def commit(self):
            self.committed = True

    fake_repo = FakeRepository()
    fake_session = FakeSession()
    monkeypatch.setattr(cli, "MarketEventsRepository", lambda _db: fake_repo)
    from app.core import db

    monkeypatch.setattr(db, "AsyncSessionLocal", lambda: fake_session)
    caplog.set_level(logging.INFO)

    result = await cli.run(
        Namespace(
            from_date=date(2026, 11, 20),
            to_date=date(2026, 11, 20),
            kinds=["bonus_issue"],
            commit=True,
        )
    )

    assert result == 1
    assert client.closed
    assert fake_session.committed
    assert len(fake_repo.events) == 1
    assert fake_repo.events[0][2] is True
    assert "KSD 저장=1" in caplog.text
    assert "정규화 실패=1" in caplog.text
