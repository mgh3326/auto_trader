"""hk 7119 — read-only broker holdings for the CLI (ESC 703687)."""

from __future__ import annotations

import ast
import subprocess
import sys
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.services.daily_loss_monitor.accounts import ACCOUNTS
from scripts import daily_loss_holdings as h
from scripts import execution_mandates

ROOT = Path(__file__).resolve().parents[3]


class _Kis:
    def __init__(self, kr=None, us=None, complete=True):
        self.kr, self.us, self.complete = kr, us, complete

    async def fetch_my_stocks_with_completion(
        self, is_mock=False, is_overseas=False, **kw
    ):
        return {"rows": self.us if is_overseas else self.kr, "complete": self.complete}

    async def order_korea_stock(self, *a, **k):  # must stay unreachable
        raise AssertionError("order reached")


def test_view_refuses_anything_but_the_allowed_methods() -> None:
    view = h.read_only_view("kis", _Kis())
    with pytest.raises(h.BrokerMethodRefusedError):
        _ = view.order_korea_stock
    with pytest.raises(h.BrokerMethodRefusedError):
        view.x = 1
    assert h.ALLOWED_METHODS["kis"] == {"fetch_my_stocks_with_completion"}
    assert h.ALLOWED_METHODS["toss"] == {"holdings", "aclose"}


@pytest.mark.asyncio
async def test_kis_rows_map_to_quantities_and_partial_rows_fail() -> None:
    kis = h.read_only_view(
        "kis",
        _Kis(
            kr=[{"pdno": "005930", "hldg_qty": "10"}],
            us=[{"ovrs_pdno": "BRK/B", "ovrs_cblc_qty": "2"}],
        ),
    )
    assert await h.kis_holdings(ACCOUNTS["kis_live_kr"], kis) == {"005930": Decimal(10)}
    assert await h.kis_holdings(ACCOUNTS["kis_live_us"], kis) == {"BRK.B": Decimal(2)}
    bad = h.read_only_view("kis", _Kis(kr=[{"pdno": "005930"}]))
    assert await h.kis_holdings(ACCOUNTS["kis_live_kr"], bad) is None
    partial = h.read_only_view(
        "kis", _Kis(kr=[{"pdno": "005930", "hldg_qty": "10"}], complete=False)
    )
    assert await h.kis_holdings(ACCOUNTS["kis_live_kr"], partial) is None


def test_upbit_accounts_are_parsed_strictly() -> None:
    ok = [
        {"currency": "KRW", "balance": "1", "locked": "0"},
        {"currency": "BTC", "balance": "0.5", "locked": "0.1"},
    ]
    assert h.upbit_account_map(ok) == {"KRW-BTC": Decimal("0.6")}
    for bad in (
        [{"currency": "BTC", "balance": "0"}],
        [{"balance": "1", "locked": "0"}],
        ["x"],
        {"a": 1},
    ):
        assert h.upbit_account_map(bad) is None


def test_upbit_partially_executed_open_order_is_unverified() -> None:
    assert h.upbit_open_orders_clear([{"executed_volume": "0"}])
    assert not h.upbit_open_orders_clear([{"executed_volume": "0.1"}])
    assert not h.upbit_open_orders_clear([{}])
    assert not h.upbit_open_orders_clear([{"executed_volume": "0"}] * 100)


@pytest.mark.asyncio
async def test_toss_items_are_split_by_country() -> None:
    items = [
        SimpleNamespace(symbol="005930", market_country="KR", quantity=Decimal(3)),
        SimpleNamespace(symbol="AAPL", market_country="US", quantity=Decimal(1)),
    ]

    class _Toss:
        async def holdings(self):
            return SimpleNamespace(items=items)

    toss = h.read_only_view("toss", _Toss())
    assert await h.toss_holdings(ACCOUNTS["toss_live_kr"], toss) == {
        "005930": Decimal(3)
    }
    assert await h.toss_holdings(ACCOUNTS["toss_live_us"], toss) == {"AAPL": Decimal(1)}


@pytest.mark.asyncio
async def test_a_failed_read_is_unverified(monkeypatch) -> None:
    import app.services.brokers.kis.client as kis_client

    def boom(**kw):
        raise RuntimeError("no creds")

    monkeypatch.setattr(kis_client, "KISClient", boom)
    assert await h.broker_holdings(ACCOUNTS["kis_live_kr"]) is None


def _loaded(module: str) -> set[str]:
    code = f"import sys\nimport {module}\nprint('\\n'.join(sorted(sys.modules)))\n"
    return set(
        subprocess.run(
            [sys.executable, "-c", code],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.split()
    )


def test_cli_import_closure_reaches_no_order_module() -> None:
    extra = _loaded("scripts.daily_loss_monitor") - _loaded("app.models")
    bad = sorted(
        m
        for m in extra
        if m.startswith("app.services.brokers")
        or any(w in m for w in ("order", "cancel", "modify"))
    )
    assert bad == []


def test_only_allowed_methods_are_called_on_the_views() -> None:
    tree = ast.parse((ROOT / "scripts/daily_loss_holdings.py").read_text())
    called = {
        (node.func.value.id, node.func.attr)
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id in ("kis", "toss")
    }
    for broker, method in called:
        assert method in h.ALLOWED_METHODS[broker], (broker, method)
    assert called


def test_operator_control_accepts_the_new_accounts() -> None:
    parser = execution_mandates._parser()
    for account in ACCOUNTS:
        args = parser.parse_args(
            [
                "--database-url-env",
                "DB",
                "control",
                "--account",
                account,
                "--mode",
                "ACTIVE",
                "--reason",
                "r",
                "--actor",
                "a",
            ]
        )
        assert args.account == account


def _quote(**kw):
    base = {
        "default_rate": 1400.0,
        "source": "toss",
        "valid_from": None,
        "valid_until": None,
    }
    base.update(kw)
    return SimpleNamespace(**base)


def test_expired_fx_quote_is_no_snapshot() -> None:
    from datetime import UTC, datetime, timedelta

    from scripts.daily_loss_monitor import fx_snapshot

    now = datetime(2026, 11, 28, 1, 0, tzinfo=UTC)
    assert fx_snapshot(_quote(valid_until=now - timedelta(seconds=1)), now) is None
    assert fx_snapshot(_quote(valid_from=now + timedelta(seconds=1)), now) is None
    snap = fx_snapshot(
        _quote(
            valid_from=now - timedelta(minutes=1),
            valid_until=now + timedelta(minutes=1),
        ),
        now,
    )
    assert snap is not None and snap.observed_at == now - timedelta(minutes=1)
    assert fx_snapshot(_quote(default_rate=0.0), now) is None
