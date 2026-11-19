"""Exercise the real paper HTTP service through an in-memory transport."""

import ast
import asyncio
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from app.core.config import settings
from app.services.brokers.alpaca.config import AlpacaPaperSettings
from app.services.brokers.alpaca.exceptions import (
    AlpacaPaperIdentityMismatch,
    AlpacaPaperOrderNotFound,
    AlpacaPaperRequestError,
)
from app.services.brokers.alpaca.schemas import OrderRequest
from app.services.brokers.alpaca.service import AlpacaPaperBrokerService
from app.services.mock_strategy import recorder as recorder_module
from app.services.mock_strategy import us, us_broker
from app.services.mock_strategy.us_broker import UsPaperBroker

pytestmark = pytest.mark.asyncio
DAY = date(2026, 11, 2)
NOW = datetime(2026, 11, 2, 15, 0, tzinfo=UTC)


class FixedDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        return NOW


class Transport:
    def __init__(self):
        self.calls = []
        self.shortable = True
        self.borrowable = True
        self.open = True
        self.position = None
        self.clock = NOW
        self.active = True

    async def request(self, method, path, **kwargs):
        self.calls.append((method, path, kwargs))
        if path == "/v2/account":
            data = {
                "id": "fixture-paper-id",
                "account_number": "fixture-paper-number",
                "status": "ACTIVE" if self.active else "ACCOUNT_CLOSED",
                "trading_blocked": False,
                "account_blocked": False,
                "shorting_enabled": True,
                "cash": "1000000",
                "buying_power": "1000000",
                "portfolio_value": "1000000",
            }
        elif path == "/v2/clock":
            data = {"timestamp": self.clock.isoformat(), "is_open": self.open}
        elif path.startswith("/v2/assets/"):
            data = {
                "id": "fixture-asset",
                "symbol": "AAPL",
                "class": "us_equity",
                "status": "active",
                "tradable": True,
                "shortable": self.shortable,
                "easy_to_borrow": self.borrowable,
                "fractionable": True,
            }
        elif path.startswith("/v2/positions/"):
            if self.position is None:
                return httpx.Response(404, json={})
            data = self.position
        elif path == "/v2/orders" and method == "POST":
            body = kwargs["json"]
            data = body | {
                "id": "fixture-order",
                "status": "accepted",
                "filled_qty": "0",
            }
        else:
            raise AssertionError(f"unexpected fixture path: {path}")
        return httpx.Response(200, json=data)


@pytest.fixture
def enabled(monkeypatch):
    monkeypatch.setattr(settings, "mock_strategy_us_enabled", True)
    monkeypatch.setattr(settings, "alpaca_paper_automated_submit_enabled", True)
    monkeypatch.setattr(us_broker, "datetime", FixedDatetime)


def config(**kwargs):
    return AlpacaPaperSettings(
        api_key="fixture",
        api_secret="fixture",
        expected_account_id_suffix="paper-id",
        expected_account_number_suffix="paper-number",
        **kwargs,
    )


def request(**kwargs):
    return OrderRequest(
        **(
            {
                "symbol": "AAPL",
                "side": "buy",
                "type": "market",
                "time_in_force": "day",
                "notional": Decimal("10000001"),
                "client_order_id": "fixture-coid",
            }
            | kwargs
        )
    )


async def test_uncapped_paper_order_keeps_identity_session_and_confirm_checks(enabled):
    transport = Transport()
    broker = UsPaperBroker(settings=config(), transport=transport)
    assert (await broker.preflight(DAY)).startswith("alpaca_paper:")
    with pytest.raises(ValueError, match="confirm"):
        await broker.submit(request(), side="buy", asof=DAY)
    assert not any(method == "POST" for method, _, _ in transport.calls)
    result = await broker.submit(request(), side="buy", asof=DAY, confirm=True)
    assert result.status == "accepted"
    posts = [body["json"] for method, _, body in transport.calls if method == "POST"]
    assert len(posts) == 1 and posts[0]["notional"] == "10000001"
    assert transport.calls[0][:2] == (
        "GET",
        "/v2/account",
    )  # identity before any other endpoint


@pytest.mark.parametrize(
    "url",
    [
        "https://api.alpaca.markets",
        "http://paper-api.alpaca.markets",
        "https://paper-api.alpaca.markets.evil.invalid",
    ],
)
async def test_live_and_nonpaper_hosts_refused_before_http(enabled, url):
    transport = Transport()
    with pytest.raises(ValueError, match="paper endpoint"):
        UsPaperBroker(settings=config(base_url=url), transport=transport)
    assert not transport.calls


async def test_wrong_account_refuses_before_clock_or_order(enabled):
    transport = Transport()
    bad = AlpacaPaperSettings(
        api_key="fixture",
        api_secret="fixture",
        expected_account_id_suffix="wrong",
        expected_account_number_suffix="wrong",
    )
    broker = UsPaperBroker(settings=bad, transport=transport)
    with pytest.raises(AlpacaPaperIdentityMismatch):
        await broker.preflight(DAY)
    assert [path for _, path, _ in transport.calls] == ["/v2/account"]


async def test_both_switches_are_disabled_by_default_and_rechecked_per_send(
    enabled, monkeypatch
):
    transport = Transport()
    broker = UsPaperBroker(settings=config(), transport=transport)
    await broker.preflight(DAY)
    monkeypatch.setattr(settings, "mock_strategy_us_enabled", False)
    with pytest.raises(ValueError, match="ENABLED"):
        await broker.submit(request(), side="buy", asof=DAY, confirm=True)
    with pytest.raises(ValueError, match="ENABLED"):
        UsPaperBroker(settings=config(), transport=transport)
    monkeypatch.setattr(settings, "mock_strategy_us_enabled", True)
    monkeypatch.setattr(settings, "alpaca_paper_automated_submit_enabled", False)
    with pytest.raises(ValueError, match="ENABLED"):
        UsPaperBroker(settings=config(), transport=transport)
    assert not any(m == "POST" for m, _, _ in transport.calls)


@pytest.mark.parametrize("flag", ["shortable", "borrowable"])
async def test_short_rechecks_borrowability_at_send(enabled, flag):
    transport = Transport()
    broker = UsPaperBroker(settings=config(), transport=transport)
    await broker.preflight(DAY)
    setattr(transport, flag, False)
    with pytest.raises(ValueError, match="easy-to-borrow"):
        await broker.submit(
            request(side="sell", qty=Decimal("81"), notional=None),
            side="short",
            asof=DAY,
            confirm=True,
        )
    assert not any(m == "POST" for m, _, _ in transport.calls)


@pytest.mark.parametrize(
    "mode", ["closed", "stale_clock", "wrong_asof", "fractional_short"]
)
async def test_session_and_short_shape_refusals(enabled, mode):
    transport = Transport()
    broker = UsPaperBroker(settings=config(), transport=transport)
    await broker.preflight(DAY)
    req = request()
    day, side = DAY, "buy"
    if mode == "closed":
        transport.open = False
    elif mode == "stale_clock":
        transport.clock = datetime(2026, 11, 2, 14, 0, tzinfo=UTC)
    elif mode == "wrong_asof":
        day = date(2026, 11, 3)
    else:
        req = request(side="sell", notional=None, qty=Decimal("1.5"))
        side = "short"
    with pytest.raises(ValueError):
        await broker.submit(req, side=side, asof=day, confirm=True)
    assert not any(m == "POST" for m, _, _ in transport.calls)


async def test_close_checks_current_same_direction_available_quantity(enabled):
    transport = Transport()
    broker = UsPaperBroker(settings=config(), transport=transport)
    await broker.preflight(DAY)
    transport.position = {
        "asset_id": "fixture-asset",
        "symbol": "AAPL",
        "qty": "10",
        "qty_available": "3",
        "avg_entry_price": "100",
        "side": "long",
    }
    with pytest.raises(ValueError, match="available"):
        await broker.submit(
            request(side="sell", notional=None, qty=Decimal("4")),
            side="sell",
            asof=DAY,
            confirm=True,
        )
    with pytest.raises(ValueError, match="same-direction"):
        await broker.submit(
            request(notional=None, qty=Decimal("4")),
            side="cover",
            asof=DAY,
            confirm=True,
        )
    with pytest.raises(ValueError, match="net another"):
        await broker.submit(
            request(side="sell", notional=None, qty=Decimal("4")),
            side="short",
            asof=DAY,
            confirm=True,
        )
    assert not any(m == "POST" for m, _, _ in transport.calls)


@pytest.mark.parametrize(
    "guard", ["borrowability", "closed", "available_quantity", "read_failure"]
)
async def test_pre_dispatch_refusal_does_not_burn_the_send_claim(enabled, guard):
    from dataclasses import replace

    from app.services.mock_strategy.us import UsPaperStrategy, plan_orders
    from tests.services.mock_strategy.test_us import (
        MemoryRecorder,
        PaperBroker,
        selection,
    )

    transport = Transport()
    broker = UsPaperBroker(settings=config(), transport=transport)
    account = await broker.preflight(DAY)
    recorder = MemoryRecorder()
    planned = plan_orders(
        selection(), await PaperBroker().assets(), account_ref=account
    ).orders[0]
    original_request = transport.request
    if guard == "borrowability":
        planned = replace(planned, side="short", qty=Decimal("81"), notional=None)
        transport.borrowable = False
    elif guard == "closed":
        transport.open = False
    elif guard == "available_quantity":
        planned = replace(planned, side="sell", qty=Decimal("4"), notional=None)
        transport.position = {
            "asset_id": "fixture-asset",
            "symbol": "AAPL",
            "qty": "10",
            "qty_available": "3",
            "avg_entry_price": "100",
            "side": "long",
        }
    else:

        async def read_failure(method, path, **kwargs):
            if path.startswith("/v2/assets/"):
                raise httpx.ReadTimeout("fixture read failed")
            return await original_request(method, path, **kwargs)

        transport.request = read_failure
    row = await recorder.record(planned)
    result = await UsPaperStrategy(recorder, broker)._execute(row, DAY)
    assert result["status"] == "planned"
    assert row.status == "planned"
    assert not any(m == "POST" for m, _, _ in transport.calls)
    transport.request = original_request
    transport.open = transport.borrowable = True
    if transport.position is not None:
        transport.position["qty_available"] = "10"
    await UsPaperStrategy(recorder, broker)._execute(row, DAY)
    assert row.status == "accepted"
    assert sum(m == "POST" for m, _, _ in transport.calls) == 1


async def test_late_guard_no_post_proof_is_terminal_without_reopening_claim(
    enabled, monkeypatch
):
    from app.services.mock_strategy.us import UsPaperStrategy, plan_orders
    from tests.services.mock_strategy.test_us import (
        MemoryRecorder,
        PaperBroker,
        selection,
    )

    transport = Transport()
    broker = UsPaperBroker(settings=config(), transport=transport)
    account = await broker.preflight(DAY)
    recorder = MemoryRecorder()
    row = await recorder.record(
        plan_orders(
            selection(), await PaperBroker().assets(), account_ref=account
        ).orders[0]
    )
    prepare = broker.prepare
    calls = 0

    async def close_after_claim(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            transport.open = False
        return await prepare(*args, **kwargs)

    monkeypatch.setattr(broker, "prepare", close_after_claim)
    await UsPaperStrategy(recorder, broker)._execute(row, DAY)
    assert row.status == "rejected" and row.meta["no_order_proven"] is True
    assert not row.broker_order_id and row.meta["filled_qty"] == "0"
    assert not await recorder.claim(row.client_order_id)
    assert not any(m == "POST" for m, _, _ in transport.calls)


@pytest.mark.parametrize(
    "exception_type", [ValueError, us_broker.PreDispatchRefusal, asyncio.CancelledError]
)
async def test_errors_after_post_boundary_are_always_uncertain(
    enabled, monkeypatch, exception_type
):
    from app.services.mock_strategy.us import UsPaperStrategy, plan_orders
    from tests.services.mock_strategy.test_us import (
        MemoryRecorder,
        PaperBroker,
        selection,
    )

    transport = Transport()
    original_request = transport.request
    failure = exception_type("fixture post outcome unknown")

    async def lost_response(method, path, **kwargs):
        if method == "POST":
            transport.calls.append((method, path, kwargs))
            raise failure
        return await original_request(method, path, **kwargs)

    monkeypatch.setattr(transport, "request", lost_response)
    broker = UsPaperBroker(settings=config(), transport=transport)
    account = await broker.preflight(DAY)
    recorder = MemoryRecorder()
    row = await recorder.record(
        plan_orders(
            selection(), await PaperBroker().assets(), account_ref=account
        ).orders[0]
    )
    if exception_type is asyncio.CancelledError:
        recorder.fail_updates = True
        with pytest.raises(asyncio.CancelledError) as caught:
            await UsPaperStrategy(recorder, broker)._execute(row, DAY)
        assert caught.value is failure and row.status == "sending"
    else:
        await UsPaperStrategy(recorder, broker)._execute(row, DAY)
        assert row.status == "uncertain" and not row.meta.get("no_order_proven")
    assert not await recorder.claim(row.client_order_id)
    assert sum(m == "POST" for m, _, _ in transport.calls) == 1


@pytest.mark.parametrize(
    "initial_status, response, prior_fill, resolves",
    [
        ("sending", "404", None, True),
        ("uncertain", "404", None, True),
        ("uncertain", "error", None, False),
        ("sending", "timeout", None, False),
        ("uncertain", "500", None, False),
        ("uncertain", "found", None, False),
        ("uncertain", "empty", None, False),
        ("uncertain", "malformed", None, False),
        ("uncertain", "404", "10", False),
        ("rejected", "404", None, False),
    ],
)
async def test_lnth_exit_absence_requires_exact_404(
    enabled, initial_status, response, prior_fill, resolves
):
    from dataclasses import replace

    from app.services.mock_strategy.us import UsPaperStrategy
    from tests.services.mock_strategy.test_us import (
        MemoryRecorder,
        PaperBroker,
        asset,
        selection,
    )

    recorder, broker = MemoryRecorder(), PaperBroker()
    broker.asset_map = {"LNTH": asset("LNTH")}
    strategy = UsPaperStrategy(recorder, broker)
    signals = selection()
    await strategy.run(
        asof=DAY,
        selection=lambda: replace(signals, up=(replace(signals.up[0], symbol="LNTH"),)),
        top_n=1,
        confirm=True,
    )
    broker.fail_submit = True
    await strategy.close_due(asof=date(2026, 11, 9), confirm=True)
    original = next(row for row in recorder.rows.values() if row.side == "sell")
    original.status = initial_status
    if prior_fill is not None:
        original.meta = {**original.meta, "filled_qty": prior_fill}
    before = dict(original.meta)
    attempts = []

    class LookupTransport(Transport):
        async def request(self, method, path, **kwargs):
            if path == "/v2/orders:by_client_order_id":
                assert method == "GET"
                assert kwargs["params"] == {"client_order_id": original.client_order_id}
                attempts.append(original.client_order_id)
                if response == "error":
                    raise httpx.ConnectError("fixture connection error")
                if response == "timeout":
                    raise httpx.ReadTimeout("fixture read timeout")
                if response == "found":
                    order = next(
                        order for side, order in broker.posts if side == "sell"
                    )
                    return httpx.Response(
                        200,
                        json=order.model_dump(mode="json")
                        | {
                            "id": "existing-order",
                            "status": "canceled",
                            "filled_qty": "0",
                        },
                    )
                if response == "empty":
                    return httpx.Response(200, json={})
                if response == "malformed":
                    return httpx.Response(200, content=b"{")
                return httpx.Response(int(response), json={})
            return await super().request(method, path, **kwargs)

    paper = UsPaperBroker(settings=config(), transport=LookupTransport())
    await paper.preflight(DAY)
    lookup = broker.lookup

    async def lookup_exit(client_order_id):
        if client_order_id == original.client_order_id:
            return await paper.lookup(client_order_id)
        return await lookup(client_order_id)

    broker.lookup = lookup_exit
    broker.fail_submit = False
    await strategy.close_due(asof=date(2026, 11, 10), confirm=True)
    await strategy.close_due(asof=date(2026, 11, 10), confirm=True)
    sells = [order for side, order in broker.posts if side == "sell"]
    if not resolves:
        assert original.status == initial_status
        assert original.meta == before
        assert len(sells) == 1
        assert len(attempts) == (0 if initial_status == "rejected" else 2)
        return
    assert attempts == [original.client_order_id]
    assert original.status == "rejected"
    assert original.meta["broker_status"] == "broker_confirmed_absent"
    assert original.meta["filled_qty"] == "0"
    assert len(sells) == 2
    assert sells[0].client_order_id != sells[1].client_order_id
    assert sells[1].qty == Decimal("100")


async def test_us_paper_absence_has_no_live_order_imports():
    allowed_services = ("app.services.brokers.alpaca.", "app.services.mock_strategy.")
    for module in (us, us_broker, recorder_module):
        tree = ast.parse(Path(module.__file__).read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                imports = [node.module or ""]
            elif isinstance(node, ast.Import):
                imports = [alias.name for alias in node.names]
            else:
                continue
            for name in imports:
                if name.startswith("app.services."):
                    assert name.startswith(allowed_services), (module.__name__, name)


async def test_account_404_is_not_order_absence():
    class MissingAccountTransport(Transport):
        async def request(self, method, path, **kwargs):
            assert (method, path) == ("GET", "/v2/account")
            return httpx.Response(404, json={})

    service = AlpacaPaperBrokerService(
        settings=config(), transport=MissingAccountTransport()
    )
    with pytest.raises(AlpacaPaperRequestError) as error:
        await service.get_order_by_client_order_id(
            "fixture-order", report_not_found=True
        )
    assert not isinstance(error.value, AlpacaPaperOrderNotFound)
