from __future__ import annotations

import ast
import asyncio
import importlib.util
import io
import re
import zipfile
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from app.services.kis_futures_read import view
from scripts.kis_futures_read import number, preview

ROOT = Path(__file__).resolve().parents[3]
BACKEND = ROOT / "app/services/brokers/kis/futures_read.py"
spec = importlib.util.spec_from_file_location("isolated_futures_read", BACKEND)
assert spec and spec.loader
backend = importlib.util.module_from_spec(spec)
spec.loader.exec_module(backend)


def run(coro):
    return asyncio.run(coro)


class Parent:
    _is_mock_client = True
    _hdr_base = {}
    _settings = SimpleNamespace(
        kis_access_token="fixture",
        kis_base_url="https://openapivts.koreainvestment.com:29443",
    )

    def __init__(self, pages):
        self.pages = iter(pages)
        self.calls = []

    async def _ensure_token(self):
        self.calls.append("token")

    def _kis_url(self, path):
        return self._settings.kis_base_url + path

    async def _request_with_rate_limit_with_headers(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return next(self.pages)


def test_balance_pages_and_mock_tr(monkeypatch):
    monkeypatch.setenv("KIS_FUTURES_READ_ENABLED", "true")
    first = {
        "rt_cd": "0",
        "output1": [{"shtn_pdno": "101W09", "cblc_qty": "1"}],
        "output2": {"cash_mgna": "125.500"},
        "ctx_area_fk200": "a",
        "ctx_area_nk200": "b",
    }
    second = {
        "rt_cd": "0",
        "output1": [{"shtn_pdno": "101W12", "cblc_qty": "2"}],
        "output2": {},
    }
    parent = Parent([(first, {"tr_cont": "M"}), (second, {})])
    result = run(backend.FuturesRead(parent).balance("12345678"))
    assert len(result["positions"]) == 2
    assert result["summary"]["cash_mgna"] == "125.500"
    requests = [call for call in parent.calls if isinstance(call, tuple)]
    assert requests[0][2]["headers"]["tr_id"] == "VTFO6118R"
    assert requests[1][2]["headers"]["tr_cont"] == "N"
    assert requests[1][2]["params"]["ACNT_PRDT_CD"] == "03"


def test_bad_cursor_is_not_a_complete_balance(monkeypatch):
    monkeypatch.setenv("KIS_FUTURES_READ_ENABLED", "true")
    parent = Parent([({"rt_cd": "0", "output1": [], "output2": {}}, {"tr_cont": "M"})])
    with pytest.raises(RuntimeError, match="연속조회"):
        run(backend.FuturesRead(parent).balance("12345678"))


def test_deposit_and_orderable_shapes(monkeypatch):
    monkeypatch.setenv("KIS_FUTURES_READ_ENABLED", "true")
    parent = Parent(
        [
            ({"rt_cd": "0", "output": {"dnca_tota": "120.50"}}, {}),
            ({"rt_cd": "0", "output": {"ord_psbl_qty": "2"}}, {}),
        ]
    )
    read = backend.FuturesRead(parent)
    assert run(read.deposit("12345678"))["dnca_tota"] == "120.50"
    assert (
        run(read.orderable("12345678", "101W09", "02", "100.25", "01"))["ord_psbl_qty"]
        == "2"
    )
    requests = [call for call in parent.calls if isinstance(call, tuple)]
    assert requests[0][2]["headers"]["tr_id"] == "VTRP6550R"
    assert requests[1][2]["headers"]["tr_id"] == "VTTO5105R"
    assert requests[1][2]["params"]["UNIT_PRICE"] == "100.25"


@pytest.mark.parametrize("gate", [None, "True", "1", " true", "false"])
def test_gate_off_prevents_token_and_network(monkeypatch, gate):
    if gate is None:
        monkeypatch.delenv("KIS_FUTURES_READ_ENABLED", raising=False)
    else:
        monkeypatch.setenv("KIS_FUTURES_READ_ENABLED", gate)
    parent = Parent([])
    with pytest.raises(RuntimeError):
        run(backend.FuturesRead(parent).balance("12345678"))
    with pytest.raises(RuntimeError):
        view.open_futures_view()
    assert parent.calls == []


def test_wrong_mock_host_prevents_token(monkeypatch):
    monkeypatch.setenv("KIS_FUTURES_READ_ENABLED", "true")
    parent = Parent([])
    parent._settings = SimpleNamespace(
        kis_base_url="https://openapi.koreainvestment.com:9443"
    )
    with pytest.raises(RuntimeError):
        run(backend.FuturesRead(parent).deposit("12345678"))
    assert parent.calls == []


def test_master_sample_shape(monkeypatch):
    monkeypatch.setenv("KIS_FUTURES_READ_ENABLED", "true")
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr(
            "fo_idx_code_mts.mst",
            "F|101W09|KR4101W90004|코스피200선물| |0|202609|001|코스피200\n".encode(
                "cp949"
            ),
        )

    class Client:
        def __init__(self, **kwargs):
            assert kwargs["follow_redirects"] is False

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def get(self, url):
            assert url == backend._MASTER_URL
            return httpx.Response(
                200, content=stream.getvalue(), request=httpx.Request("GET", url)
            )

    monkeypatch.setattr(backend.httpx, "AsyncClient", Client)
    rows = run(backend.FuturesRead(Parent([])).master())
    assert rows[0]["short_code"] == "101W09"
    assert rows[0]["name"] == "코스피200선물"


def test_view_guards_and_lazy_import():
    v = view.read_only_view(object())
    for name in ("place_order", "_request_with_rate_limit", "_target", "__dict__"):
        with pytest.raises(AttributeError):
            getattr(v, name)
    with pytest.raises(AttributeError):
        v._target = object()
    paths = [
        ROOT / "app/services/kis_futures_read/view.py",
        BACKEND,
        ROOT / "scripts/kis_futures_read.py",
    ]
    forbidden = re.compile(r"\b[A-Z0-9]*110[13]U\b")
    for path in paths:
        assert not forbidden.search(path.read_text())
    for path in paths:
        tree = ast.parse(path.read_text())
        assert not any(
            (
                isinstance(n, ast.ImportFrom)
                and (n.module or "").startswith("app.services.brokers.kis")
            )
            or (
                isinstance(n, ast.Import)
                and any(
                    alias.name.startswith("app.services.brokers.kis")
                    for alias in n.names
                )
            )
            for n in tree.body
        )
    assert view.ALLOWED_METHODS == {
        "futures_balance",
        "futures_deposit",
        "futures_orderable",
        "futures_master",
        "close",
    }


def test_exact_decimal_and_missing():
    assert number({"v": "12345678901234567890.123400"}, "v") == {
        "value": "12345678901234567890.123400",
        "reason": None,
    }
    for value in (None, "", "NaN", "bad", 0):
        assert number({"v": value}, "v")["value"] is None
        assert number({"v": value}, "v")["reason"]


def test_preview_masks_account_and_reports_unavailable(monkeypatch):
    class Fake:
        async def futures_balance(self, account):
            return {
                "summary": {"cash_mgna": "10.20"},
                "positions": [{"shtn_pdno": "101W09", "cblc_qty": "1"}],
            }

        async def futures_deposit(self, account):
            raise RuntimeError(account)

        async def close(self):
            pass

    monkeypatch.setattr(
        "scripts.kis_futures_read.open_futures_view", lambda **kwargs: Fake()
    )
    result = run(preview("12345678", None, "02", "1", "01"))
    assert "12345678" not in str(result)
    assert result["잔고"]["cash_mgna"]["value"] == "10.20"
    assert result["예수금"] == {
        "value": None,
        "reason": "RuntimeError",
        "rt_cd": None,
        "msg_cd": None,
    }


def test_live_trs_and_request_fields(monkeypatch):
    monkeypatch.setenv("KIS_FUTURES_READ_ENABLED", "true")
    parent = Parent(
        [({"rt_cd": "0", "output": {}}, {}), ({"rt_cd": "0", "output": {}}, {})]
    )
    parent._is_mock_client = False
    reader = backend.FuturesRead(parent)
    run(reader.deposit("12345678"))
    run(reader.orderable("12345678", "101W09", "01", "250.125", "01"))
    calls = [call for call in parent.calls if isinstance(call, tuple)]
    assert [call[2]["headers"]["tr_id"] for call in calls] == ["CTRP6550R", "TTTO5105R"]
    assert calls[1][2]["params"]["PDNO"] == "101W09"
    assert calls[1][2]["params"]["SLL_BUY_DVSN_CD"] == "01"


def test_master_gate_off_before_network(monkeypatch):
    monkeypatch.delenv("KIS_FUTURES_READ_ENABLED", raising=False)

    def forbidden(*args, **kwargs):
        raise AssertionError("network client created")

    monkeypatch.setattr(backend.httpx, "AsyncClient", forbidden)
    with pytest.raises(RuntimeError):
        run(backend.FuturesRead(Parent([])).master())


def test_real_client_delegates_only_to_futures_reader():
    tree = ast.parse((ROOT / "app/services/brokers/kis/client.py").read_text())
    cls = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "KISClient"
    )
    names = {
        node.name: node
        for node in cls.body
        if isinstance(node, ast.AsyncFunctionDef) and node.name.startswith("futures_")
    }
    assert set(names) == view.ALLOWED_METHODS - {"close"}
    for method in names.values():
        calls = [n for n in ast.walk(method) if isinstance(n, ast.Call)]
        assert len(calls) == 2
        assert any(
            isinstance(n.func, ast.Name) and n.func.id == "FuturesRead" for n in calls
        )
        assert any(
            isinstance(n.func, ast.Attribute)
            and n.func.attr in {"balance", "deposit", "orderable", "master"}
            for n in calls
        )


def test_factory_wraps_client_before_exposing_it():
    tree = ast.parse((ROOT / "app/services/kis_futures_read/view.py").read_text())
    factory = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "open_futures_view"
    )
    constructors = [
        node
        for node in ast.walk(factory)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "KISClient"
    ]
    assert len(constructors) == 1
    returns = [node for node in ast.walk(factory) if isinstance(node, ast.Return)]
    assert len(returns) == 1
    wrapped = returns[0].value
    assert (
        isinstance(wrapped, ast.Call)
        and isinstance(wrapped.func, ast.Name)
        and wrapped.func.id == "read_only_view"
    )
    assert wrapped.args == constructors


def test_orderable_rejects_unreadable_price_before_network(monkeypatch):
    monkeypatch.setenv("KIS_FUTURES_READ_ENABLED", "true")
    for price in ("NaN", "0", "-1", "bad"):
        parent = Parent([])
        with pytest.raises(ValueError):
            run(
                backend.FuturesRead(parent).orderable(
                    "12345678", "101W09", "02", price, "01"
                )
            )
        assert parent.calls == []


def test_preview_deposit_rejection_exposes_only_protocol_codes(monkeypatch):
    from app.services.brokers.kis.futures_read import FuturesRead

    monkeypatch.setenv("KIS_FUTURES_READ_ENABLED", "true")
    parent = Parent([({"rt_cd": "1", "msg_cd": "APBK1234", "msg1": "12345678"}, {})])
    read = FuturesRead(parent)

    class Fake:
        futures_deposit = read.deposit

        async def futures_balance(self, account):
            return {"summary": {}, "positions": []}

        async def close(self):
            pass

    monkeypatch.setattr(
        "scripts.kis_futures_read.open_futures_view", lambda **kwargs: Fake()
    )
    result = run(preview("12345678", None, "02", "1", "01"))
    assert result["예수금"] == {
        "value": None,
        "reason": "FuturesReadResponseError",
        "rt_cd": "1",
        "msg_cd": "APBK1234",
    }
    assert "12345678" not in str(result)
