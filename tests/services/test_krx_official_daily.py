"""Unit contracts for the KRX Open API official daily collector (hk 6586).

Provider inputs are spec-derived fixtures or inline payloads; no network is used.
"""

from __future__ import annotations

import json
from collections import Counter
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from sqlalchemy.dialects import postgresql

from app.services.krx_official_daily import client as client_module
from app.services.krx_official_daily import collector as collector_module
from app.services.krx_official_daily.client import (
    BASE_URL,
    ENDPOINTS,
    KrxOpenApiClient,
    assert_allowed_url,
)
from app.services.krx_official_daily.collector import (
    collect_official_daily,
    exit_code_for,
)
from app.services.krx_official_daily.errors import (
    KrxHostRefusedError,
    KrxMalformedError,
    KrxServiceNotApprovedError,
    KrxTransportError,
    KrxUnauthorizedKeyError,
)
from app.services.krx_official_daily.parse import parse_daily_payload, parse_decimal
from app.services.krx_official_daily.reader import read_zero_volume_fact
from app.services.krx_official_daily.store import insert_official_daily

pytestmark = pytest.mark.unit

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "krx_official_daily"
SESSION = date(2026, 11, 25)
SENTINEL = "SENTINEL-KEY-do-not-leak-0001"


def fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / name).read_text())


class RecordingHttp:
    """Stands in for the ``httpx`` module; records constructor kwargs and requests."""

    def __init__(self, handler: Any) -> None:
        self.handler = handler
        self.constructed: list[dict[str, Any]] = []
        self.requests: list[httpx.Request] = []

    def AsyncClient(self, **kwargs: Any) -> httpx.AsyncClient:  # noqa: N802
        self.constructed.append(kwargs)

        def recording(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            return self.handler(request)

        return httpx.AsyncClient(transport=httpx.MockTransport(recording), **kwargs)


def respond(
    status: int = 200,
    *,
    body: Any = None,
    text: str | None = None,
    content_type: str = "application/json",
) -> Any:
    def handler(_request: httpx.Request) -> httpx.Response:
        headers = {"content-type": content_type}
        if text is not None:
            return httpx.Response(status, content=text.encode(), headers=headers)
        return httpx.Response(
            status, json=body if body is not None else {}, headers=headers
        )

    return handler


# ---- parsing -------------------------------------------------------------


def test_captured_kospi_rows_keep_exact_decimals_and_reported_zeros() -> None:
    parsed = parse_daily_payload(
        fixture("stk_bydd_trd_captured_20261125.json"),
        market="kospi",
        session_date=SESSION,
    )
    traded, zero_day, _zero_day_2 = parsed.rows
    assert traded["symbol"] == "005930"
    assert str(traded["close_price"]) == "170700"
    assert str(traded["volume"]) == "22046619"
    assert str(traded["trade_value"]) == "3810252743750"
    assert traded["field_reasons"] == {}
    assert traded["full_day_zero_volume"] is False

    # Captured zero-volume day: the exchange reports OHLC as 0 with a real close.
    # The zero volume is evidence only; the 0 prices are stored as reported.
    assert zero_day["symbol"] == "000300"
    assert zero_day["full_day_zero_volume"] is True
    assert zero_day["volume"] == Decimal("0")
    assert zero_day["open_price"] == Decimal("0")
    assert str(zero_day["close_price"]) == "4200"
    assert zero_day["field_reasons"] == {}
    assert parsed.rejected == Counter()


def test_captured_kosdaq_rows_parse_under_their_market() -> None:
    parsed = parse_daily_payload(
        fixture("ksq_bydd_trd_captured_20261125.json"),
        market="kosdaq",
        session_date=SESSION,
    )
    first = parsed.rows[0]
    assert first["market"] == "kosdaq"
    assert str(first["close_price"]) == "1075"
    assert str(first["volume"]) == "65986"
    assert first["full_day_zero_volume"] is False
    assert parsed.rows[1]["full_day_zero_volume"] is True


def test_dash_and_blank_fields_become_null_with_reason() -> None:
    payload = {
        "OutBlock_1": [
            {
                "BAS_DD": "20261125",
                "ISU_CD": "005930",
                "TDD_CLSPRC": "-",
                "TDD_OPNPRC": "",
                "ACC_TRDVOL": "7",
                "MKTCAP": "1,000",
            }
        ]
    }
    (row,) = parse_daily_payload(payload, market="kospi", session_date=SESSION).rows
    assert row["close_price"] is None and row["field_reasons"]["close_price"] == "empty"
    assert row["open_price"] is None and row["field_reasons"]["open_price"] == "empty"
    assert row["full_day_zero_volume"] is False


@pytest.mark.parametrize(
    ("raw", "value", "reason"),
    [
        ("1,234.50", "1234.50", None),
        ("-", None, "empty"),
        ("   ", None, "empty"),
        ("", None, "empty"),
        ("abc", None, "unparsable"),
        ("NaN", None, "unparsable"),
        ("Infinity", None, "unparsable"),
        (None, None, "missing"),
        ("-5", None, "negative"),
        (1.5, None, "unparsable"),
        (True, None, "unparsable"),
        (12, "12", None),
    ],
)
def test_decimal_parser_is_exact_and_never_defaults_to_zero(
    raw: object, value: str | None, reason: str | None
) -> None:
    got, got_reason = parse_decimal(raw)
    assert got_reason == reason
    assert (None if got is None else str(got)) == value


def test_negative_volume_is_null_not_zero() -> None:
    payload = {
        "OutBlock_1": [{"BAS_DD": "20261125", "ISU_CD": "005930", "ACC_TRDVOL": "-3"}]
    }
    (row,) = parse_daily_payload(payload, market="kospi", session_date=SESSION).rows
    assert row["volume"] is None
    assert row["field_reasons"]["volume"] == "negative"
    assert row["full_day_zero_volume"] is None


def test_rows_for_another_day_bad_symbols_and_duplicates_are_counted_not_stored() -> (
    None
):
    def row(**overrides: str) -> dict[str, str]:
        base = {"BAS_DD": "20261125", "ISU_CD": "005930", "ACC_TRDVOL": "1"}
        return {**base, **overrides}

    payload = {
        "OutBlock_1": [
            row(),
            row(BAS_DD="20261124", ISU_CD="000660"),
            row(ISU_CD="000660", BAS_DD=""),
            row(ISU_CD="KR7000660001"),
            row(ISU_CD="00593"),
            row(ISU_CD="005930"),
        ]
    }
    parsed = parse_daily_payload(payload, market="kospi", session_date=SESSION)
    assert [r["symbol"] for r in parsed.rows] == ["005930"]
    assert parsed.rejected == Counter(
        {
            "bas_dd_mismatch": 1,
            "bas_dd_missing": 1,
            "symbol_not_short_code": 2,
            "duplicate_symbol": 1,
        }
    )


@pytest.mark.parametrize(
    "payload", [[], {}, {"OutBlock_1": None}, {"OutBlock_1": ["not-a-row"]}]
)
def test_outblock_missing_or_wrong_shape_is_malformed(payload: object) -> None:
    with pytest.raises(KrxMalformedError):
        parse_daily_payload(payload, market="kospi", session_date=SESSION)


def test_empty_outblock_is_no_rows_not_an_error() -> None:
    parsed = parse_daily_payload(
        {"OutBlock_1": []}, market="kospi", session_date=SESSION
    )
    assert parsed.rows == []
    assert parsed.rejected == Counter()


# ---- host allowlist, transport, typed errors -----------------------------


@pytest.mark.parametrize(
    "url",
    [
        "http://data-dbg.krx.co.kr/svc/apis/sto/stk_bydd_trd",
        "https://data.krx.co.kr/svc/apis/sto/stk_bydd_trd",
        "https://data-dbg.krx.co.kr.evil.example/svc/apis/sto/stk_bydd_trd",
        "https://evil.example/data-dbg.krx.co.kr",
        "https://data-dbg.krx.co.kr:8443/svc/apis/sto/stk_bydd_trd",
        "https://user:pw@data-dbg.krx.co.kr/svc/apis/sto/stk_bydd_trd",
    ],
)
def test_allowlist_refuses_other_hosts_schemes_ports_and_userinfo(url: str) -> None:
    with pytest.raises(KrxHostRefusedError):
        assert_allowed_url(url)


def test_allowlist_accepts_only_the_exact_endpoints() -> None:
    assert ENDPOINTS == {"kospi": "sto/stk_bydd_trd", "kosdaq": "sto/ksq_bydd_trd"}
    for endpoint in ENDPOINTS.values():
        assert_allowed_url(BASE_URL + endpoint)


@pytest.mark.asyncio
async def test_host_is_checked_before_any_http_client_exists(monkeypatch) -> None:
    http = RecordingHttp(respond(200, body={"OutBlock_1": []}))

    def refuse(_url: str) -> None:
        raise KrxHostRefusedError()

    monkeypatch.setattr(client_module, "assert_allowed_url", refuse)
    api = KrxOpenApiClient(auth_key=SENTINEL, http=http)
    with pytest.raises(KrxHostRefusedError):
        await api.fetch_daily("kospi", SESSION)
    assert http.constructed == []
    assert http.requests == []


@pytest.mark.asyncio
async def test_request_carries_key_only_in_header_and_never_follows_redirects() -> None:
    http = RecordingHttp(respond(200, body={"OutBlock_1": []}))
    api = KrxOpenApiClient(auth_key=f"  {SENTINEL}  ", http=http)
    payload = await api.fetch_daily("kosdaq", SESSION)
    assert payload == {"OutBlock_1": []}
    assert http.constructed[0]["follow_redirects"] is False
    (request,) = http.requests
    assert str(request.url) == f"{BASE_URL}sto/ksq_bydd_trd?basDd=20261125"
    assert request.headers["AUTH_KEY"] == SENTINEL
    assert SENTINEL not in str(request.url)


@pytest.mark.asyncio
async def test_redirect_is_refused_and_not_followed() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(
            302, headers={"location": "https://evil.example/steal"}, content=b""
        )

    api = KrxOpenApiClient(auth_key=SENTINEL, http=RecordingHttp(handler))
    with pytest.raises(KrxTransportError) as excinfo:
        await api.fetch_daily("kospi", SESSION)
    assert excinfo.value.code == "transport"
    assert str(excinfo.value) == "transport:redirect_refused"
    assert len(calls) == 1 and "evil.example" not in calls[0]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("body", "error", "exit_code"),
    [
        (
            {"respMsg": "Unauthorized Key", "respCode": "401"},
            KrxUnauthorizedKeyError,
            2,
        ),
        (
            {"respMsg": "Unauthorized API Call", "respCode": "401"},
            KrxServiceNotApprovedError,
            3,
        ),
    ],
)
async def test_the_two_401_bodies_map_to_different_typed_errors(
    body: dict[str, str], error: type, exit_code: int
) -> None:
    api = KrxOpenApiClient(
        auth_key=SENTINEL, http=RecordingHttp(respond(401, body=body))
    )
    with pytest.raises(error) as excinfo:
        await api.fetch_daily("kospi", SESSION)
    assert exit_code_for(excinfo.value) == exit_code


@pytest.mark.asyncio
async def test_unknown_401_body_is_not_guessed_into_a_key_error() -> None:
    api = KrxOpenApiClient(
        auth_key=SENTINEL, http=RecordingHttp(respond(401, body={"respMsg": "x"}))
    )
    with pytest.raises(KrxTransportError) as excinfo:
        await api.fetch_daily("kospi", SESSION)
    assert str(excinfo.value) == "transport:http_401_unclassified"
    assert exit_code_for(excinfo.value) == 1


@pytest.mark.asyncio
async def test_sentinel_key_never_appears_in_any_error_or_repr() -> None:
    echo = {"respMsg": f"Unauthorized Key echo {SENTINEL}", "respCode": "401"}
    cases: list[Any] = [
        respond(401, body=echo),
        respond(500, text=f"server says {SENTINEL}"),
        respond(200, text=f"<html>{SENTINEL}</html>", content_type="text/html"),
        lambda request: (_ for _ in ()).throw(httpx.ConnectError(f"boom {SENTINEL}")),
    ]
    for handler in cases:
        api = KrxOpenApiClient(auth_key=SENTINEL, http=RecordingHttp(handler))
        with pytest.raises(Exception) as excinfo:
            await api.fetch_daily("kosdaq", SESSION)
        assert SENTINEL not in str(excinfo.value)
        assert SENTINEL not in repr(excinfo.value)
        assert SENTINEL not in repr(api)


@pytest.mark.asyncio
async def test_non_json_content_type_and_bad_json_are_malformed() -> None:
    html = KrxOpenApiClient(
        auth_key=SENTINEL,
        http=RecordingHttp(respond(200, text="<html>", content_type="text/html")),
    )
    with pytest.raises(KrxMalformedError) as first:
        await html.fetch_daily("kospi", SESSION)
    assert str(first.value) == "malformed:content_type"

    broken = KrxOpenApiClient(
        auth_key=SENTINEL,
        http=RecordingHttp(respond(200, text="{not json")),
    )
    with pytest.raises(KrxMalformedError) as second:
        await broken.fetch_daily("kospi", SESSION)
    assert str(second.value) == "malformed:json"


def test_blank_key_is_refused_at_construction() -> None:
    with pytest.raises(ValueError):
        KrxOpenApiClient(auth_key="   ")


@pytest.mark.asyncio
async def test_unknown_market_is_refused_before_request() -> None:
    http = RecordingHttp(respond(200, body={"OutBlock_1": []}))
    api = KrxOpenApiClient(auth_key=SENTINEL, http=http)
    with pytest.raises(ValueError):
        await api.fetch_daily("nasdaq", SESSION)
    assert http.constructed == []


# ---- collector orchestration ---------------------------------------------


class FakeApi:
    def __init__(self, outcomes: dict[str, Any]) -> None:
        self.outcomes = outcomes
        self.calls: list[str] = []

    async def fetch_daily(self, market: str, session_date: date) -> Any:
        self.calls.append(market)
        outcome = self.outcomes[market]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class FakeSession:
    def __init__(self) -> None:
        self.executed: list[Any] = []
        self.commits = 0

    async def execute(self, statement: Any) -> Any:
        self.executed.append(statement)
        return SimpleNamespace(rowcount=0)

    async def commit(self) -> None:
        self.commits += 1

    async def flush(self) -> None:
        return None


@pytest.mark.asyncio
async def test_a_failed_market_writes_nothing_for_the_whole_run() -> None:
    session = FakeSession()
    api = FakeApi(
        {
            "kospi": fixture("stk_bydd_trd_captured_20261125.json"),
            "kosdaq": KrxServiceNotApprovedError(),
        }
    )
    result = await collect_official_daily(
        session_date=SESSION, client=api, session=session
    )
    assert result["status"] == "failed"
    assert result["error_code"] == "service_not_approved"
    assert result["exit_code"] == 3
    assert result["inserted"] is None
    assert session.executed == [] and session.commits == 0


@pytest.mark.asyncio
async def test_an_empty_list_is_not_published_yet_and_writes_nothing() -> None:
    session = FakeSession()
    api = FakeApi(
        {
            "kospi": fixture("stk_bydd_trd_captured_20261125.json"),
            "kosdaq": {"OutBlock_1": []},
        }
    )
    result = await collect_official_daily(
        session_date=SESSION, client=api, session=session
    )
    assert result["status"] == "not_published_yet"
    assert result["error_code"] == "not_published_yet"
    assert result["exit_code"] == 4
    assert result["markets"]["kosdaq"]["status"] == "not_published_yet"
    assert result["markets"]["kospi"]["status"] == "ok"
    assert result["inserted"] is None
    assert session.executed == [] and session.commits == 0


@pytest.mark.asyncio
async def test_an_all_published_run_is_ok_with_exit_zero() -> None:
    api = FakeApi(
        {
            "kospi": fixture("stk_bydd_trd_captured_20261125.json"),
            "kosdaq": fixture("ksq_bydd_trd_captured_20261125.json"),
        }
    )
    result = await collect_official_daily(session_date=SESSION, client=api)
    assert result["status"] == "ok"
    assert result["exit_code"] == 0
    assert result["markets"]["kospi"] == {"status": "ok", "rows": 3, "rejected": {}}


@pytest.mark.asyncio
async def test_commit_inserts_once_with_our_clock_and_commits(monkeypatch) -> None:
    captured: dict[str, Any] = {}

    async def fake_insert(session, *, rows, first_received_at):
        captured["rows"] = rows
        captured["first_received_at"] = first_received_at
        return len(rows)

    monkeypatch.setattr(collector_module, "insert_official_daily", fake_insert)
    session = FakeSession()
    received = datetime(2026, 11, 27, 8, 30, tzinfo=UTC)
    api = FakeApi(
        {
            "kospi": fixture("stk_bydd_trd_captured_20261125.json"),
            "kosdaq": fixture("ksq_bydd_trd_captured_20261125.json"),
        }
    )
    result = await collect_official_daily(
        session_date=SESSION, client=api, session=session, now=received
    )
    assert api.calls == ["kospi", "kosdaq"]
    assert captured["first_received_at"] == received
    assert len(captured["rows"]) == 6
    assert result["inserted"] == 6
    assert session.commits == 1
    assert result["first_received_at"] == received.isoformat()


@pytest.mark.asyncio
async def test_dry_run_never_touches_the_writer(monkeypatch) -> None:
    async def boom(*args, **kwargs):
        raise AssertionError("writer must not run in dry-run")

    monkeypatch.setattr(collector_module, "insert_official_daily", boom)
    api = FakeApi(
        {
            "kospi": fixture("stk_bydd_trd_captured_20261125.json"),
            "kosdaq": fixture("ksq_bydd_trd_captured_20261125.json"),
        }
    )
    result = await collect_official_daily(session_date=SESSION, client=api)
    assert result["inserted"] is None
    assert len(result["rows"]) == 6
    assert result["rows"][0]["close_price"] == "170700"


@pytest.mark.parametrize(
    ("error", "code"),
    [
        (KrxUnauthorizedKeyError(), 2),
        (KrxServiceNotApprovedError(), 3),
        (KrxTransportError("http_500"), 1),
        (KrxMalformedError("json"), 1),
    ],
)
def test_exit_codes_for_typed_errors(error: Exception, code: int) -> None:
    assert exit_code_for(error) == code


# ---- store and reader ----------------------------------------------------


@pytest.mark.asyncio
async def test_store_is_insert_only_on_the_natural_key() -> None:
    session = FakeSession()
    rows = [{"symbol": "005930", "session_date": SESSION, "market": "kospi"}]
    inserted = await insert_official_daily(
        session, rows=rows, first_received_at=datetime(2026, 11, 27, tzinfo=UTC)
    )
    assert inserted == 0
    (statement,) = session.executed
    sql = str(statement.compile(dialect=postgresql.dialect()))
    assert "ON CONFLICT (symbol, session_date) DO NOTHING" in sql
    assert "DO UPDATE" not in sql


@pytest.mark.asyncio
async def test_a_full_collection_is_split_into_statements_under_the_parameter_limit() -> (
    None
):
    """asyncpg rejects more than 32,767 bound parameters; one full run is ~44k unchunked."""
    template = parse_daily_payload(
        fixture("stk_bydd_trd_captured_20261125.json"),
        market="kospi",
        session_date=SESSION,
    ).rows[0]
    columns = len(template) + 1  # + first_received_at
    total_rows = 2772  # about 935 KOSPI + 1837 KOSDAQ rows on one session
    rows = [{**template, "symbol": f"{i:06d}"} for i in range(total_rows)]

    class CountingSession:
        def __init__(self) -> None:
            self.parameter_counts: list[int] = []

        async def execute(self, statement: Any) -> Any:
            compiled = statement.compile(dialect=postgresql.dialect())
            params = len(compiled.params)
            self.parameter_counts.append(params)
            return SimpleNamespace(rowcount=params // columns)

        async def flush(self) -> None:
            return None

    session = CountingSession()
    inserted = await insert_official_daily(
        session, rows=rows, first_received_at=datetime(2026, 11, 27, tzinfo=UTC)
    )
    assert inserted == total_rows
    assert len(session.parameter_counts) == 3
    assert max(session.parameter_counts) <= 32767


@pytest.mark.asyncio
async def test_store_with_no_rows_issues_no_statement() -> None:
    session = FakeSession()
    assert (
        await insert_official_daily(
            session, rows=[], first_received_at=datetime.now(UTC)
        )
        == 0
    )
    assert session.executed == []


class FakeReadSession:
    def __init__(self, row: Any) -> None:
        self.row = row
        self.keys: list[tuple[Any, ...]] = []

    async def get(self, _model: Any, key: tuple[Any, ...]) -> Any:
        self.keys.append(key)
        return self.row


def _stored(**overrides: Any) -> SimpleNamespace:
    base = {
        "volume": Decimal("0"),
        "full_day_zero_volume": True,
        "field_reasons": {},
        "source": "krx_openapi",
        "first_received_at": datetime(2026, 11, 27, 8, 30, tzinfo=UTC),
    }
    return SimpleNamespace(**{**base, **overrides})


@pytest.mark.asyncio
async def test_reader_states_and_dates_the_zero_volume_fact() -> None:
    zero = await read_zero_volume_fact(
        FakeReadSession(_stored()), symbol="000080", session_date=SESSION
    )
    assert zero["state"] == "full_day_zero_volume"
    assert zero["session_date"] == "2026-11-25"
    assert zero["is_halt_confirmation"] is False

    traded = await read_zero_volume_fact(
        FakeReadSession(_stored(volume=Decimal("5"), full_day_zero_volume=False)),
        symbol="005930",
        session_date=SESSION,
    )
    assert traded["state"] == "traded"


@pytest.mark.asyncio
async def test_reader_is_unknown_without_a_row_or_without_volume() -> None:
    absent = await read_zero_volume_fact(
        FakeReadSession(None), symbol="005930", session_date=SESSION
    )
    assert absent["state"] == "unknown"
    assert absent["reason"] == "no_official_row"

    no_volume = await read_zero_volume_fact(
        FakeReadSession(
            _stored(
                volume=None,
                full_day_zero_volume=None,
                field_reasons={"volume": "empty"},
            )
        ),
        symbol="005930",
        session_date=SESSION,
    )
    assert no_volume["state"] == "unknown"
    assert no_volume["reason"] == "empty"


@pytest.mark.asyncio
async def test_reader_refuses_a_non_short_code_symbol() -> None:
    with pytest.raises(ValueError):
        await read_zero_volume_fact(
            FakeReadSession(None), symbol="KR7005930003", session_date=SESSION
        )
