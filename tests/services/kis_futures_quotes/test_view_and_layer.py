"""hk 6148: quotes gate, request shape, paging, night, and the static boundary."""

from __future__ import annotations

import ast
import asyncio
import re
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.services.brokers.kis import futures_quotes as layer
from app.services.kis_futures_quotes import view as view_module

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[3]
NEW_MODULES = (
    ROOT / "app/services/brokers/kis/futures_quotes.py",
    ROOT / "app/services/kis_futures_quotes/view.py",
    ROOT / "app/services/kis_futures_quotes/contracts.py",
    ROOT / "app/services/research_candles/futures_minutes_collector.py",
    ROOT / "scripts/collect_futures_minutes.py",
)
LIVE = layer.LIVE_HOST


def run(coro):
    return asyncio.run(coro)


class Parent:
    _is_mock_client = False
    _hdr_base: dict[str, str] = {}
    _settings = SimpleNamespace(kis_access_token="fixture", kis_base_url=LIVE)

    def __init__(self, pages=()):
        self.pages = iter(pages)
        self.calls: list = []

    async def _ensure_token(self):
        self.calls.append("token")

    def _kis_url(self, path):
        return LIVE + path

    async def _request_with_rate_limit_with_headers(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return next(self.pages), {}

    async def close(self):
        self.calls.append("close")


class TrapParent(Parent):
    async def _ensure_token(self):
        raise AssertionError("token lookup before the gate")

    async def _request_with_rate_limit_with_headers(self, *a, **k):
        raise AssertionError("HTTP before the gate")


# ------------------------------------------------------------------ gate ---


@pytest.mark.parametrize("value", [None, "", "TRUE", "1", "yes", " true"])
def test_gate_off_raises_before_token_and_http(monkeypatch, value) -> None:
    if value is None:
        monkeypatch.delenv(layer.GATE_ENV, raising=False)
    else:
        monkeypatch.setenv(layer.GATE_ENV, value)
    # the older read gate is a different switch and does not open this one
    monkeypatch.setenv("KIS_FUTURES_READ_ENABLED", "true")
    quotes = layer.FuturesQuotes(TrapParent())
    with pytest.raises(layer.FuturesQuotesGateOff):
        run(quotes.price("A05612"))
    with pytest.raises(layer.FuturesQuotesGateOff):
        run(quotes.minute_bars("A05612", day=date(2026, 11, 24)))
    with pytest.raises(layer.FuturesQuotesGateOff):
        run(quotes.master())


def test_open_view_refuses_before_the_client_exists(monkeypatch) -> None:
    import app.services.brokers.kis.client as client_module

    def trap(*args, **kwargs):
        raise AssertionError("KISClient constructed with the gate off")

    monkeypatch.setattr(client_module, "KISClient", trap)
    monkeypatch.delenv(layer.GATE_ENV, raising=False)
    with pytest.raises(layer.FuturesQuotesGateOff):
        view_module.open_quotes_view()


def test_open_view_builds_a_live_client_when_the_gate_is_open(monkeypatch) -> None:
    import app.services.brokers.kis.client as client_module

    built = []

    def fake(**kwargs):
        built.append(kwargs)
        return Parent()

    monkeypatch.setattr(client_module, "KISClient", fake)
    monkeypatch.setenv(layer.GATE_ENV, "true")
    assert isinstance(view_module.open_quotes_view(), view_module.FuturesQuotesView)
    assert built == [{"is_mock": False}]


# ----------------------------------------------------------- request shape ---


def test_price_uses_the_documented_path_tr_and_market_code(monkeypatch) -> None:
    monkeypatch.setenv(layer.GATE_ENV, "true")
    page = {
        "rt_cd": "0",
        "output1": {"futs_prpr": "400.10", "hts_kor_isnm": "F 202612"},
        "output2": {"bstp_nmix_prpr": "399.0"},
        "output3": {},
    }
    parent = Parent([page])
    result = run(layer.FuturesQuotes(parent).price("A01612"))
    assert result["output1"]["futs_prpr"] == "400.10"
    (_, url, kwargs) = [c for c in parent.calls if isinstance(c, tuple)][0]
    assert url == LIVE + "/uapi/domestic-futureoption/v1/quotations/inquire-price"
    assert kwargs["headers"]["tr_id"] == "FHMIF10000000"
    assert kwargs["params"] == {
        "FID_COND_MRKT_DIV_CODE": "F",
        "FID_INPUT_ISCD": "A01612",
    }
    assert parent.calls[0] == "token"


def _row(hhmmss: str, price: str = "400.00") -> dict[str, str]:
    return {
        "stck_bsop_date": "20261124",
        "stck_cntg_hour": hhmmss,
        "futs_oprc": price,
        "futs_hgpr": price,
        "futs_lwpr": price,
        "futs_prpr": price,
        "cntg_vol": "3",
        "acml_tr_pbmn": "999",
    }


def test_minute_bars_pages_back_with_the_cursor_and_dedupes(monkeypatch) -> None:
    monkeypatch.setenv(layer.GATE_ENV, "true")
    page1 = {"rt_cd": "0", "output1": {}, "output2": [_row("090200"), _row("090100")]}
    # second page repeats the cursor row (inclusive cursor) and reaches the open
    page2 = {"rt_cd": "0", "output1": {}, "output2": [_row("090100"), _row("084500")]}
    parent = Parent([page1, page2])
    rows = run(
        layer.FuturesQuotes(parent).minute_bars(
            "A05612", day=date(2026, 11, 24), until_hour="091000"
        )
    )
    assert [r["stck_cntg_hour"] for r in rows] == ["084500", "090100", "090200"]
    requests = [c for c in parent.calls if isinstance(c, tuple)]
    assert len(requests) == 2
    assert requests[0][2]["headers"]["tr_id"] == "FHKIF03020200"
    first, second = (r[2]["params"] for r in requests)
    assert first["FID_INPUT_HOUR_1"] == "091000"
    assert second["FID_INPUT_HOUR_1"] == "090100"
    assert first["FID_HOUR_CLS_CODE"] == "60"
    assert first["FID_PW_DATA_INCU_YN"] == "N"
    assert first["FID_FAKE_TICK_INCU_YN"] == "N"
    assert first["FID_INPUT_DATE_1"] == "20261124"


def test_minute_bars_stops_when_a_page_adds_nothing(monkeypatch) -> None:
    monkeypatch.setenv(layer.GATE_ENV, "true")
    same = {"rt_cd": "0", "output1": {}, "output2": [_row("100000")]}
    parent = Parent([same, same])
    rows = run(
        layer.FuturesQuotes(parent).minute_bars("A05612", day=date(2026, 11, 24))
    )
    assert len(rows) == 1


def test_minute_bars_over_the_page_limit_is_an_error_not_a_partial_day(
    monkeypatch,
) -> None:
    monkeypatch.setenv(layer.GATE_ENV, "true")

    def pages():
        for i in range(layer.MAX_MINUTE_PAGES):
            yield {
                "rt_cd": "0",
                "output1": {},
                "output2": [_row(f"15{59 - i:02d}00")],
            }

    parent = Parent(pages())
    with pytest.raises(RuntimeError, match="한도"):
        run(layer.FuturesQuotes(parent).minute_bars("A05612", day=date(2026, 11, 24)))


def test_night_is_unsupported_before_any_token_or_http(monkeypatch) -> None:
    monkeypatch.setenv(layer.GATE_ENV, "true")
    with pytest.raises(layer.FuturesQuotesUnsupported):
        run(
            layer.FuturesQuotes(TrapParent()).minute_bars(
                "A05612", session="night", day=date(2026, 11, 24)
            )
        )


def test_mock_client_and_foreign_host_are_refused(monkeypatch) -> None:
    monkeypatch.setenv(layer.GATE_ENV, "true")

    class MockParent(Parent):
        _is_mock_client = True

    class OtherHost(Parent):
        _settings = SimpleNamespace(
            kis_access_token="x", kis_base_url="https://example.invalid"
        )

    for parent in (MockParent(), OtherHost()):
        with pytest.raises(RuntimeError, match="실전"):
            run(layer.FuturesQuotes(parent).price("A05612"))
        assert parent.calls == []


def test_error_response_is_refused(monkeypatch) -> None:
    monkeypatch.setenv(layer.GATE_ENV, "true")
    parent = Parent([{"rt_cd": "1", "msg1": "x"}])
    with pytest.raises(RuntimeError, match="거부"):
        run(layer.FuturesQuotes(parent).price("A05612"))


def test_view_forwards_only_to_the_allowlisted_client_methods() -> None:
    calls: list[str] = []

    class Target:
        async def quote_futures_master(self):
            calls.append("master")
            return []

        async def quote_futures_price(self, symbol):
            calls.append("price")
            return {}

        async def quote_futures_minutes(self, symbol, *, session, day, until_hour):
            calls.append(f"minutes:{session}")
            return []

        async def close(self):
            calls.append("close")

    v = view_module.FuturesQuotesView(Target())
    run(v.master())
    run(v.price("A05612"))
    run(v.minute_bars("A05612", day=date(2026, 11, 24)))
    run(v.close())
    assert calls == ["master", "price", "minutes:day", "close"]
    with pytest.raises(AttributeError):
        v.anything = 1  # type: ignore[attr-defined]
    with pytest.raises(AttributeError):
        v.futures_place_order  # noqa: B018


# --------------------------------------------------------- static boundary ---

FORBIDDEN_IMPORT_PARTS = (
    "futures_orders",
    "kis_futures_mock.place",
    "kis_futures_mock.ledger_service",
    "kis_futures_mock.attribution",
    "kis_futures_mock.rules",
    "execution_ledger",
    "order_proposals",
    "order_execution",
    "approval",
    "ledger",
    "orders",
)


def _imports(path: Path) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            names.add(node.module or "")
            names.update(f"{node.module}.{alias.name}" for alias in node.names)
    return names


@pytest.mark.parametrize("path", NEW_MODULES, ids=lambda p: p.name)
def test_new_modules_import_no_order_ledger_approval_or_mock_place_module(
    path: Path,
) -> None:
    offenders = [
        name
        for name in _imports(path)
        for part in FORBIDDEN_IMPORT_PARTS
        if part in name
    ]
    assert offenders == []


@pytest.mark.parametrize("path", NEW_MODULES, ids=lambda p: p.name)
def test_kis_futures_mock_is_reached_only_through_constants_and_calendar(
    path: Path,
) -> None:
    mock = {n for n in _imports(path) if "kis_futures_mock" in n}
    assert mock <= {
        "app.services.kis_futures_mock",
        "app.services.kis_futures_mock.constants",
        "app.services.kis_futures_mock.constants.MINI_KOSPI200",
        "app.services.kis_futures_mock.session_calendar",
    }


def test_quote_layer_reaches_only_quotes_paths_and_two_tr_ids() -> None:
    source = (ROOT / "app/services/brokers/kis/futures_quotes.py").read_text()
    assert "/trading/" not in source
    assert layer.QUOTES_PATH_PREFIX.endswith("/quotations/")
    literals = {
        node.value
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }
    tr_like = {v for v in literals if re.fullmatch(r"[A-Z]{4}[A-Z0-9]{6,}", v)}
    assert tr_like == {layer.TR_PRICE, layer.TR_MINUTE}
    assert not {"CANO", "ACNT_PRDT_CD"} & literals  # no account number


def test_view_calls_only_allowlisted_target_methods() -> None:
    tree = ast.parse((ROOT / "app/services/kis_futures_quotes/view.py").read_text())
    called = {
        node.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Attribute)
        and node.value.attr == "_target"
    }
    assert called == set(view_module.ALLOWED_TARGET_METHODS)
    client = (ROOT / "app/services/brokers/kis/client.py").read_text()
    body = client[client.index("async def quote_futures_master") :]
    body = body[: body.index("@staticmethod")]
    assert "FuturesRead" not in body and "futures_orders" not in body
    for name in (
        "quote_futures_master",
        "quote_futures_price",
        "quote_futures_minutes",
    ):
        assert f"async def {name}" in body


def test_the_older_read_view_cannot_reach_the_quote_methods() -> None:
    from app.services.kis_futures_read.view import ALLOWED_METHODS

    assert not {n for n in ALLOWED_METHODS if "quote" in n}
    assert not set(view_module.ALLOWED_TARGET_METHODS) - {"close"} & set(
        ALLOWED_METHODS
    )
