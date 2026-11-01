"""No order endpoint is reachable from research/data_plane (ROB-1448).

The broker clients the collector uses are order-capable app objects, so the
guard is an explicit allowlist: research reaches them only through
``read_only_view`` with ``ALLOWED_METHODS``, which holds daily-chart and close
methods only. These tests pin the allowlist, the view, the only import site,
and the adapters' calls.
"""

from __future__ import annotations

import ast
import asyncio
import re
import subprocess
import sys
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

from research.data_plane import broker_adapters
from research.data_plane.broker_adapters import (
    ALLOWED_METHODS,
    KIS_US_EXCHANGE,
    BrokerMethodRefusedError,
    KisDailyClient,
    TossDailyClient,
    open_broker_clients,
    read_only_view,
)
from research.data_plane.sources import SourceError

REPO = Path(__file__).resolve().parents[3]
PACKAGE = REPO / "research" / "data_plane"
ADAPTERS = PACKAGE / "broker_adapters.py"

# Words that name a mutation, an account read or a credential surface.
FORBIDDEN_WORDS = re.compile(
    r"order|cancel|modify|buy|sell|place|dispatch|submit|account|balance|"
    r"cash|holding|position|token|secret|settle",
    re.IGNORECASE,
)
# Order-capable methods of the real clients (inventory, ROB-1448).
KIS_ORDER_METHODS = (
    "order_korea_stock",
    "sell_korea_stock",
    "cancel_korea_order",
    "modify_korea_order",
    "order_overseas_stock",
    "buy_overseas_stock",
    "sell_overseas_stock",
    "cancel_overseas_order",
    "modify_overseas_order",
)
TOSS_ORDER_METHODS = ("place_order", "modify_order", "cancel_order")


@pytest.mark.unit
def test_allowlist_holds_only_chart_and_close_methods():
    assert set(ALLOWED_METHODS) == {"kis", "toss"}
    for broker, names in ALLOWED_METHODS.items():
        for name in names:
            assert not FORBIDDEN_WORDS.search(name), (broker, name)
            assert not name.startswith("_"), (broker, name)
    assert ALLOWED_METHODS["kis"] == {
        "inquire_daily_itemchartprice_unclamped",
        "inquire_overseas_daily_price_unclamped",
        "close",
    }
    assert ALLOWED_METHODS["toss"] == {"candles", "aclose"}


@pytest.mark.unit
def test_allowlist_names_exist_on_the_real_clients_and_orders_are_outside_it():
    from app.services.brokers.kis.client import KISClient
    from app.services.brokers.toss.client import TossReadClient

    for cls, broker, order_methods in (
        (KISClient, "kis", KIS_ORDER_METHODS),
        (TossReadClient, "toss", TOSS_ORDER_METHODS),
    ):
        for name in ALLOWED_METHODS[broker]:
            assert callable(getattr(cls, name, None)), (broker, name)
        for name in order_methods:
            assert callable(getattr(cls, name, None)), (broker, name)  # still real
            assert name not in ALLOWED_METHODS[broker]


class _OrderCapable:
    """Stand-in client: every order method would be a send."""

    def __init__(self):
        self.sent = []

    def __getattr__(self, name):
        def call(*a, **k):
            self.sent.append(name)

        return call


@pytest.mark.unit
@pytest.mark.parametrize(
    ("broker", "name"),
    [("kis", m) for m in KIS_ORDER_METHODS]
    + [("toss", m) for m in TOSS_ORDER_METHODS]
    + [
        ("kis", "_request_with_rate_limit"),
        ("kis", "_market_data"),
        ("kis", "_domestic_orders"),
        ("toss", "_request"),
        ("toss", "account_seq"),
        ("kis", "__dict__"),
    ],
)
def test_view_refuses_every_method_outside_the_allowlist(broker, name):
    target = _OrderCapable()
    view = read_only_view(broker, target)
    with pytest.raises(BrokerMethodRefusedError):
        getattr(view, name)
    with pytest.raises(BrokerMethodRefusedError):
        view._target = target
    assert target.sent == []
    assert "_OrderCapable" not in repr(view)


@pytest.mark.unit
def test_app_broker_imports_are_lazy_and_wrapped_in_the_view():
    tree = ast.parse(ADAPTERS.read_text())
    # no app import at module level
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            mod = (
                node.module if isinstance(node, ast.ImportFrom) else node.names[0].name
            )
            assert not (mod or "").startswith("app"), mod
    # the app client constructors appear only as the first argument of the
    # adapter that wraps them in the view
    wrappers = {"KISClient": "KisDailyClient", "TossReadClient": "TossDailyClient"}
    parents = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node
    seen = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id in wrappers:
            if isinstance(parents[node], ast.ImportFrom | ast.alias):
                continue
            call = parents[node]
            while not (
                isinstance(call, ast.Call)
                and call.args
                and _contains(call.args[0], node)
            ):
                call = parents[call]
            assert isinstance(call.func, ast.Name) and call.func.id == wrappers[node.id]
            seen.add(node.id)
    assert seen == set(wrappers)
    # each adapter keeps only the view of its client
    for cls in (n for n in tree.body if isinstance(n, ast.ClassDef)):
        if cls.name not in wrappers.values():
            continue
        init = next(
            f
            for f in cls.body
            if isinstance(f, ast.FunctionDef) and f.name == "__init__"
        )
        uses = [
            n for n in ast.walk(init) if isinstance(n, ast.Name) and n.id == "client"
        ]
        assert len(uses) == 1
        assert isinstance(parents[uses[0]], ast.Call)
        assert parents[uses[0]].func.id == "read_only_view"


def _contains(tree, node):
    return any(n is node for n in ast.walk(tree))


@pytest.mark.unit
def test_adapters_call_only_allowlisted_methods_on_the_views():
    tree = ast.parse(ADAPTERS.read_text())
    views = {"_kis": "kis", "_toss": "toss"}
    found = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Attribute)
            and isinstance(node.value.value, ast.Name)
            and node.value.value.id == "self"
            and node.value.attr in views
        ):
            assert node.attr in ALLOWED_METHODS[views[node.value.attr]], node.attr
            found.add(node.attr)
    assert found  # the scan saw the calls


@pytest.mark.unit
def test_no_package_module_names_an_order_method():
    known = set(KIS_ORDER_METHODS) | set(TOSS_ORDER_METHODS)
    for path in sorted(PACKAGE.glob("*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            name = (
                node.attr
                if isinstance(node, ast.Attribute)
                else node.value
                if isinstance(node, ast.Constant) and isinstance(node.value, str)
                else None
            )
            assert name not in known, f"{path.name} names {name}"
        assert "getattr(target" not in path.read_text() or path == ADAPTERS


@pytest.mark.unit
def test_importing_the_adapters_loads_no_app_module():
    probe = (
        "import sys, research.data_plane.broker_adapters, research.data_plane.cli; "
        "print(','.join(m for m in sys.modules if (m == 'app' or m.startswith('app.'))"
        " and m not in ('app', 'app.core', 'app.core.symbol')))"
    )
    out = subprocess.run(  # noqa: S603
        [sys.executable, "-c", probe],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=True,
    )
    assert out.stdout.strip() == ""


# --- adapters against fake app clients ----------------------------------------


class _FakeKis:
    def __init__(self, frame):
        self.frame = frame
        self.calls = []

    async def inquire_daily_itemchartprice_unclamped(self, **kw):
        self.calls.append(("kr", kw))
        return self.frame

    async def inquire_overseas_daily_price_unclamped(self, **kw):
        self.calls.append(("us", kw))
        return self.frame

    async def close(self):
        self.calls.append(("close", {}))

    async def order_korea_stock(self, **kw):  # pragma: no cover - must never run
        raise AssertionError("order sent")


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _frame(days):
    return pd.DataFrame(
        {
            "date": pd.to_datetime([d.isoformat() for d in days]),
            "open": [70000.0] * len(days),
            "high": [71000.0] * len(days),
            "low": [69000.5] * len(days),
            "close": [70500.0] * len(days),
            "volume": [123] * len(days),
        }
    )


@pytest.mark.unit
def test_kis_kr_adapter_passes_the_adjustment_flag_and_end_date():
    kis = _FakeKis(_frame([date(2026, 10, 29), date(2026, 10, 30)]))
    client = KisDailyClient(kis, _run)
    for adjusted in (False, True):
        bars = client.daily_bars(
            market="kr",
            instrument="005930",
            venue="KOSPI",
            end=date(2026, 10, 30),
            count=2,
            adjusted=adjusted,
        )
        assert [b.day for b in bars] == [date(2026, 10, 29), date(2026, 10, 30)]
        assert bars[-1].close == "70500.0" and bars[-1].volume == 123
    assert [c[1]["adj"] for c in kis.calls] == [False, True]
    assert {c[1]["end_date"] for c in kis.calls} == {date(2026, 10, 30)}
    assert {c[1]["market"] for c in kis.calls} == {"J"}
    # one budget slot = one chart request: a single page wide enough for count
    assert {c[1]["max_pages"] for c in kis.calls} == {1}
    assert {c[1]["per_call_days"] for c in kis.calls} == {
        broker_adapters.kr_calendar_days(2)
    }
    assert broker_adapters.kr_calendar_days(100) * 5 / 7 >= 100
    client.close()
    assert kis.calls[-1][0] == "close"


@pytest.mark.unit
def test_kis_us_adapter_maps_venues_and_trims_to_the_end_day():
    kis = _FakeKis(_frame([date(2026, 10, 29), date(2026, 10, 30), date(2026, 11, 2)]))
    client = KisDailyClient(kis, _run)
    bars = client.daily_bars(
        market="us",
        instrument="BRK.B",
        venue="NYSE",
        end=date(2026, 10, 30),
        count=1,
        adjusted=False,
    )
    assert [b.day for b in bars] == [date(2026, 10, 30)]
    call = kis.calls[-1][1]
    assert call["exchange_code"] == "NYSE" and call["adjusted"] is False
    assert call["symbol"] == "BRK.B"  # the app converts to the KIS form itself
    with pytest.raises(SourceError, match="no exchange code"):
        client.daily_bars(
            market="us",
            instrument="X",
            venue="UNKNOWN:Q",
            end=date(2026, 10, 30),
            count=1,
            adjusted=True,
        )
    assert set(KIS_US_EXCHANGE.values()) <= {"NASD", "NYSE", "AMEX"}


@pytest.mark.unit
def test_toss_adapter_reads_daily_candles_before_the_next_local_midnight():
    calls = []

    class _FakeToss:
        async def candles(self, symbol, **kw):
            calls.append((symbol, kw))
            from decimal import Decimal

            c = SimpleNamespace(
                timestamp="2026-10-30T00:00:00+09:00",
                open_price=Decimal("100"),
                high_price=Decimal("110"),
                low_price=Decimal("90"),
                close_price=Decimal("105.5"),
                volume=Decimal("1000"),
            )
            return SimpleNamespace(candles=[c], next_before=None)

        async def aclose(self):
            calls.append(("aclose", {}))

        async def place_order(self, **kw):  # pragma: no cover
            raise AssertionError("order sent")

    client = TossDailyClient(_FakeToss(), _run)
    bars = client.daily_bars(
        market="kr",
        instrument="005930",
        venue="KOSPI",
        end=date(2026, 10, 30),
        count=1,
        adjusted=True,
    )
    assert [(b.day, b.close, b.volume) for b in bars] == [
        (date(2026, 10, 30), "105.5", 1000)
    ]
    symbol, kw = calls[0]
    assert symbol == "005930"
    assert kw == {
        "interval": "1d",
        "count": 1,
        "before": "2026-10-31T00:00:00+09:00",
        "adjusted": True,
    }


@pytest.mark.unit
def test_open_broker_clients_reports_a_failing_constructor_by_class_only(monkeypatch):
    import app.services.brokers.kis.client as kis_client

    class Boom:
        def __init__(self):
            raise RuntimeError("APPKEY=leaked-value")

    monkeypatch.setattr(kis_client, "KISClient", Boom)
    clients = open_broker_clients(["kis"])
    try:
        assert clients.clients == {}
        assert clients.unavailable == {"kis": "client_unavailable:RuntimeError"}
    finally:
        clients.close()
    assert broker_adapters.ALLOWED_METHODS is ALLOWED_METHODS


@pytest.mark.unit
@pytest.mark.parametrize(("broker", "name"), [("kis", "close"), ("toss", "candles")])
def test_an_allowed_method_does_not_lead_back_to_the_client(broker, name):
    class Client:
        def close(self):
            return "closed"

        def candles(self, *a, **k):
            return "page"

        def place_order(self, **k):  # pragma: no cover
            raise AssertionError("order sent")

    target = Client()
    method = getattr(read_only_view(broker, target), name)
    assert getattr(method, "__self__", None) is None
    assert not any(
        getattr(method, attr, None) is target for attr in ("__self__", "__wrapped__")
    )
    assert method() in ("closed", "page")
